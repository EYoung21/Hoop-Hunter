"""Scan a bbox of NAIP imagery with a trained court detector.

The area is read in large overlapping blocks (one HTTP-ranged read each,
prefetched in background threads), each block is cut into model-sized tiles
with overlap, detections are mapped to lon/lat, duplicates across tiles and
blocks are merged, and anything within ``--known-radius`` metres of a court
already in OSM is dropped. The result is ``out/candidates.geojson``.

Examples
--------
    python -m hoop_pipeline.scan --region downtown_chattanooga
    python -m hoop_pipeline.scan --bbox 35.040 -85.315 35.052 -85.300 --conf 0.1 --save-crops 20
    python -m hoop_pipeline.scan --region chattanooga --device 0 --workers 8

Detections are *candidates* for human review, not courts: the app shows them
with status "candidate".
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, TypeVar

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import Point, box, shape
from shapely.geometry.base import BaseGeometry

from . import config
from .common import (
    LocalMeters,
    add_common_args,
    bbox_area_km2,
    feature_collection,
    read_json,
    setup_logging,
    stable_detection_id,
    write_json,
)
from .naip import Chip, NaipIndex, read_chip, to_lonlat

log = logging.getLogger("scan")
T = TypeVar("T")
R = TypeVar("R")


@dataclass
class Detection:
    conf: float
    lat: float
    lon: float
    x0: float  # box in local metres (east/north), see LocalMeters
    y0: float
    x1: float
    y1: float
    item_id: str
    date: str
    touches_edge: bool

    @property
    def geom(self) -> BaseGeometry:
        return box(self.x0, self.y0, self.x1, self.y1)

    @property
    def size_m(self) -> tuple[float, float]:
        return self.x1 - self.x0, self.y1 - self.y0


# --------------------------------------------------------------------------- tiling
def block_centers(
    bbox: tuple[float, float, float, float], proj: LocalMeters, block_m: float, step_m: float,
) -> list[tuple[float, float]]:
    s, w, n, e = bbox
    x_min, y_min = proj.fwd(s, w)
    x_max, y_max = proj.fwd(n, e)
    nx = max(1, math.ceil((x_max - x_min - block_m) / step_m) + 1) if x_max - x_min > block_m else 1
    ny = max(1, math.ceil((y_max - y_min - block_m) / step_m) + 1) if y_max - y_min > block_m else 1
    cx0 = x_min + block_m / 2 if nx > 1 else (x_min + x_max) / 2
    cy0 = y_min + block_m / 2 if ny > 1 else (y_min + y_max) / 2
    out = []
    for iy in range(ny):
        for ix in range(nx):
            out.append(proj.inv(cx0 + ix * step_m, cy0 + iy * step_m))
    return out


def tile_offsets(size: int, tile: int, stride: int) -> list[int]:
    if size <= tile:
        return [0]
    offs = list(range(0, size - tile + 1, stride))
    if offs[-1] != size - tile:
        offs.append(size - tile)
    return offs


def iter_tiles(chip: Chip, tile: int, stride: int, min_valid: float) -> Iterator[tuple[int, int, np.ndarray]]:
    rgb = chip.rgb
    h, w = rgb.shape[:2]
    valid = rgb.max(axis=2) > 0
    for r in tile_offsets(h, tile, stride):
        for c in tile_offsets(w, tile, stride):
            if valid[r:r + tile, c:c + tile].mean() < min_valid:
                continue
            yield c, r, rgb[r:r + tile, c:c + tile]


def prefetch(pool: ThreadPoolExecutor, fn: Callable[[T], R], items: list[T], depth: int) -> Iterator[R]:
    """Like pool.map, but keeps at most ``depth`` results in flight so a slow
    consumer (inference) can't make the readers buffer a whole county in RAM."""
    pending: deque[Future[R]] = deque()
    it = iter(items)
    for x in islice(it, depth):
        pending.append(pool.submit(fn, x))
    while pending:
        fut = pending.popleft()
        for x in islice(it, 1):
            pending.append(pool.submit(fn, x))
        yield fut.result()


def _to_numpy(x: Any) -> np.ndarray:
    """torch tensor (any device) or array -> numpy."""
    return x.cpu().numpy() if hasattr(x, "cpu") else np.asarray(x)


