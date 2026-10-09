"""Dense NAIP read benchmark (brute-force style): read whole items at 0.6 m, measure MB/s and tiles/s.

Run ON the instance from ~/hh/pipeline:
    PYTHONPATH=. ~/hhvenv/bin/python cloud/bench_reads.py --state TN --year 2023 --items 12 --threads 32 64 128
    PYTHONPATH=. ~/hhvenv/bin/python cloud/bench_reads.py --state OH --year 2023 --items 8 --threads 64   # 0.3 m items

Every setting reads *different* items (no cache reuse). Bytes come from /proc/net/dev of the
default interface, so they are what actually crossed the wire. "Tiles" are 320 px tiles with
96 px overlap (stride 224 px = 134.4 m), i.e. 55.4 tiles per km^2, as in scan_state.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from hoop_pipeline.common import setup_logging
from hoop_pipeline.naip_index import GDAL_ENV_BULK, NaipCatalog, _signed

TILE_STRIDE_M = (320 - 96) * 0.6


def rx_bytes() -> int:
    best = 0
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, rest = line.split(":", 1)
        if name.strip() in ("lo",) or name.strip().startswith("docker"):
            continue
        best = max(best, int(rest.split()[0]))
    return best


def cpu_seconds() -> float:
    t = os.times()
    return t.user + t.system


def read_item_blocks(href: str, blocks: list[tuple[int, int, int, int]], out_scale: float) -> int:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    px = 0
    with rasterio.Env(**GDAL_ENV_BULK):
        with rasterio.open(_signed(href)) as src:
            for c0, r0, w, h in blocks:
                ow, oh = max(1, int(round(w * out_scale))), max(1, int(round(h * out_scale)))
                rs = Resampling.nearest if out_scale == 1 else Resampling.average
                a = src.read([1, 2, 3], window=Window(c0, r0, w, h), out_shape=(3, oh, ow), resampling=rs)
                px += a.shape[1] * a.shape[2]
    return px


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="TN")
    ap.add_argument("--year", type=int, default=2023)
    ap.add_argument("--items", type=int, default=12, help="items per setting")
    ap.add_argument("--threads", type=int, nargs="+", default=[32, 64, 128])
    ap.add_argument("--block", type=int, default=2048, help="native px per read window")
    ap.add_argument("--blocks-per-task", type=int, default=4)
    ap.add_argument("--gsd", type=float, default=0.6)
    ap.add_argument("--out", type=Path, default=Path("out/bench_reads.json"))
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    setup_logging(0)
    cat = NaipCatalog.load()
    pool_idx = cat.items_for_state(args.state, args.year)
    pool_idx = pool_idx[cat.years[pool_idx] == args.year]
    rng = random.Random(args.seed)
    picks = rng.sample(list(pool_idx), min(len(pool_idx), args.items * len(args.threads)))
    _signed(str(cat.hrefs[picks[0]]))  # warm the SAS token
    results = []
    for k, threads in enumerate(args.threads):
        items = picks[k * args.items:(k + 1) * args.items]
        tasks = []
        area_km2 = 0.0
        for it in items:
            w, h = int(cat.width[it]), int(cat.height[it])
            native = abs(float(cat.transform[it, 0]))
            area_km2 += w * h * native * native / 1e6
            blocks = [(c, r, min(args.block, w - c), min(args.block, h - r))
                      for r in range(0, h, args.block) for c in range(0, w, args.block)]
            for j in range(0, len(blocks), args.blocks_per_task):
                tasks.append((str(cat.hrefs[it]), blocks[j:j + args.blocks_per_task], native / args.gsd))
        rng.shuffle(tasks)
        b0, c0, t0 = rx_bytes(), cpu_seconds(), time.monotonic()
        with ThreadPoolExecutor(threads) as pool:
            px = sum(pool.map(lambda t: read_item_blocks(*t), tasks))
        el = time.monotonic() - t0
        mb = (rx_bytes() - b0) / 1e6
        cpu = cpu_seconds() - c0
        out_km2 = px * args.gsd * args.gsd / 1e6
        res = {
            "state": args.state, "year": args.year, "native_gsd": float(np.median(np.abs(cat.transform[items, 0]))),
            "threads": threads, "items": len(items), "read_windows": sum(len(t[1]) for t in tasks),
            "seconds": round(el, 1), "MB": round(mb, 1), "MB_per_s": round(mb / el, 1),
            "Mpx_per_s_at_0.6m": round(px / el / 1e6, 2), "km2": round(out_km2, 1), "km2_per_s": round(out_km2 / el, 2),
            "MB_per_km2": round(mb / max(out_km2, 1e-9), 2), "item_area_km2": round(area_km2, 1),
            "tiles_per_s_320_overlap96": round(out_km2 / el * 1e6 / TILE_STRIDE_M ** 2, 1),
            "cpu_cores_busy": round(cpu / el, 1),
        }
        print(json.dumps(res), flush=True)
        results.append(res)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    prev = json.loads(args.out.read_text()) if args.out.exists() else []
    args.out.write_text(json.dumps(prev + results, indent=1))


if __name__ == "__main__":
    main()
