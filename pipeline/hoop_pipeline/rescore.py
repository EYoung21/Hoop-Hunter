"""Second opinion on a scan's candidates from fresh NAIP chips: another detector, the verifier and the context CNN.

Runs on CPU (or GPU when there is one) for a few thousand candidates, so a scan made with one model can be
filtered with a newer one without re-scanning the state:

    python -m hoop_pipeline.rescore --candidates out/scan2_tn_v1/detections_privacy_rules.parquet --min-conf 0.4 \
        --weights weights/best_v2.pt --imgsz 416 --verifier weights/verifier_v2.pt \
        --context-model weights/context_v2.pt --chips-dir data/rescore/tn_v1 --out out/scan2_tn_v1/rescore.parquet

For every candidate not near a known OSM court with confidence >= --min-conf it fetches a 320 px (192 m)
chip centred on the box (cached as JPEG in --chips-dir, so a rerun only scores), then records:

* ``conf2``: best score of the second detector over boxes that overlap the candidate's box (IoU >= 0.3) or
  contain its centre (0 when it sees nothing there);
* ``verify``: the box verifier (cropcls, ResNet-18 on the candidate box with 1.6x context);
* ``p_home``: the context CNN on the central 160 m (feed it to ``privacy classify --p-home``).
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import config
from .common import add_common_args, setup_logging

log = logging.getLogger(__name__)

CHIP_PX = 320


def fetch_chips(lons: np.ndarray, lats: np.ndarray, states: np.ndarray, naip_dir: Path,
                workers: int = 32) -> tuple[np.ndarray, np.ndarray]:
    from .privacy import fetch_context_crops

    return fetch_context_crops(lons, lats, states, CHIP_PX, naip_dir, workers=workers)


def best_overlap(dets: np.ndarray, box: np.ndarray) -> float:
    """Max score of detections (K, 5: x0 y0 x1 y1 score) overlapping ``box`` (IoU >= 0.3) or containing its centre."""
    if not len(dets):
        return 0.0
    ix = np.clip(np.minimum(dets[:, 2], box[2]) - np.maximum(dets[:, 0], box[0]), 0, None)
    iy = np.clip(np.minimum(dets[:, 3], box[3]) - np.maximum(dets[:, 1], box[1]), 0, None)
    inter = ix * iy
    union = (dets[:, 2] - dets[:, 0]) * (dets[:, 3] - dets[:, 1]) + (box[2] - box[0]) * (box[3] - box[1]) - inter
    iou = inter / np.maximum(union, 1e-9)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    inside = (dets[:, 0] <= cx) & (cx <= dets[:, 2]) & (dets[:, 1] <= cy) & (cy <= dets[:, 3])
    m = (iou >= 0.3) | inside
    return float(dets[m, 4].max()) if m.any() else 0.0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", type=Path, required=True, help="scan_engine merge (or privacy classify) parquet")
    ap.add_argument("--min-conf", type=float, default=0.4)
    ap.add_argument("--min-verify", type=float, default=None,
                    help="also require the candidates file's own verifier score (the scan's in-stream 'verify') >= this")
    ap.add_argument("--include-known", action="store_true", help="also candidates near a known OSM court")
    ap.add_argument("--weights", type=Path, default=Path("weights/best_v2.pt"))
    ap.add_argument("--imgsz", type=int, default=416)
    ap.add_argument("--verifier", type=Path)
    ap.add_argument("--context-model", type=Path)
    ap.add_argument("--chips-dir", type=Path, required=True)
    ap.add_argument("--naip-dir", type=Path, default=config.NAIP_GEOPARQUET_DIR)
    ap.add_argument("--chunk", type=int, default=512, help="candidates fetched per round (grouped by NAIP item)")
    ap.add_argument("--batch", type=int, default=32, help="chips per detector / classifier batch")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--shard", help="K/N: only every N-th candidate (by confidence rank) starting at K")
    ap.add_argument("--out", type=Path, required=True)
    add_common_args(ap)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    import torch
    from PIL import Image

    from .cropcls import CropClassifier
    from .fastdet import FastDetector

    t0 = time.monotonic()
    d = pq.read_table(args.candidates).to_pydict()
    conf = np.asarray(d["conf"], float)
    near = np.asarray(d.get("near_known_osm") or [""] * len(conf), dtype=object)
    keep = (conf >= args.min_conf) & ((near == "") | args.include_known)
    if args.min_verify is not None:
        v_scan = np.asarray([np.nan if v is None else v for v in d.get("verify") or [None] * len(conf)], float)
        keep &= np.nan_to_num(v_scan, nan=-1.0) >= args.min_verify
    sel = np.flatnonzero(keep)
    sel = sel[np.argsort(-conf[sel])]
    if args.shard:  # K/N: every N-th candidate starting at K, so N nodes can split one rescore
        k_, n_ = (int(v) for v in args.shard.split("/"))
        sel = sel[k_::n_]
    log.info("%d candidates with conf >= %.2f%s%s%s", len(sel), args.min_conf,
             "" if args.min_verify is None else f", scan verify >= {args.min_verify:.2f}",
             "" if args.include_known else " (not near OSM)", f", shard {args.shard}" if args.shard else "")
    ids = np.asarray(d["id"], dtype=object)
    lon, lat = np.asarray(d["lon"], float), np.asarray(d["lat"], float)
    st = np.asarray(d.get("state") or ["TN"] * len(conf), dtype=object)
    bw, bh = np.asarray(d["box_w_m"], float), np.asarray(d["box_h_m"], float)
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    det = FastDetector(args.weights, imgsz=args.imgsz, device=device, half=device != "cpu")
    ver = CropClassifier.load(args.verifier) if args.verifier else None
    ctx = CropClassifier.load(args.context_model) if args.context_model else None
    ctx_px = int(round(float(ctx.meta.get("point_m", 160.0)) / config.TARGET_GSD_M)) if ctx else 0
    args.chips_dir.mkdir(parents=True, exist_ok=True)
    from concurrent.futures import ThreadPoolExecutor

    save_pool = ThreadPoolExecutor(max_workers=8)
    res: dict[str, list[Any]] = {k: [] for k in ("id", "conf", "chip_ok", "conf2", "verify", "p_home", "seconds_chunk")}
    fetched = 0
    for k in range(0, len(sel), args.chunk):
        tc = time.monotonic()
        idx = sel[k:k + args.chunk]
        chips = np.zeros((len(idx), CHIP_PX, CHIP_PX, 3), np.uint8)
        ok = np.zeros(len(idx), bool)
        need = []
        for j, i in enumerate(idx):
            p = args.chips_dir / f"{ids[i]}.jpg"
            if p.exists():
                chips[j] = np.asarray(Image.open(p).convert("RGB"))
                ok[j] = True
            else:
                need.append(j)
        if need:
            nj = np.asarray(need)
            imgs, got = fetch_chips(lon[idx[nj]], lat[idx[nj]], st[idx[nj]], args.naip_dir, args.workers)
            fetched += int(got.sum())
            saves = []
            for q, jj in enumerate(nj.tolist()):
                if got[q]:
                    chips[jj] = imgs[q]
                    ok[jj] = True
                    saves.append((imgs[q], args.chips_dir / f"{ids[idx[jj]]}.jpg"))
            # JPEG encoding releases the GIL; at national scale a serial save loop costs as much as the fetch
            list(save_pool.map(lambda a: Image.fromarray(a[0]).save(a[1], quality=92), saves))
            del imgs, saves
        conf2 = np.zeros(len(idx))
        verify = np.full(len(idx), np.nan)
        p_home = np.full(len(idx), np.nan)
        good = np.flatnonzero(ok)
        c = CHIP_PX / 2
        for b0 in range(0, len(good), args.batch):  # small batches: a CPU box has little RAM to spare
            gb = good[b0:b0 + args.batch]
            x = torch.from_numpy(chips[gb])
            x_pre = det.preprocess(x)
            out = det.detect(x, conf=0.05, x_pre=x_pre).cpu().numpy()
            boxes = np.zeros((len(gb), 6), np.float32)
            for q, jj in enumerate(gb.tolist()):
                ii = int(idx[jj])
                w, h = bw[ii] / config.TARGET_GSD_M, bh[ii] / config.TARGET_GSD_M
                box = np.array([c - w / 2, c - h / 2, c + w / 2, c + h / 2])
                conf2[jj] = best_overlap(out[out[:, 0] == q, 1:6], box)
                boxes[q] = [q, *box, conf[ii]]
            if ver is not None:
                verify[gb] = ver.score_boxes(x_pre.to(ver.device), torch.from_numpy(boxes).to(ver.device),
                                             CHIP_PX).float().cpu().numpy()
            if ctx is not None:
                o = (CHIP_PX - ctx_px) // 2
                p_home[gb] = ctx.score_images(chips[gb, o:o + ctx_px, o:o + ctx_px])
        for j, i in enumerate(idx.tolist()):
            res["id"].append(ids[i])
            res["conf"].append(float(conf[i]))
            res["chip_ok"].append(bool(ok[j]))
            res["conf2"].append(float(conf2[j]) if ok[j] else float("nan"))
            res["verify"].append(float(verify[j]))
            res["p_home"].append(float(p_home[j]))
            res["seconds_chunk"].append(round(time.monotonic() - tc, 2))
        log.info("%d/%d scored (%d fetched so far), chunk %.1f s, %.0f s total", min(k + args.chunk, len(sel)), len(sel),
                 fetched, time.monotonic() - tc, time.monotonic() - t0)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(res), args.out, compression="zstd")  # after every chunk: partial results survive
    log.info("rescored %d candidates in %.1f min -> %s", len(sel), (time.monotonic() - t0) / 60, args.out)


if __name__ == "__main__":
    main()
