"""Masked statewide scan: find courts that are not in OSM across a whole state.

Only places where public courts usually are get scanned: the union of parks,
schools, colleges, places of worship, community centres, recreation grounds,
sports centres and apartment-complex landuse from ``context_us.parquet``,
polygons buffered ``--buffer-m`` (40 m) and point features ``--node-buffer-m``
(100 m), clipped to the state.

Three subcommands, so the network-bound part can run while the GPU trains:

``plan``    builds the mask and a tile plan. Every location is assigned to one
            NAIP item of the state's own flight (newest year first; overlaps
            go to the higher-priority item), tiles of 320 px (0.6 m) with
            96 px overlap are laid out in each item's native pixel grid, the
            tiles touching the mask are kept, and neighbouring tiles are
            grouped into blocks (one ranged read each).
``fetch``   reads every block once (COG opened once per item, thread pool,
            retries) and caches it as JPEG q95, matching the training chips.
``detect``  runs the detector over all tiles (batched, FP16 on GPU), maps
            boxes to lon/lat, merges duplicates across tiles/blocks/items
            (greedy NMS in metres), drops boxes outside 6-120 m and anything
            within ``--known-radius`` (40 m) of any OSM basketball feature, and
            writes candidates GeoJSON plus review crops of the top N.

Example
-------
    python -m hoop_pipeline.scan_state plan --state TN
    python -m hoop_pipeline.scan_state fetch --state TN --workers 48
    python -m hoop_pipeline.scan_state detect --state TN --weights weights/best.pt --crops 300
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from PIL import Image, ImageDraw
from pyproj import Transformer
from shapely import STRtree

from . import config
from .common import add_common_args, feature_collection, read_json, setup_logging, stable_detection_id, write_json
from .geoparquet import read_geoparquet
from .naip_index import GDAL_ENV_BULK, GroupedReader, NaipCatalog, _signed, plan_windows
from .osm_tags import MASK_KINDS
from .scan import Detection, merge_detections, prefetch, tile_offsets
from .states import States

log = logging.getLogger("scan_state")
ALBERS = "EPSG:5070"


def _tf(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


def _transform_geoms(geoms: Any, tr: Transformer) -> Any:
    return shapely.transform(geoms, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))


def plan_dir_for(state: str) -> Path:
    return config.SCAN_DIR / state.upper()


# --------------------------------------------------------------------------- plan
@dataclass
class MaskFeatures:
    geoms: np.ndarray     # Albers metres (points for node features)
    kinds: np.ndarray
    is_point: np.ndarray
    area_km2: np.ndarray


def load_mask_features(context: Path, state: str, kinds: list[str]) -> MaskFeatures:
    a, g = read_geoparquet(context, columns=["kind", "state", "area_m2", "geom_kind", "center_lon", "center_lat"],
                           filters=[("state", "=", state.upper()), ("kind", "in", kinds)])
    is_pt = a["geom_kind"] == "point"
    geoms = np.empty(len(is_pt), dtype=object)
    for i, (gg, ip, lo, la) in enumerate(zip(g, is_pt, a["center_lon"], a["center_lat"])):
        geoms[i] = shapely.Point(lo, la) if (gg is None or ip) else gg
    alb = _transform_geoms(geoms, _tf("EPSG:4326", ALBERS)) if len(geoms) else geoms
    return MaskFeatures(alb, a["kind"], is_pt, np.nan_to_num(a["area_m2"].astype(float)) / 1e6)


def build_mask(context: Path, state: str, states: States, kinds: list[str], buffer_m: float, node_buffer_m: float,
               max_park_km2: float) -> tuple[Any, dict[str, Any]]:
    """Mask (Multi)Polygon in Albers metres plus stats."""
    mf = load_mask_features(context, state, kinds)
    big_park = (mf.kinds == "park") & (mf.area_km2 > max_park_km2) if max_park_km2 > 0 else np.zeros(len(mf.kinds), bool)
    stats: dict[str, Any] = {
        "features_by_kind": dict(Counter(mf.kinds.tolist())),
        "points_by_kind": dict(Counter(mf.kinds[mf.is_point].tolist())),
        "largest_parks_km2": sorted((round(float(v), 1) for v in mf.area_km2[mf.kinds == "park"]), reverse=True)[:10],
        "big_parks_excluded": int(big_park.sum()),
        "big_parks_excluded_km2": round(float(mf.area_km2[big_park].sum()), 1),
    }
    keep = ~big_park
    buffers = np.where(mf.is_point[keep], node_buffer_m, buffer_m)
    buffered = shapely.buffer(mf.geoms[keep], buffers, quad_segs=4)
    t0 = time.monotonic()
    st_alb = _transform_geoms(states.geometry(state), _tf("EPSG:4326", ALBERS))
    kinds_k = mf.kinds[keep]
    per_kind: dict[str, Any] = {}
    unions = []
    for kd in sorted(set(kinds_k.tolist())):
        u = shapely.union_all(buffered[kinds_k == kd])
        unions.append(u)
        per_kind[kd] = round(shapely.intersection(u, st_alb).area / 1e6, 1)
    mask = shapely.intersection(shapely.union_all(unions), st_alb)
    stats["km2_by_kind"] = per_kind  # each kind on its own (they overlap)
    stats["mask_km2"] = round(mask.area / 1e6, 1)
    stats["state_km2"] = round(st_alb.area / 1e6, 1)
    stats["mask_fraction"] = round(mask.area / st_alb.area, 4)
    stats["union_seconds"] = round(time.monotonic() - t0, 1)
    return mask, stats


def item_zones(cat: NaipCatalog, state: str, min_year: int) -> list[tuple[int, Any]]:
    """[(catalog index, zone polygon in Albers)] for the state's own flights, newest first;
    a location covered by several items belongs to the highest-priority one."""
    idx = cat.items_for_state(state, min_year)
    order = idx[np.lexsort((cat.ids[idx].astype(str), -cat.dates[idx].astype("datetime64[s]").astype(np.int64),
                            -cat.years[idx].astype(np.int64)))]
    to_alb = _tf("EPSG:4326", ALBERS)
    fps = _transform_geoms(cat.geoms[order], to_alb)
    tree = STRtree(fps)
    zones: list[tuple[int, Any]] = []
    for k, it in enumerate(order):
        higher = [j for j in tree.query(fps[k], predicate="intersects") if j < k]
        zone = fps[k] if not higher else shapely.difference(fps[k], shapely.union_all(fps[higher]))
        if not zone.is_empty and zone.area > 1.0:
            zones.append((int(it), zone))
    return zones


def tile_grid(cat: NaipCatalog, it: int, tile: int, overlap: int, gsd: float) -> tuple[np.ndarray, np.ndarray, int, int]:
    """Tile origins (native px) and native tile/stride sizes for one item."""
    native = abs(float(cat.transform[it, 0]))
    f = gsd / native
    nt = int(round(tile * f))
    ns = int(round((tile - overlap) * f))
    w, h = int(cat.width[it]), int(cat.height[it])
    cols = np.arange(0, max(1, w - nt) + 1, ns)
    rows = np.arange(0, max(1, h - nt) + 1, ns)
    if cols[-1] + nt < w:
        cols = np.r_[cols, w - nt]
    if rows[-1] + nt < h:
        rows = np.r_[rows, h - nt]
    return cols, rows, nt, ns


def cmd_plan(args: argparse.Namespace) -> None:
    t0 = time.monotonic()
    out = args.plan_dir or plan_dir_for(args.state)
    out.mkdir(parents=True, exist_ok=True)
    states = States.load(args.states_file)
    mask, stats = build_mask(args.context, args.state, states, args.kinds, args.buffer_m, args.node_buffer_m,
                             args.max_park_km2)
    log.info("mask %s km^2 (%.2f%% of %s km^2) from %s", stats["mask_km2"], 100 * stats["mask_fraction"],
             stats["state_km2"], stats["features_by_kind"])
    to_ll = _tf(ALBERS, "EPSG:4326")
    mask_ll = _transform_geoms(shapely.simplify(mask, 5.0), to_ll)
    write_json(out / "mask.geojson", feature_collection(
        [{"type": "Feature", "geometry": json.loads(shapely.to_geojson(mask_ll)), "properties": stats}]), indent=None)

    cat = NaipCatalog.load(args.naip_dir, min_year=args.min_year)
    zones = item_zones(cat, args.state, args.min_year)
    log.info("%d %s NAIP items own part of the state", len(zones), args.state)
    # The mask is one huge MultiPolygon; index its parts so each item only touches nearby pieces.
    parts = shapely.get_parts(mask)
    ptree = STRtree(parts)
    blocks: list[dict[str, Any]] = []
    n_tiles = 0
    years: Counter[int] = Counter()
    transformers: dict[int, Transformer] = {}
    for it, zone in zones:
        near = ptree.query(zone, predicate="intersects")
        if not len(near):
            continue
        pieces = shapely.intersection(parts[near], zone)
        pieces = pieces[~shapely.is_empty(pieces)]
        if not len(pieces):
            continue
        code = int(cat.epsg[it])
        if code not in transformers:
            transformers[code] = _tf(ALBERS, f"EPSG:{code}")
        pieces_n = _transform_geoms(pieces, transformers[code])
        cols, rows, nt, ns = tile_grid(cat, it, args.tile, args.overlap, args.gsd)
        a, _, c, _, e, f = cat.transform[it]
        cc, rr = np.meshgrid(cols, rows)
        cc, rr = cc.ravel(), rr.ravel()
        boxes = shapely.box(c + cc * a, f + (rr + nt) * e, c + (cc + nt) * a, f + rr * e)
        bi, _ = STRtree(pieces_n).query(boxes, predicate="intersects")
        hit = np.zeros(len(boxes), dtype=bool)
        hit[bi] = True
        if not hit.any():
            continue
        cc, rr = cc[hit], rr[hit]
        ci = np.searchsorted(cols, cc)
        ri = np.searchsorted(rows, rr)
        bkey = (ri // args.block_tiles) * 100000 + (ci // args.block_tiles)
        scale = args.gsd / abs(float(a))  # native px per output px
        for key in np.unique(bkey):
            m = bkey == key
            c0, r0 = int(cc[m].min()), int(rr[m].min())
            c1, r1 = int(cc[m].max()) + nt, int(rr[m].max()) + nt
            ow, oh = int(round((c1 - c0) / scale)), int(round((r1 - r0) / scale))
            tiles = [[int(round((x - c0) / scale)), int(round((y - r0) / scale))] for x, y in zip(cc[m], rr[m])]
            blocks.append({
                "id": f"{cat.ids[it]}_{int(key)}", "item": int(it), "item_id": str(cat.ids[it]),
                "col_off": c0, "row_off": r0, "width": c1 - c0, "height": r1 - r0, "out_w": ow, "out_h": oh,
                "transform": [a * scale, 0.0, c + c0 * a, 0.0, e * scale, f + r0 * e],
                "epsg": int(cat.epsg[it]), "date": cat.date_str(it), "year": int(cat.years[it]), "tiles": tiles,
            })
            n_tiles += len(tiles)
        years[int(cat.years[it])] += int(hit.sum())
    stats.update({
        "state": args.state.upper(), "items": len({b["item"] for b in blocks}), "blocks": len(blocks), "tiles": n_tiles,
        "tile_px": args.tile, "overlap_px": args.overlap, "gsd": args.gsd, "buffer_m": args.buffer_m,
        "node_buffer_m": args.node_buffer_m, "naip_years_tiles": dict(years),
        "est_scan_km2": round(n_tiles * ((args.tile - args.overlap) * args.gsd) ** 2 / 1e6, 1),
        "plan_seconds": round(time.monotonic() - t0, 1),
    })
    write_json(out / "plan.json", {"stats": stats, "blocks": blocks}, indent=None)
    write_json(out / "plan_stats.json", stats)
    log.info("plan: %d blocks, %d tiles from %d items (NAIP years %s); %.1f min", len(blocks), n_tiles,
             stats["items"], dict(years), stats["plan_seconds"] / 60)


# --------------------------------------------------------------------------- fetch
def _read_block(src: Any, b: dict[str, Any]) -> np.ndarray:
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    w, h, ow, oh = b["width"], b["height"], b["out_w"], b["out_h"]
    resampling = Resampling.nearest if (w == ow and h == oh) else (Resampling.average if w > ow else Resampling.bilinear)
    W, H = src.width, src.height
    c0, r0 = b["col_off"], b["row_off"]
    if c0 >= 0 and r0 >= 0 and c0 + w <= W and r0 + h <= H:
        data = src.read([1, 2, 3], window=Window(c0, r0, w, h), out_shape=(3, oh, ow), resampling=resampling)
    else:  # clipped at the raster edge: read the inside part, pad with 0
        data = np.zeros((3, oh, ow), dtype=np.uint8)
        cc0, rr0, cc1, rr1 = max(0, c0), max(0, r0), min(W, c0 + w), min(H, r0 + h)
        if cc1 > cc0 and rr1 > rr0:
            sx, sy = ow / w, oh / h
            oc0, or0 = int(round((cc0 - c0) * sx)), int(round((rr0 - r0) * sy))
            oc1, or1 = int(round((cc1 - c0) * sx)), int(round((rr1 - r0) * sy))
            if oc1 > oc0 and or1 > or0:
                data[:, or0:or1, oc0:oc1] = src.read([1, 2, 3], window=Window(cc0, rr0, cc1 - cc0, rr1 - rr0),
                                                     out_shape=(3, or1 - or0, oc1 - oc0), resampling=resampling)
    return np.ascontiguousarray(np.transpose(data, (1, 2, 0)))


def cmd_fetch(args: argparse.Namespace) -> None:
    import rasterio

    out = args.plan_dir or plan_dir_for(args.state)
    plan = read_json(out / "plan.json")
    blocks: list[dict[str, Any]] = plan["blocks"]
    bdir = out / "blocks"
    bdir.mkdir(exist_ok=True)
    cat = NaipCatalog.load(args.naip_dir, min_year=args.min_year)
    todo = [b for b in blocks if not (bdir / f"{b['id']}.jpg").exists()]
    by_item: dict[int, list[dict[str, Any]]] = {}
    for b in todo:
        by_item.setdefault(b["item"], []).append(b)
    tasks = []
    for it, bl in by_item.items():
        bl.sort(key=lambda b: (b["row_off"], b["col_off"]))
        for k in range(0, len(bl), 24):
            tasks.append((it, bl[k:k + 24]))
    log.info("fetching %d of %d blocks from %d items (%d tasks, %d threads)", len(todo), len(blocks), len(by_item),
             len(tasks), args.workers)
    t0 = time.monotonic()
    lock = threading.Lock()
    counts: Counter[str] = Counter()

    def run(task: tuple[int, list[dict[str, Any]]]) -> None:
        it, bl = task
        with rasterio.Env(**GDAL_ENV_BULK):
            src = None
            for b in bl:
                for attempt in range(6):
                    try:
                        if src is None:
                            src = rasterio.open(_signed(str(cat.hrefs[it])))
                        img = _read_block(src, b)
                        tmp = bdir / f"{b['id']}.tmp.jpg"
                        Image.fromarray(img).save(tmp, quality=args.jpeg_quality)
                        tmp.replace(bdir / f"{b['id']}.jpg")
                        with lock:
                            counts["ok"] += 1
                        break
                    except Exception as exc:
                        with lock:
                            counts["retries"] += 1
                        if src is not None:
                            src.close()
                            src = None
                        if attempt == 5:
                            log.warning("block %s failed: %s", b["id"], exc)
                            with lock:
                                counts["failed"] += 1
                        else:
                            time.sleep(min(60, 2 ** attempt) * (0.5 + np.random.random()))
            if src is not None:
                src.close()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(run, t) for t in tasks]
        for k, fut in enumerate(as_completed(futs)):
            fut.result()
            if (k + 1) % max(1, len(futs) // 50) == 0:
                el = time.monotonic() - t0
                log.info("  %d/%d tasks, %s, %.1f blocks/s", k + 1, len(futs), dict(counts), counts["ok"] / max(el, 1e-6))
    el = time.monotonic() - t0
    size = sum(p.stat().st_size for p in bdir.glob("*.jpg"))
    write_json(out / "fetch_stats.json", {"blocks": len(blocks), "fetched": counts["ok"], "failed": counts["failed"],
                                          "retries": counts["retries"], "seconds": round(el, 1),
                                          "cache_gb": round(size / 1e9, 2)})
    log.info("fetch done: %s in %.1f min, cache %.2f GB", dict(counts), el / 60, size / 1e9)


# --------------------------------------------------------------------------- detect
@dataclass
class TileRef:
    block: int
    col: int
    row: int


def iter_tile_batches(blocks: list[dict[str, Any]], bdir: Path, tile: int, batch: int, threads: int,
                      min_valid: float, retile: int = 0, overlap: int = config.SCAN_OVERLAP_PX,
                      ) -> Iterator[tuple[list[TileRef], list[np.ndarray], int]]:
    """Decode cached blocks in background threads (bounded prefetch) and yield
    (refs, BGR tiles, n_missing_blocks). With ``retile`` > 0 each block is cut into
    ``retile``-px tiles on its own grid (padded with no-data if smaller) instead of
    the planned ``tile``-px tiles."""

    def load(bi: int) -> tuple[int, np.ndarray | None]:
        p = bdir / f"{blocks[bi]['id']}.jpg"
        if not p.exists():
            return bi, None
        with Image.open(p) as im:
            return bi, np.asarray(im.convert("RGB"))

    refs: list[TileRef] = []
    imgs: list[np.ndarray] = []
    missing = 0
    pool = ThreadPoolExecutor(max_workers=threads)
    for bi, arr in prefetch(pool, load, list(range(len(blocks))), depth=threads * 3):
        if arr is None:
            missing += 1
            continue
        offsets = blocks[bi]["tiles"]
        if retile:
            tile = retile
            h, w = arr.shape[:2]
            if h < tile or w < tile:
                arr = np.pad(arr, ((0, max(0, tile - h)), (0, max(0, tile - w)), (0, 0)))
            offsets = [[c, r] for r in tile_offsets(arr.shape[0], tile, tile - overlap)
                       for c in tile_offsets(arr.shape[1], tile, tile - overlap)]
        valid = arr.max(axis=2) > 0
        for cx, ry in offsets:
            t = arr[ry:ry + tile, cx:cx + tile]
            if t.shape[0] != tile or t.shape[1] != tile or valid[ry:ry + tile, cx:cx + tile].mean() < min_valid:
                continue
            refs.append(TileRef(bi, cx, ry))
            imgs.append(np.ascontiguousarray(t[..., ::-1]))  # Ultralytics wants BGR numpy
            if len(imgs) >= batch:
                yield refs, imgs, missing
                refs, imgs = [], []
    pool.shutdown()
    if imgs:
        yield refs, imgs, missing
    else:
        yield [], [], missing


def block_footprints_ll(blocks: list[dict[str, Any]]) -> np.ndarray:
    """Lon/lat polygons of the area each cached block covers."""
    out = np.empty(len(blocks), dtype=object)
    by_epsg: dict[int, list[int]] = {}
    for i, b in enumerate(blocks):
        by_epsg.setdefault(int(b["epsg"]), []).append(i)
    for code, idx in by_epsg.items():
        tr = _tf(f"EPSG:{code}", "EPSG:4326")
        xs, ys = [], []
        for i in idx:
            A, _, C, _, E, F = blocks[i]["transform"]
            x1, y1 = C + blocks[i]["out_w"] * A, F + blocks[i]["out_h"] * E
            xs += [C, x1, x1, C]
            ys += [F, F, y1, y1]
        lon, lat = tr.transform(np.array(xs), np.array(ys))
        rings = np.stack([lon, lat], axis=1).reshape(len(idx), 4, 2)
        out[idx] = shapely.polygons(rings)
    return out


def known_court_recall(courts: Path, state: str, blocks: list[dict[str, Any]], merged: list[Detection],
                       thresholds: tuple[float, ...] = (0.1, 0.25, 0.4, 0.5, 0.6, 0.7)) -> dict[str, Any]:
    """End-to-end check on the held-out state: of the labelable OSM courts that lie inside the
    scanned blocks, how many does the scan find (before the known-court filter removes them)?"""
    a, g = read_geoparquet(courts, columns=["state", "label_ok", "center_lon", "center_lat"])
    sel = (a["state"] == state.upper()) & a["label_ok"].astype(bool)
    fps = block_footprints_ll(blocks)
    ftree = STRtree(fps)
    pts = shapely.points(a["center_lon"][sel], a["center_lat"][sel])
    inside, _ = ftree.query(pts, predicate="within")
    scanned = np.unique(inside)
    court_alb = _transform_geoms(g[sel][scanned], _tf("EPSG:4326", ALBERS))
    res: dict[str, Any] = {"labelable_courts_in_state": int(sel.sum()), "inside_scanned_blocks": int(len(scanned))}
    if not len(scanned) or not merged:
        return res
    for t in thresholds:
        dets = [d for d in merged if d.conf >= t]
        if not dets:
            res[f"{t:.2f}"] = {"recall": 0.0, "detections": 0}
            continue
        dtree = STRtree([d.geom for d in dets])
        hit_c, hit_d = dtree.query(shapely.buffer(court_alb, 5.0), predicate="intersects")
        res[f"{t:.2f}"] = {"recall": round(len(np.unique(hit_c)) / len(scanned), 4), "detections": len(dets),
                           "detections_on_known_courts": int(len(np.unique(hit_d)))}
    return res


def load_known_albers(courts: Path, bounds_ll: tuple[float, float, float, float]) -> tuple[list[Any], STRtree | None]:
    a, g = read_geoparquet(courts, columns=["center_lon", "center_lat"])
    w, s, e, n = bounds_ll
    sel = (a["center_lon"] >= w) & (a["center_lon"] <= e) & (a["center_lat"] >= s) & (a["center_lat"] <= n)
    alb = list(_transform_geoms(g[sel], _tf("EPSG:4326", ALBERS)))
    return alb, (STRtree(alb) if alb else None)


def near_known_albers(d: Detection, tree: STRtree | None, geoms: list[Any], radius_m: float) -> bool:
    if tree is None:
        return False
    center = shapely.Point((d.x0 + d.x1) / 2, (d.y0 + d.y1) / 2)
    for j in tree.query(d.geom.buffer(radius_m), predicate="intersects"):
        if geoms[j].distance(center) <= radius_m or geoms[j].intersects(d.geom):
            return True
    return False


def fp16_kwargs(gpu: bool) -> dict[str, Any]:
    """FP16 inference flag for this Ultralytics version (8.4 renamed half= to quantize=16)."""
    if not gpu:
        return {}
    from ultralytics.cfg import DEFAULT_CFG_DICT

    return {"quantize": 16} if "quantize" in DEFAULT_CFG_DICT else {"half": True}


def region_label_for(lat: float, lon: float, default: str, sub_regions: list[config.Region]) -> str:
    for r in sub_regions:
        if r.south <= lat <= r.north and r.west <= lon <= r.east:
            return r.app_label
    return default


def cmd_detect(args: argparse.Namespace) -> None:
    import torch

    config.init_ultralytics()
    from ultralytics import YOLO

    t_start = time.monotonic()
    out_dir = args.plan_dir or plan_dir_for(args.state)
    plan = read_json(out_dir / "plan.json")
    blocks: list[dict[str, Any]] = plan["blocks"]
    bdir = out_dir / "blocks"
    device = args.device or ("0" if torch.cuda.is_available() else "cpu")
    model = YOLO(str(args.weights))
    precision = fp16_kwargs(device != "cpu")
    tile = args.retile or args.tile
    log.info("detect: %d blocks, %d planned tiles, model %s on %s, %d px tiles%s", len(blocks), plan["stats"]["tiles"],
             args.weights, device, tile, " (re-tiled)" if args.retile else "")

    to_alb: dict[int, Transformer] = {}
    to_ll: dict[int, Transformer] = {}
    raw: list[Detection] = []
    n_tiles = 0
    missing = 0
    t0 = time.monotonic()
    for refs, imgs, miss in iter_tile_batches(blocks, bdir, args.tile, args.batch, args.io_threads, 0.5,
                                              retile=args.retile, overlap=args.overlap):
        missing = miss
        if not imgs:
            continue
        n_tiles += len(imgs)
        results = model.predict(imgs, imgsz=tile, conf=args.conf, iou=args.iou, device=device, verbose=False,
                                batch=len(imgs), **precision)
        for ref, res in zip(refs, results):
            boxes = getattr(res, "boxes", None)
            if boxes is None or len(boxes) == 0:
                continue
            b = blocks[ref.block]
            epsg = b["epsg"]
            if epsg not in to_alb:
                to_alb[epsg] = _tf(f"EPSG:{epsg}", ALBERS)
                to_ll[epsg] = _tf(f"EPSG:{epsg}", "EPSG:4326")
            A, _, C, _, E, F = b["transform"]
            xy = boxes.xyxy.cpu().numpy().astype(float)
            confs = boxes.conf.cpu().numpy().astype(float)
            bw, bh = b["out_w"], b["out_h"]
            for (x0, y0, x1, y1), cf in zip(xy, confs):
                edge = ((x0 <= 2 and ref.col > 0) or (y0 <= 2 and ref.row > 0)
                        or (x1 >= tile - 2 and ref.col + tile < bw)
                        or (y1 >= tile - 2 and ref.row + tile < bh))
                px = np.array([ref.col + x0, ref.col + x1, ref.col + x1, ref.col + x0])
                py = np.array([ref.row + y0, ref.row + y0, ref.row + y1, ref.row + y1])
                X, Y = C + px * A, F + py * E
                ax, ay = to_alb[epsg].transform(X, Y)
                lon, lat = to_ll[epsg].transform(X.mean(), Y.mean())
                raw.append(Detection(float(cf), float(lat), float(lon), float(ax.min()), float(ay.min()),
                                     float(ax.max()), float(ay.max()), b["item_id"], b["date"], bool(edge)))
        if n_tiles % (args.batch * 50) < len(imgs):
            el = time.monotonic() - t0
            log.info("  %d tiles (%.0f/s), %d raw detections", n_tiles, n_tiles / max(el, 1e-6), len(raw))
    infer_s = time.monotonic() - t0
    if missing:
        log.warning("%d blocks were missing from the cache (fetch incomplete?)", missing)

    sized = [d for d in raw if args.min_box_m <= min(d.size_m) and max(d.size_m) <= args.max_box_m]
    merged = merge_detections(sized, args.merge_iou, args.merge_contain)
    lats = np.array([d.lat for d in merged]) if merged else np.zeros(0)
    lons = np.array([d.lon for d in merged]) if merged else np.zeros(0)
    pad = 0.02
    bounds = (float(lons.min()) - pad, float(lats.min()) - pad, float(lons.max()) + pad, float(lats.max()) + pad) \
        if len(lons) else (0.0, 0.0, 0.0, 0.0)
    known_geoms, known_tree = load_known_albers(args.courts, bounds)
    fresh = [d for d in merged if not near_known_albers(d, known_tree, known_geoms, args.known_radius)]
    log.info("raw=%d size-ok=%d merged=%d not-near-known(%.0f m, %d known)=%d", len(raw), len(sized), len(merged),
             args.known_radius, len(known_geoms), len(fresh))
    recall = known_court_recall(args.courts, args.state, blocks, merged)
    log.info("known-court recall inside scanned blocks: %s", recall)

    subs = config.resolve_regions(args.sub_region) if args.sub_region else []
    today = dt.date.today().isoformat()
    feats: dict[str, dict[str, Any]] = {}
    for d in sorted(fresh, key=lambda d: d.conf, reverse=True):
        fid = stable_detection_id(d.lat, d.lon)
        if fid in feats:
            continue
        bw_m, bh_m = d.size_m
        feats[fid] = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(d.lon, 6), round(d.lat, 6)]},
            "properties": {
                "id": fid,
                "confidence": round(d.conf, 4),
                "region": region_label_for(d.lat, d.lon, args.region_label, subs),
                "state": args.state.upper(),
                "box_m": [round(bw_m, 1), round(bh_m, 1)],
                "mask_kinds": [],
                "on_site": None,
                "naip_item": d.item_id,
                "naip_date": d.date,
                "model": Path(args.weights).name,
                "detected": today,
            },
        }
    features = list(feats.values())
    on_site = annotate_on_site(features, args.context, args.state) if args.tag_kinds else {}
    meta = {
        "state": args.state.upper(), "weights": str(args.weights), "conf": args.conf, "known_radius_m": args.known_radius,
        "plan": plan["stats"], "tile_px": tile, "tiles_scanned": n_tiles, "blocks_missing": missing,
        "raw_detections": len(raw), "known_court_recall": recall, "on_site": on_site,
        "merged_detections": len(merged), "candidates": len(features),
        "candidates_by_threshold": {f"{t:.2f}": sum(1 for f in features if f["properties"]["confidence"] >= t)
                                    for t in (0.1, 0.25, 0.4, 0.5, 0.6, 0.7, 0.8)},
        "inference_seconds": round(infer_s, 1), "tiles_per_s": round(n_tiles / max(infer_s, 1e-6), 1),
        "seconds": round(time.monotonic() - t_start, 1),
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "attribution": "Imagery: USDA NAIP (public domain). Known courts: (c) OpenStreetMap contributors, ODbL.",
    }
    write_json(args.out, feature_collection(features, metadata=meta), indent=None)
    log.info("wrote %d candidates to %s; by threshold %s", len(features), args.out, meta["candidates_by_threshold"])
    if args.crops and features:
        save_crops(features[: args.crops], args.out.parent / (args.out.stem + "_crops"), args)


def annotate_on_site(features: list[dict[str, Any]], context: Path, state: str, poly_m: float = 15.0,
                     point_m: float = 75.0) -> dict[str, int]:
    """Set ``mask_kinds`` and ``on_site`` on candidate features (in place).

    A candidate is *on site* when its centre lies inside (or within ``poly_m`` of) a park, school,
    college, place of worship, community centre, recreation ground, sports centre or apartment
    polygon, or within ``point_m`` of such a feature mapped only as a point. The scan mask is
    wider (a 40 m buffer, 100 m around points), so off-site hits are often on neighbouring
    residential lots, where backyard courts are private and must not be shown.
    """
    mf = load_mask_features(context, state, list(MASK_KINDS))
    tree = STRtree(mf.geoms) if len(mf.geoms) else None
    to_alb = _tf("EPSG:4326", ALBERS)
    counts: Counter[str] = Counter()
    for f in features:
        lon, lat = f["geometry"]["coordinates"]
        x, y = to_alb.transform(lon, lat)
        c = shapely.Point(x, y)
        kinds: set[str] = set()
        if tree is not None:
            for j in tree.query(c, predicate="dwithin", distance=max(poly_m, point_m)):
                limit = point_m if mf.is_point[j] else poly_m
                if mf.geoms[j].distance(c) <= limit:
                    kinds.add(str(mf.kinds[j]))
        f["properties"]["mask_kinds"] = sorted(kinds)
        f["properties"]["on_site"] = bool(kinds)
        counts["on_site" if kinds else "off_site"] += 1
    return dict(counts)


def cmd_annotate(args: argparse.Namespace) -> None:
    fc = read_json(args.candidates)
    counts = annotate_on_site(fc["features"], args.context, args.state, args.poly_m, args.point_m)
    fc.setdefault("metadata", {})["on_site_rule"] = {"poly_m": args.poly_m, "point_m": args.point_m, **counts}
    write_json(args.candidates, fc, indent=None)
    by_t = {f"{t:.2f}": sum(1 for f in fc["features"] if f["properties"]["confidence"] >= t and f["properties"]["on_site"])
            for t in (0.25, 0.4, 0.5, 0.6, 0.65, 0.7)}
    log.info("annotated %s: %s; on-site candidates by threshold %s", args.candidates, counts, by_t)


def save_crops(features: list[dict[str, Any]], crops_dir: Path, args: argparse.Namespace) -> None:
    """256 px review crops (box drawn) read straight from NAIP, plus contact sheets."""
    crops_dir.mkdir(parents=True, exist_ok=True)
    cat = NaipCatalog.load(args.naip_dir, min_year=args.min_year)
    lons = np.array([f["geometry"]["coordinates"][0] for f in features])
    lats = np.array([f["geometry"]["coordinates"][1] for f in features])
    item = cat.select(lons, lats, np.array([args.state.upper()] * len(features)), half_m=args.crop_px * args.gsd / 2)
    wp = plan_windows(cat, item, lons, lats, args.crop_px, args.gsd)
    paths: dict[int, Path] = {}

    def handle(key: Any, img: np.ndarray | None, err: str) -> int:
        i = int(key)
        if img is None:
            return i
        p = features[i]["properties"]
        im = Image.fromarray(img)
        dr = ImageDraw.Draw(im)
        c = args.crop_px / 2
        bw, bh = p["box_m"][0] / args.gsd, p["box_m"][1] / args.gsd
        dr.rectangle([c - bw / 2 - 3, c - bh / 2 - 3, c + bw / 2 + 3, c + bh / 2 + 3], outline=(255, 0, 255), width=2)
        path = crops_dir / f"{i + 1:03d}_{p['id']}_{p['confidence']:.2f}.jpg"
        im.save(path, quality=92)
        paths[i] = path
        return i

    list(GroupedReader(cat, workers=16).run(wp, np.arange(len(features)), handle))
    from .build_dataset_us import montage

    order = sorted(paths)
    for k in range(0, len(order), 30):
        chunk = order[k:k + 30]
        ims = [Image.open(paths[i]) for i in chunk]
        caps = [f"#{i + 1} {features[i]['properties']['confidence']:.2f} {','.join(features[i]['properties']['mask_kinds'])}"
                for i in chunk]
        montage(ims, 6, crops_dir.parent / f"{crops_dir.name}_sheet_{k // 30:02d}.jpg", tile=256, captions=caps)
    log.info("wrote %d crops to %s", len(paths), crops_dir)


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--state", default="TN")
        p.add_argument("--plan-dir", type=Path, help="default data/scan/<STATE>")
        p.add_argument("--naip-dir", type=Path, default=config.NAIP_GEOPARQUET_DIR)
        p.add_argument("--min-year", type=int, default=config.NAIP_MIN_YEAR)
        p.add_argument("--context", type=Path, default=config.OSM_US_DIR / "context_us.parquet")
        p.add_argument("--courts", type=Path, default=config.OSM_US_DIR / "courts_us.parquet")
        p.add_argument("--states-file", type=Path, default=config.STATES_FILE)
        p.add_argument("--gsd", type=float, default=config.TARGET_GSD_M)
        p.add_argument("--tile", type=int, default=config.CHIP_SIZE_PX)
        p.add_argument("--buffer-m", type=float, default=40.0)
        add_common_args(p)

    p = sub.add_parser("plan", help="build the mask and tile/block plan")
    common(p)
    p.add_argument("--kinds", nargs="+", default=list(MASK_KINDS))
    p.add_argument("--node-buffer-m", type=float, default=100.0)
    p.add_argument("--max-park-km2", type=float, default=0.0, help="drop park polygons larger than this (0 = keep all)")
    p.add_argument("--overlap", type=int, default=config.SCAN_OVERLAP_PX)
    p.add_argument("--block-tiles", type=int, default=6, help="tiles per block side")

    p = sub.add_parser("fetch", help="download plan blocks to the JPEG cache")
    common(p)
    p.add_argument("--workers", type=int, default=48)
    p.add_argument("--jpeg-quality", type=int, default=95)

    p = sub.add_parser("detect", help="run the detector over cached blocks")
    common(p)
    p.add_argument("--weights", type=Path, default=config.DEFAULT_WEIGHTS)
    p.add_argument("--conf", type=float, default=0.05)
    p.add_argument("--iou", type=float, default=0.5)
    p.add_argument("--merge-iou", type=float, default=0.3)
    p.add_argument("--merge-contain", type=float, default=0.6)
    p.add_argument("--min-box-m", type=float, default=6.0)
    p.add_argument("--max-box-m", type=float, default=120.0)
    p.add_argument("--known-radius", type=float, default=config.KNOWN_COURT_RADIUS_M)
    p.add_argument("--device")
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--io-threads", type=int, default=12)
    p.add_argument("--retile", type=int, default=0, help="cut blocks into tiles of this size instead of the plan's")
    p.add_argument("--overlap", type=int, default=config.SCAN_OVERLAP_PX, help="tile overlap for --retile")
    p.add_argument("--region-label", default="tennessee")
    p.add_argument("--sub-region", nargs="*", default=["chattanooga"],
                   help="regions.json names whose bbox relabels candidates (e.g. chattanooga)")
    p.add_argument("--no-tag-kinds", dest="tag_kinds", action="store_false")
    p.add_argument("--out", type=Path, default=config.OUT_DIR / "candidates_tn.geojson")
    p.add_argument("--crops", type=int, default=300)
    p.add_argument("--crop-px", type=int, default=256)

    p = sub.add_parser("annotate", help="(re)compute mask_kinds / on_site on an existing candidates file")
    common(p)
    p.add_argument("candidates", type=Path)
    p.add_argument("--poly-m", type=float, default=15.0)
    p.add_argument("--point-m", type=float, default=75.0)

    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    {"plan": cmd_plan, "fetch": cmd_fetch, "detect": cmd_detect, "annotate": cmd_annotate}[args.cmd](args)


if __name__ == "__main__":
    main()