# --------------------------------------------------------------------------- merging
def merge_detections(dets: list[Detection], iou_thr: float, contain_thr: float) -> list[Detection]:
    """Greedy cross-tile NMS in metres. A box is suppressed by a better one if
    IoU >= iou_thr or if >= contain_thr of the smaller box lies inside the other
    (catches truncated boxes from tile edges). Complete boxes beat edge-touching
    ones of similar confidence."""
    if not dets:
        return []
    order = sorted(range(len(dets)), key=lambda i: (dets[i].conf - (0.05 if dets[i].touches_edge else 0.0)), reverse=True)
    geoms = [d.geom for d in dets]
    tree = STRtree(geoms)
    suppressed = np.zeros(len(dets), dtype=bool)
    keep: list[Detection] = []
    for i in order:
        if suppressed[i]:
            continue
        keep.append(dets[i])
        gi = geoms[i]
        for j in tree.query(gi, predicate="intersects"):
            if j == i or suppressed[j]:
                continue
            gj = geoms[j]
            inter = gi.intersection(gj).area
            if inter <= 0:
                continue
            union = gi.area + gj.area - inter
            if inter / union >= iou_thr or inter / max(1e-6, min(gi.area, gj.area)) >= contain_thr:
                suppressed[j] = True
    return keep


# --------------------------------------------------------------------------- known courts
def find_known_files(bbox: tuple[float, float, float, float], pad_deg: float = 0.001) -> list[Path]:
    """Cached fetch_osm files whose bbox covers the scan area (plus a small pad)."""
    s, w, n, e = bbox
    out = []
    for p in sorted(config.OSM_DIR.glob("*.geojson")):
        if p.name.endswith(".negatives.geojson"):
            continue
        try:
            fb = read_json(p).get("bbox")
        except (OSError, ValueError):
            continue
        if fb and fb[0] <= s - pad_deg and fb[1] <= w - pad_deg and fb[2] >= n + pad_deg and fb[3] >= e + pad_deg:
            out.append(p)
    return out


def load_known(paths: list[Path], proj: LocalMeters) -> tuple[list[BaseGeometry], STRtree | None]:
    geoms: list[BaseGeometry] = []
    for p in paths:
        for f in read_json(p)["features"]:
            g = shape(f["geometry"])  # lon/lat -> local metres
            geoms.append(shapely.transform(g, lambda xy: np.column_stack(_fwd_xy(proj, xy[:, 0], xy[:, 1]))))
    return geoms, (STRtree(geoms) if geoms else None)


def _fwd_xy(proj: LocalMeters, lon: Any, lat: Any) -> tuple[Any, Any]:
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    return (lon - proj.lon0) * proj.kx, (lat - proj.lat0) * proj.ky


