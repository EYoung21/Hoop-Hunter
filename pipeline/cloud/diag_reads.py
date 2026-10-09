"""Diagnose NAIP read concurrency on the instance: signing, parallel opens, parallel window reads."""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import planetary_computer
import rasterio

from hoop_pipeline.naip_index import GDAL_ENV_BULK, NaipCatalog, read_square

cat = NaipCatalog.load()
idx = cat.items_for_state("TN", 2023)[: int(sys.argv[1]) if len(sys.argv) > 1 else 32]
hrefs = [str(cat.hrefs[i]) for i in idx]

t = time.time()
signed = [planetary_computer.sign(h) for h in hrefs]
print(f"sign x{len(hrefs)} sequential: {time.time() - t:.2f}s")


def open_only(url: str) -> float:
    t0 = time.time()
    with rasterio.Env(**GDAL_ENV_BULK):
        with rasterio.open(url) as src:
            _ = src.width
    return time.time() - t0


def open_read(url: str) -> float:
    t0 = time.time()
    with rasterio.Env(**GDAL_ENV_BULK):
        with rasterio.open(url) as src:
            read_square(src, 4000, 4000, 320, 320)
    return time.time() - t0


for name, fn in (("open", open_only), ("open+read", open_read)):
    t = time.time()
    one = fn(signed[0])
    print(f"{name} single: {one:.2f}s")
    for workers in (1, 8, 32):
        t = time.time()
        with ThreadPoolExecutor(workers) as pool:
            per = list(pool.map(fn, signed[:workers * 2] if workers > 1 else signed[:4]))
        el = time.time() - t
        print(f"{name} x{len(per)} with {workers} threads: wall {el:.2f}s, mean per-call {np.mean(per):.2f}s")
