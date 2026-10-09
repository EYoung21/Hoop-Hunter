"""Inference throughput benchmark for the court detector. Run ON the instance from ~/hh/pipeline:

    PYTHONPATH=. ~/hhvenv/bin/python cloud/bench_infer.py --weights weights/best.pt [--trt]

Measures, at imgsz 320:
  * raw model forward (fused, FP32 vs FP16) on GPU-resident batches of 32..1024 tiles,
  * NMS cost per batch,
  * end-to-end Ultralytics predict() on real 320 px tiles (numpy BGR, CPU preprocessing included),
  * optionally a TensorRT FP16 engine (export + predict), skipped on any error.
Results go to out/bench_infer.json.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from hoop_pipeline import config
from hoop_pipeline.common import setup_logging


def real_tiles(n: int) -> list[np.ndarray]:
    tiles: list[np.ndarray] = []
    for p in sorted((config.DATASETS_DIR / "us" / "images" / "val").glob("*.jpg"))[: n * 2]:
        tiles.append(np.ascontiguousarray(np.asarray(Image.open(p).convert("RGB"))[..., ::-1]))
        if len(tiles) >= n:
            break
    while len(tiles) < n:
        tiles += tiles[: n - len(tiles)]
    return tiles


def timed(fn: Any, min_s: float = 4.0) -> tuple[float, int]:
    import torch

    fn()
    torch.cuda.synchronize()
    n, t0 = 0, time.perf_counter()
    while time.perf_counter() - t0 < min_s:
        fn()
        n += 1
    torch.cuda.synchronize()
    return (time.perf_counter() - t0) / n, n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", type=Path, default=config.DEFAULT_WEIGHTS)
    ap.add_argument("--imgsz", type=int, default=320)
    ap.add_argument("--batches", type=int, nargs="+", default=[32, 64, 128, 256, 512, 1024])
    ap.add_argument("--trt", action="store_true", help="also try a TensorRT FP16 engine")
    ap.add_argument("--trt-batch", type=int, default=256)
    ap.add_argument("--out", type=Path, default=config.OUT_DIR / "bench_infer.json")
    args = ap.parse_args()
    setup_logging(0)
    import torch

    config.init_ultralytics()
    from ultralytics import YOLO

    from hoop_pipeline.scan_state import fp16_kwargs

    res: dict[str, Any] = {"gpu": torch.cuda.get_device_name(0), "torch": torch.__version__, "imgsz": args.imgsz,
                           "weights": str(args.weights)}
    yolo = YOLO(str(args.weights))
    net = yolo.model.fuse().eval().cuda()
    raw: dict[str, Any] = {}
    for dtype_name, dtype in (("fp32", torch.float32), ("fp16", torch.float16)):
        m = net.half() if dtype == torch.float16 else net.float()
        for b in args.batches:
            x = torch.rand(b, 3, args.imgsz, args.imgsz, device="cuda", dtype=dtype)
            try:
                with torch.inference_mode():
                    torch.cuda.reset_peak_memory_stats()
                    dt, _ = timed(lambda m=m, x=x: m(x))
                raw[f"{dtype_name}_b{b}"] = {"tiles_per_s": round(b / dt, 1), "ms_per_batch": round(dt * 1e3, 2),
                                             "peak_mem_gb": round(torch.cuda.max_memory_allocated() / 1e9, 2)}
            except torch.cuda.OutOfMemoryError:
                raw[f"{dtype_name}_b{b}"] = "OOM"
                torch.cuda.empty_cache()
            print(dtype_name, b, raw[f"{dtype_name}_b{b}"], flush=True)
    res["raw_forward"] = raw
    # Bigger tiles at the same 0.6 m GSD (the network is fully convolutional): pixel throughput
    # decides whether 640 px tiles (1.38x overlap overhead) beat 320 px tiles (2.04x).
    big: dict[str, Any] = {}
    m16 = net.half()
    for sz, b in ((640, 64), (640, 128), (640, 256), (1024, 32), (1024, 64)):
        x = torch.rand(b, 3, sz, sz, device="cuda", dtype=torch.float16)
        try:
            with torch.inference_mode():
                dt, _ = timed(lambda x=x: m16(x))
            big[f"fp16_{sz}px_b{b}"] = {"tiles_per_s": round(b / dt, 1),
                                         "Mpx_per_s": round(b * sz * sz / dt / 1e6, 1),
                                         "equiv_320px_tiles_per_s": round(b / dt * (sz / 320) ** 2, 1)}
        except torch.cuda.OutOfMemoryError:
            big[f"fp16_{sz}px_b{b}"] = "OOM"
            torch.cuda.empty_cache()
        print("fp16", sz, b, big[f"fp16_{sz}px_b{b}"], flush=True)
    res["raw_forward_large_tiles"] = big
    # NMS cost on a realistic batch output
    try:
        try:
            from ultralytics.utils.nms import non_max_suppression
        except ImportError:
            from ultralytics.utils.ops import non_max_suppression
        x = torch.rand(256, 3, args.imgsz, args.imgsz, device="cuda", dtype=torch.float16)
        with torch.inference_mode():
            pred = net.half()(x)
            p0 = pred[0] if isinstance(pred, (list, tuple)) else pred
            dt, _ = timed(lambda: non_max_suppression(p0.float(), conf_thres=0.05, iou_thres=0.5), 2.0)
        res["nms_ms_per_256"] = round(dt * 1e3, 2)
    except Exception as exc:  # API drift between versions
        res["nms_ms_per_256"] = f"skipped: {type(exc).__name__}: {exc}"
    print("nms", res["nms_ms_per_256"], flush=True)
    # end-to-end predict on real tiles
    tiles = real_tiles(2048)
    e2e: dict[str, Any] = {}
    yolo = YOLO(str(args.weights))
    for b in (128, 256, 512):
        def run(b: int = b) -> None:
            for k in range(0, len(tiles), b):
                yolo.predict(tiles[k:k + b], imgsz=args.imgsz, conf=0.05, iou=0.5, device="0", verbose=False,
                             batch=b, **fp16_kwargs(True))
        run()
        t0 = time.perf_counter()
        run()
        dt = time.perf_counter() - t0
        e2e[f"b{b}"] = round(len(tiles) / dt, 1)
        print("predict e2e", b, e2e[f"b{b}"], flush=True)
    res["predict_end_to_end_tiles_per_s"] = e2e
    if args.trt:
        t0 = time.perf_counter()
        try:
            engine = YOLO(str(args.weights)).export(format="engine", half=True, imgsz=args.imgsz, batch=args.trt_batch,
                                                    device=0, workspace=8)
            res["trt_export_s"] = round(time.perf_counter() - t0, 1)
            trt = YOLO(str(engine), task="detect")
            b = args.trt_batch
            batch = tiles[:b]
            trt.predict(batch, imgsz=args.imgsz, conf=0.05, device="0", verbose=False, batch=b)
            t1 = time.perf_counter()
            for _ in range(5):
                trt.predict(batch, imgsz=args.imgsz, conf=0.05, device="0", verbose=False, batch=b)
            res["trt_predict_end_to_end_tiles_per_s"] = round(5 * b / (time.perf_counter() - t1), 1)
        except Exception as exc:
            res["trt"] = f"failed after {time.perf_counter() - t0:.0f}s: {type(exc).__name__}: {str(exc)[:300]}"
        print("trt", {k: v for k, v in res.items() if k.startswith("trt")}, flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=1))
    print(json.dumps(res), flush=True)


if __name__ == "__main__":
    main()