def near_known(det: Detection, tree: STRtree | None, geoms: list[BaseGeometry], radius_m: float) -> bool:
    if tree is None:
        return False
    zone = det.geom.buffer(radius_m)
    center = Point((det.x0 + det.x1) / 2, (det.y0 + det.y1) / 2)
    for j in tree.query(zone, predicate="intersects"):
        g = geoms[j]
        if g.distance(center) <= radius_m or g.intersects(det.geom):
            return True
    return False


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--region", help="region name from regions.json")
    g.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    ap.add_argument("--region-label", help="value for the 'region' property (default: region name, or 'chattanooga' for --bbox)")
    ap.add_argument("--weights", type=Path, default=config.DEFAULT_WEIGHTS)
    ap.add_argument("--conf", type=float, default=0.25, help="min detection confidence to keep (export filters again)")
    ap.add_argument("--iou", type=float, default=0.5, help="NMS IoU inside a tile")
    ap.add_argument("--merge-iou", type=float, default=0.3)
    ap.add_argument("--merge-contain", type=float, default=0.6)
    ap.add_argument("--tile", type=int, default=config.CHIP_SIZE_PX, help="model tile size in px (match training)")
    ap.add_argument("--overlap", type=int, default=config.SCAN_OVERLAP_PX)
    ap.add_argument("--block", type=int, default=1600, help="pixels per NAIP read (bigger = fewer HTTP reads)")
    ap.add_argument("--gsd", type=float, default=config.TARGET_GSD_M)
    ap.add_argument("--year", type=int, help="force a NAIP year")
    ap.add_argument("--known", type=Path, nargs="*",
                    help="GeoJSON(s) of known courts to exclude (default: data/osm/<region>.geojson, fetched if missing)")
    ap.add_argument("--known-radius", type=float, default=config.KNOWN_COURT_RADIUS_M)
    ap.add_argument("--min-box-m", type=float, default=6.0, help="drop boxes with a side shorter than this")
    ap.add_argument("--max-box-m", type=float, default=120.0, help="drop boxes with a side longer than this")
    ap.add_argument("--device", help="'cpu' or GPU index (default: GPU if available)")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--workers", type=int, default=4, help="parallel NAIP block reads")
    ap.add_argument("--max-blocks", type=int, help="safety cap for large areas")
    ap.add_argument("--out", type=Path, default=config.CANDIDATES_FILE)
    ap.add_argument("--save-crops", type=int, default=0, help="write review PNGs for the top N candidates")
    add_common_args(ap)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    t_start = time.monotonic()

    if args.region:
        region = config.resolve_regions([args.region])[0]
        region_label = args.region_label or region.app_label
    else:
        region = config.bbox_region(args.bbox)
        region_label = args.region_label or "chattanooga"
    bbox = region.bbox
    s, w, n, e = bbox
    proj = LocalMeters((s + n) / 2, (w + e) / 2)
    log.info("scanning %s (%.2f km^2) bbox=%s", region.name, bbox_area_km2(bbox), bbox)

    if not args.weights.exists():
        raise SystemExit(f"weights {args.weights} not found; run train first (or pass --weights)")

    # ---- known courts (OSM) to exclude
    known_paths = args.known
    if known_paths is None:
        known_paths = find_known_files(bbox)
        if not known_paths:
            from .fetch_osm import run_region
            pad = 0.002  # courts just outside the window still matter for the radius test
            fetch_region = config.Region(region.name, s - pad, w - pad, n + pad, e + pad, "custom")
            known_paths = [run_region(fetch_region, config.OSM_DIR, False, False, None, config.OVERPASS_MAX_TILE_DEG,
                                      config.OVERPASS_SLEEP_S, config.OVERPASS_TIMEOUT_S)]
    known_geoms, known_tree = load_known(known_paths, proj)
    log.info("%d known courts loaded from %s", len(known_geoms), ", ".join(str(p) for p in known_paths))

    # ---- model
    import torch

    config.init_ultralytics()
    from ultralytics import YOLO

    device = args.device or ("0" if torch.cuda.is_available() else "cpu")
    model = YOLO(str(args.weights))
    log.info("model %s on %s", args.weights, device)

    # ---- blocks
    naip_index = NaipIndex.for_bbox(bbox)
    block_m = args.block * args.gsd
    step_m = (args.block - args.overlap) * args.gsd
    centers = block_centers(bbox, proj, block_m, step_m)
    if args.max_blocks and len(centers) > args.max_blocks:
        raise SystemExit(f"{len(centers)} blocks > --max-blocks {args.max_blocks}")
    stride = args.tile - args.overlap
    log.info("%d block(s) of %d px (%.0f m), tiles %d px stride %d", len(centers), args.block, block_m, args.tile, stride)

    def load(center: tuple[float, float]) -> Chip | None:
        lat, lon = center
        if not naip_index.candidates(lat, lon, year=args.year):
            return None
        try:
            return read_chip(lat, lon, args.block, index=naip_index, target_gsd=args.gsd, year=args.year)
        except Exception as exc:  # keep going on transient read errors
            log.warning("block %.5f,%.5f failed: %s", lat, lon, exc)
            return None

    raw: list[Detection] = []
    n_tiles = 0
    dates: set[str] = set()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for bi, chip in enumerate(prefetch(pool, load, centers, depth=2 * args.workers)):
            if chip is None:
                continue
            dates.add(chip.item.date)
            tiles = list(iter_tiles(chip, args.tile, stride, min_valid=0.5))
            n_tiles += len(tiles)
            to_ll = to_lonlat(chip.crs)
            bh, bw = chip.rgb.shape[:2]
            for k in range(0, len(tiles), args.batch):
                batch = tiles[k:k + args.batch]
                # Ultralytics treats numpy input as BGR (OpenCV order)
                imgs = [np.ascontiguousarray(t[2][..., ::-1]) for t in batch]
                results = model.predict(imgs, imgsz=args.tile, conf=args.conf, iou=args.iou, device=device, verbose=False)
                for (c_off, r_off, tile_img), res in zip(batch, results):
                    boxes = getattr(res, "boxes", None)
                    if boxes is None or len(boxes) == 0:
                        continue
                    th, tw = tile_img.shape[:2]
                    for (bx0, by0, bx1, by1), conf in zip(_to_numpy(boxes.xyxy), _to_numpy(boxes.conf)):
                        edge = (
                            (bx0 <= 2 and c_off > 0) or (by0 <= 2 and r_off > 0)
                            or (bx1 >= tw - 2 and c_off + tw < bw) or (by1 >= th - 2 and r_off + th < bh)
                        )
                        corners = [(c_off + bx0, r_off + by0), (c_off + bx1, r_off + by0),
                                   (c_off + bx1, r_off + by1), (c_off + bx0, r_off + by1)]
                        xs, ys = zip(*(chip.pixel_to_crs(c, r) for c, r in corners))
                        lons, lats = to_ll.transform(list(xs), list(ys))
                        mx, my = zip(*(proj.fwd(la, lo) for la, lo in zip(lats, lons)))
                        clat, clon = float(np.mean(lats)), float(np.mean(lons))
                        if not (s <= clat <= n and w <= clon <= e):
                            continue
                        raw.append(Detection(float(conf), clat, clon, min(mx), min(my), max(mx), max(my),
                                             chip.item.id, chip.item.date, bool(edge)))
            if (bi + 1) % 10 == 0 or bi + 1 == len(centers):
                log.info("  block %d/%d, %d tiles so far, %d raw detections", bi + 1, len(centers), n_tiles, len(raw))

    # ---- merge + filter
    sized = [d for d in raw if args.min_box_m <= min(d.size_m) and max(d.size_m) <= args.max_box_m]
    merged = merge_detections(sized, args.merge_iou, args.merge_contain)
    fresh = [d for d in merged if not near_known(d, known_tree, known_geoms, args.known_radius)]
    log.info("raw=%d  size-ok=%d  merged=%d  after dropping known-court matches (%.0f m)=%d",
             len(raw), len(sized), len(merged), args.known_radius, len(fresh))

    today = dt.date.today().isoformat()
    by_id: dict[str, dict[str, Any]] = {}
    for d in sorted(fresh, key=lambda d: d.conf, reverse=True):
        fid = stable_detection_id(d.lat, d.lon)
        if fid in by_id:
            continue  # same ~11 m cell as a stronger detection
        bw_m, bh_m = d.size_m
        lat0, lon0 = proj.inv(d.x0, d.y0)
        lat1, lon1 = proj.inv(d.x1, d.y1)
        by_id[fid] = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(d.lon, 6), round(d.lat, 6)]},
            "properties": {
                "id": fid,
                "confidence": round(d.conf, 4),
                "region": region_label,
                "box_m": [round(bw_m, 1), round(bh_m, 1)],
                "bbox_lonlat": [round(lon0, 6), round(lat0, 6), round(lon1, 6), round(lat1, 6)],
                "naip_item": d.item_id,
                "naip_date": d.date,
                "model": args.weights.name,
                "detected": today,
            },
        }
    feats = list(by_id.values())
    meta = {
        "region": region.name,
        "region_label": region_label,
        "bbox": list(bbox),
        "weights": str(args.weights),
        "conf": args.conf,
        "known_radius_m": args.known_radius,
        "naip_dates": sorted(dates),
        "blocks": len(centers),
        "tiles": n_tiles,
        "raw_detections": len(raw),
        "merged_detections": len(merged),
        "candidates": len(feats),
        "seconds": round(time.monotonic() - t_start, 1),
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "attribution": "Imagery: USDA NAIP (public domain). Known courts: (c) OpenStreetMap contributors, ODbL.",
    }
    write_json(args.out, feature_collection(feats, metadata=meta))
    log.info("wrote %d candidates to %s (%.1f min)", len(feats), args.out, meta["seconds"] / 60)

    if args.save_crops and feats:
        from PIL import Image, ImageDraw

        crops_dir = args.out.parent / (args.out.stem + "_crops")
        crops_dir.mkdir(parents=True, exist_ok=True)
        for f in feats[: args.save_crops]:
            p = f["properties"]
            lon, lat = f["geometry"]["coordinates"]
            bw_px, bh_px = p["box_m"][0] / args.gsd, p["box_m"][1] / args.gsd
            size = int(min(640, max(160, 1.5 * max(bw_px, bh_px))))  # box plus context
            chip = read_chip(lat, lon, size, index=naip_index, target_gsd=args.gsd)
            im = Image.fromarray(chip.rgb)
            dr = ImageDraw.Draw(im)
            c = size / 2
            dr.rectangle([c - bw_px / 2, c - bh_px / 2, c + bw_px / 2, c + bh_px / 2], outline=(255, 0, 255), width=2)
            im.save(crops_dir / f"{p['id']}_{p['confidence']:.2f}.png")
        log.info("review crops in %s", crops_dir)


if __name__ == "__main__":
    main()
