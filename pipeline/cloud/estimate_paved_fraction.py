"""Estimate what fraction of CONUS a "paved pixels + buffer" scan mask would cover.

Proxy for NLCD impervious > 0 %: USDA CDL (Planetary Computer ``usda-cdl``, type ``cropland``,
30 m, EPSG:5070), whose classes 121-124 are NLCD's developed classes (open space, low, medium,
high intensity: every NLCD pixel with mapped impervious surface, roads included). Windows are
sampled uniformly by area over CONUS; each is dilated by 1 and 2 pixels (~30 m / ~60 m buffer).

    python cloud/estimate_paved_fraction.py --samples 400 --win 200
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
import shapely
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hoop_pipeline.naip_index import _signed  # noqa: E402
from hoop_pipeline.states import States  # noqa: E402

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
DEVELOPED = (121, 122, 123, 124)


def cdl_items(year: int) -> list[dict]:
    body = {"collections": ["usda-cdl"], "datetime": f"{year}-01-01T00:00:00Z/{year}-12-31T23:59:59Z",
            "query": {"usda_cdl:type": {"eq": "cropland"}}, "limit": 1000}
    items: list[dict] = []
    req: tuple[str, str, dict | None] | None = ("POST", STAC, body)
    while req:
        method, url, b = req
        r = requests.post(url, json=b, timeout=120) if method == "POST" else requests.get(url, timeout=120)
        r.raise_for_status()
        d = r.json()
        items += d["features"]
        nxt = [ln for ln in d.get("links", []) if ln.get("rel") == "next"]
        if not nxt or not d["features"]:
            break
        req = (nxt[0].get("method", "GET"), nxt[0]["href"], nxt[0].get("body"))
    return items


def dilate(mask: np.ndarray, r: int) -> np.ndarray:
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx or dy:
                out[max(0, dy):mask.shape[0] + min(0, dy), max(0, dx):mask.shape[1] + min(0, dx)] |= \
                    mask[max(0, -dy):mask.shape[0] + min(0, -dy), max(0, -dx):mask.shape[1] + min(0, -dx)]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2021)
    ap.add_argument("--samples", type=int, default=400)
    ap.add_argument("--win", type=int, default=200, help="window side in 30 m pixels")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "out" / "paved_fraction.json")
    args = ap.parse_args()
    import rasterio
    from rasterio.windows import Window

    t0 = time.time()
    items = cdl_items(args.year)
    grid = []
    for it in items:
        a, _, c, _, e, f = it["properties"]["proj:transform"][:6]
        h, w = it["properties"]["proj:shape"]
        grid.append((c, f + h * e, c + w * a, f, it["assets"]["cropland"]["href"], a, e))
    print(f"{len(grid)} CDL {args.year} cropland tiles", flush=True)
    to_alb = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    conus = shapely.union_all([shapely.transform(g, lambda xy: np.column_stack(to_alb.transform(xy[:, 0], xy[:, 1])))
                               for g in States.load().geoms])
    shapely.prepare(conus)
    minx, miny, maxx, maxy = conus.bounds
    rng = random.Random(args.seed)
    pts = []
    while len(pts) < args.samples:
        x, y = rng.uniform(minx, maxx), rng.uniform(miny, maxy)
        if shapely.intersects_xy(conus, x, y):
            pts.append((x, y))

    def sample(pt: tuple[float, float]) -> dict | None:
        x, y = pt
        for x0, y0, x1, y1, href, a, e in grid:
            if x0 <= x < x1 and y0 <= y < y1:
                col, row = int((x - x0) / a), int((y - y1) / e)
                with rasterio.open(_signed(href)) as src:
                    win = Window(col - args.win // 2, row - args.win // 2, args.win, args.win)
                    arr = src.read(1, window=win, boundless=True, fill_value=0)
                valid = arr > 0
                if valid.sum() < 0.5 * arr.size:
                    return None
                dev = np.isin(arr, DEVELOPED)
                buf = dilate(dev, 1) & valid
                out = {"valid": int(valid.sum()), "dev": int((dev & valid).sum()),
                       "dev_buf30": int(buf.sum()), "dev_buf60": int((dilate(dev, 2) & valid).sum())}
                # What a scan actually touches: cells that contain any buffered-mask pixel.
                # 4 px = 120 m ~ 320 px tile stride (134 m); 10 px = 300 m ~ one 512 px COG block (307 m);
                # 11 px = 330 m ~ 640 px tile stride (326 m).
                for k in (4, 10, 11):
                    n = (arr.shape[0] // k) * k
                    cells_any = buf[:n, :n].reshape(n // k, k, n // k, k).any(axis=(1, 3))
                    cells_valid = valid[:n, :n].reshape(n // k, k, n // k, k).mean(axis=(1, 3)) > 0.5
                    out[f"cells{k}_any"] = int((cells_any & cells_valid).sum())
                    out[f"cells{k}_valid"] = int(cells_valid.sum())
                return out
        return None

    with ThreadPoolExecutor(args.threads) as pool:
        rows = [r for r in pool.map(sample, pts) if r]
    fr = {k: np.array([r[k] / r["valid"] for r in rows]) for k in ("dev", "dev_buf30", "dev_buf60")}
    conus_km2 = conus.area / 1e6
    res = {"proxy": f"USDA CDL {args.year} classes 121-124 (NLCD developed), 30 m", "windows": len(rows),
           "window_km": args.win * 0.03, "conus_km2": round(conus_km2), "seconds": round(time.time() - t0, 1)}
    for k in (4, 10, 11):
        fr[f"cells_{k * 30}m_touching_buf30"] = np.array([r[f"cells{k}_any"] / max(1, r[f"cells{k}_valid"]) for r in rows])
    for k, v in fr.items():
        se = v.std(ddof=1) / math.sqrt(len(v))
        res[k] = {"fraction": round(float(v.mean()), 4), "se": round(float(se), 4),
                  "km2": round(float(v.mean()) * conus_km2), "median_window": round(float(np.median(v)), 4)}
    print(json.dumps(res, indent=1))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
