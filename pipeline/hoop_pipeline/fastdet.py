"""Fast batched court detection on the GPU (no Ultralytics ``predict()`` in the loop).

Ultralytics' ``predict()`` spends most of its time on per-image CPU pre/post-processing
(289 tiles/s end to end in the v1 Tennessee scan versus 5,723 tiles/s for the raw FP16
forward pass). ``FastDetector`` keeps everything on the GPU:

1. uint8 tiles (N, H, W, 3) RGB, ideally from pinned memory, are copied to the GPU,
   permuted to NCHW, converted to FP16 and scaled to [0, 1];
2. if the model input size differs from the tile size the batch is resized with
   bilinear interpolation (``cv2.INTER_LINEAR`` equivalent, which is what Ultralytics
   uses to upscale training images);
3. the fused YOLO model (PyTorch FP16, or a TensorRT 10 engine exported by Ultralytics)
   returns decoded (cx, cy, w, h, score) rows;
4. low scores are dropped and per-tile NMS runs with ``torchvision.ops.batched_nms``;
5. boxes are mapped back to tile pixels and clipped.

Optional test-time augmentation (``tta="lr"`` or ``"lrud"``) runs flipped copies of
the batch, un-flips their boxes and keeps, for every box of the plain pass, the mean
of its best-matching scores over all passes (a box no flipped pass confirms has its
score diluted). That lowers the score of unstable look-alikes more than of courts.

Output of ``detect``: one float32 tensor (K, 6) on the GPU per call:
``[tile_index, x0, y0, x1, y1, score]`` in tile pixels.

Example
-------
    det = FastDetector("weights/best.pt", imgsz=416)
    out = det.detect(torch.from_numpy(tiles_uint8).cuda(), conf=0.05)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("fastdet")


@dataclass
class DetectorInfo:
    weights: str
    imgsz: int
    backend: str          # "torch-fp16" | "torch-fp32" | "tensorrt"
    tta: str
    nc: int


def _load_trt_engine(path: Path) -> Any:
    """Deserialize a TensorRT engine; Ultralytics prepends a length-prefixed JSON metadata header."""
    import tensorrt as trt

    logger = trt.Logger(trt.Logger.WARNING)
    with open(path, "rb") as fh, trt.Runtime(logger) as runtime:
        head = fh.read(4)
        try:
            meta_len = int.from_bytes(head, byteorder="little")
            json.loads(fh.read(meta_len).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            fh.seek(0)
        engine = runtime.deserialize_cuda_engine(fh.read())
    if engine is None:
        raise RuntimeError(f"could not deserialize TensorRT engine {path}")
    return engine


class _TrtRunner:
    """Minimal TensorRT 10 runner on torch tensors (static or dynamic batch)."""

    def __init__(self, path: Path, device: Any) -> None:
        import tensorrt as trt
        import torch

        self.torch = torch
        self.engine = _load_trt_engine(path)
        self.context = self.engine.create_execution_context()
        self.device = device
        self.inputs: list[str] = []
        self.outputs: list[str] = []
        dt_map = {trt.float16: torch.float16, trt.float32: torch.float32, trt.int32: torch.int32,
                  trt.int8: torch.int8, trt.bool: torch.bool}
        self.dtypes: dict[str, Any] = {}
        for i in range(self.engine.num_io_tensors):
            name = self.engine.get_tensor_name(i)
            self.dtypes[name] = dt_map[self.engine.get_tensor_dtype(name)]
            if self.engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                self.inputs.append(name)
            else:
                self.outputs.append(name)
        shape = tuple(self.engine.get_tensor_shape(self.inputs[0]))
        self.max_batch = shape[0] if shape[0] > 0 else int(self.engine.get_tensor_profile_shape(self.inputs[0], 0)[2][0])
        self.static = shape[0] > 0
        self.input_hw = (int(shape[2]), int(shape[3]))

    def __call__(self, x: Any) -> Any:
        torch = self.torch
        n = x.shape[0]
        if self.static and n != self.max_batch:  # pad to the engine's fixed batch
            pad = torch.zeros((self.max_batch - n, *x.shape[1:]), dtype=x.dtype, device=x.device)
            x = torch.cat([x, pad], 0)
        x = x.to(self.dtypes[self.inputs[0]]).contiguous()
        self.context.set_input_shape(self.inputs[0], tuple(x.shape))
        self.context.set_tensor_address(self.inputs[0], x.data_ptr())
        outs = []
        for name in self.outputs:
            shp = tuple(self.context.get_tensor_shape(name))
            o = torch.empty(shp, dtype=self.dtypes[name], device=self.device)
            self.context.set_tensor_address(name, o.data_ptr())
            outs.append(o)
        stream = torch.cuda.current_stream(self.device)
        if not self.context.execute_async_v3(stream.cuda_stream):
            raise RuntimeError("TensorRT execution failed")
        return outs[0][:n]


class FastDetector:
    """YOLO detector with GPU pre/post-processing. See module docstring."""

    def __init__(self, weights: str | Path, imgsz: int | None = None, device: str = "cuda:0", half: bool = True,
                 engine: str | Path | None = None, tta: str = "none") -> None:
        import torch

        from . import config

        config.init_ultralytics()
        from ultralytics import YOLO

        self.torch = torch
        self.device = torch.device(device if torch.cuda.is_available() or not device.startswith("cuda") else "cpu")
        self.half = half and self.device.type == "cuda"
        yolo = YOLO(str(weights))
        args = getattr(yolo.model, "args", {}) or {}
        train_imgsz = args.get("imgsz", 320) if isinstance(args, dict) else 320
        self.imgsz = int(imgsz or (train_imgsz if isinstance(train_imgsz, int) else train_imgsz[0]))
        self.names = yolo.names
        self.nc = len(self.names)
        self.trt: _TrtRunner | None = None
        self.model: Any = None
        if engine:
            self.trt = _TrtRunner(Path(engine), self.device)
            if self.trt.input_hw != (self.imgsz, self.imgsz):
                log.warning("engine input %s != imgsz %d; using the engine's size", self.trt.input_hw, self.imgsz)
                self.imgsz = self.trt.input_hw[0]
            backend = "tensorrt"
        else:
            net: Any = yolo.model
            m = net.fuse().eval().to(self.device)
            self.model = m.half() if self.half else m.float()
            backend = "torch-fp16" if self.half else "torch-fp32"
        if tta not in ("none", "lr", "lrud"):
            raise ValueError(f"tta must be none|lr|lrud, not {tta!r}")
        self.tta = tta
        self.info = DetectorInfo(str(weights), self.imgsz, backend, tta, self.nc)
        log.info("FastDetector %s: imgsz %d, %s, tta %s, classes %s", weights, self.imgsz, backend, tta, self.names)

    # ------------------------------------------------------------------ core
    def preprocess(self, tiles_u8: Any) -> Any:
        """(N, H, W, 3) uint8 RGB (any device) -> (N, 3, imgsz, imgsz) float16/32 on the GPU."""
        torch = self.torch
        x = tiles_u8.to(self.device, non_blocking=True).permute(0, 3, 1, 2)
        x = x.half() if self.half or self.trt is not None else x.float()
        x = x.div_(255.0)
        if x.shape[-1] != self.imgsz or x.shape[-2] != self.imgsz:
            x = torch.nn.functional.interpolate(x, size=(self.imgsz, self.imgsz), mode="bilinear", align_corners=False)
        return x.contiguous()

    def forward(self, x: Any) -> Any:
        """Raw decoded predictions (N, 4 + nc, A) as float32."""
        torch = self.torch
        with torch.inference_mode():
            if self.trt is not None:
                y = self.trt(x)
            else:
                y = self.model(x)
                if isinstance(y, (list, tuple)):
                    y = y[0]
        return y.float()

    def _candidates(self, y: Any, conf: float) -> tuple[Any, Any, Any]:
        """(tile index, xyxy in model px, score) of every anchor above conf (single-class score = max)."""
        torch = self.torch
        scores = y[:, 4:, :].amax(1) if y.shape[1] > 5 else y[:, 4, :]
        b, a = torch.nonzero(scores > conf, as_tuple=True)
        xywh = y[b, :4, a]
        xyxy = torch.cat([xywh[:, :2] - xywh[:, 2:] / 2, xywh[:, :2] + xywh[:, 2:] / 2], 1)
        return b, xyxy, scores[b, a]

    def detect(self, tiles_u8: Any, conf: float = 0.05, iou: float = 0.5, max_det: int = 100,
               x_pre: Any = None) -> Any:
        """Detections for a batch of tiles: (K, 6) float32 on the GPU, rows
        [tile_index, x0, y0, x1, y1, score] in *tile* pixels (input H/W)."""

        torch = self.torch
        n, th, tw = int(tiles_u8.shape[0]), int(tiles_u8.shape[1]), int(tiles_u8.shape[2])
        if n == 0:
            return torch.zeros((0, 6), dtype=torch.float32, device=self.device)
        x = self.preprocess(tiles_u8) if x_pre is None else x_pre
        b, xyxy, s = self._nms(*self._candidates(self.forward(x), conf), iou, max_det * n)
        if self.tta != "none" and len(b):
            s = self._tta_scores(x, b, xyxy, s, conf, iou, max_det * n)
            keep = s > conf
            b, xyxy, s = b[keep], xyxy[keep], s[keep]
        sx, sy = tw / self.imgsz, th / self.imgsz
        xyxy = xyxy * torch.tensor([sx, sy, sx, sy], device=xyxy.device)
        xyxy[:, 0::2] = xyxy[:, 0::2].clamp(0, tw)
        xyxy[:, 1::2] = xyxy[:, 1::2].clamp(0, th)
        return torch.cat([b[:, None].float(), xyxy, s[:, None]], 1)

    def _nms(self, b: Any, xyxy: Any, s: Any, iou: float, limit: int) -> tuple[Any, Any, Any]:
        import torchvision

        if not len(b):
            return b, xyxy, s
        keep = torchvision.ops.batched_nms(xyxy, s, b, iou)
        if limit and len(keep) > limit:
            keep = keep[:limit]
        return b[keep], xyxy[keep], s[keep]

    def _tta_scores(self, x: Any, b: Any, xyxy: Any, s: Any, conf: float, iou: float, limit: int) -> Any:
        """Mean over passes of the best-IoU-matching score (0 when a pass has no match >= 0.5)."""
        import torchvision

        torch = self.torch
        flips: list[tuple[int, ...]] = [(3,)] if self.tta == "lr" else [(3,), (2,), (2, 3)]
        total = s.clone()
        size = float(self.imgsz)
        low = max(0.01, conf * 0.25)
        # only tiles that have a candidate are re-run (a few % of a scan's tiles), so TTA costs little
        uniq = torch.unique(b)
        xs = x[uniq]
        for dims in flips:
            bf, xf, sf = self._nms(*self._candidates(self.forward(torch.flip(xs, dims)), low), iou, limit)
            bf = uniq[bf]
            if 3 in dims:
                xf = torch.stack([size - xf[:, 2], xf[:, 1], size - xf[:, 0], xf[:, 3]], 1)
            if 2 in dims:
                xf = torch.stack([xf[:, 0], size - xf[:, 3], xf[:, 2], size - xf[:, 1]], 1)
            if len(bf):
                # offset boxes by tile index so boxes of different tiles never overlap, then one IoU matrix
                ia = xyxy + b.float()[:, None] * (size * 4)
                fa = xf + bf.float()[:, None] * (size * 4)
                m = torchvision.ops.box_iou(ia, fa)
                total = total + torch.where(m >= 0.5, sf[None, :].expand_as(m), torch.zeros_like(m)).amax(1)
        return total / (1 + len(flips))

    # ------------------------------------------------------------------ helpers
    def detect_numpy(self, tiles: np.ndarray, conf: float = 0.05, iou: float = 0.5, batch: int = 256) -> np.ndarray:
        """Convenience: numpy (N, H, W, 3) uint8 RGB -> numpy (K, 6) detections (CPU)."""
        torch = self.torch
        outs = []
        for k in range(0, len(tiles), batch):
            t = torch.from_numpy(np.ascontiguousarray(tiles[k:k + batch]))
            d = self.detect(t, conf=conf, iou=iou)
            d[:, 0] += k
            outs.append(d.cpu().numpy())
        return np.concatenate(outs) if outs else np.zeros((0, 6), np.float32)


def export_engine(weights: Path, imgsz: int, batch: int, out: Path | None = None, workspace_gb: int = 8) -> Path:
    """Build a TensorRT FP16 engine with Ultralytics' exporter (static batch). Takes ~5-8 min on an A100."""
    from . import config

    config.init_ultralytics()
    from ultralytics import YOLO

    t0 = time.monotonic()
    path = Path(YOLO(str(weights)).export(format="engine", half=True, imgsz=imgsz, batch=batch, device=0,
                                          workspace=workspace_gb, simplify=True))
    if out is not None and path != out:
        out.parent.mkdir(parents=True, exist_ok=True)
        path.replace(out)
        path = out
    log.info("TensorRT engine %s built in %.1f min", path, (time.monotonic() - t0) / 60)
    return path


def benchmark(det: FastDetector, tile: int = 320, batch: int = 512, seconds: float = 10.0) -> dict[str, float]:
    """Steady-state tiles/s of detect() on random uint8 tiles already on the GPU (no host transfer)."""
    torch = det.torch
    x = torch.randint(0, 255, (batch, tile, tile, 3), dtype=torch.uint8, device=det.device)
    det.detect(x)
    torch.cuda.synchronize()
    n, t0 = 0, time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        det.detect(x)
        n += batch
    torch.cuda.synchronize()
    el = time.perf_counter() - t0
    return {"tiles_per_s": round(n / el, 1), "batch": batch, "tile": tile, "imgsz": det.imgsz}
