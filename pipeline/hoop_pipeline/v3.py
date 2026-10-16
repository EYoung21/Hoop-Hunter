"""Detector v3: learn the courts OpenStreetMap misses. See README "v3 (2026-10-16)".

v1/v2 learned only from OSM courts, so courts OSM tends to miss (unmarked school blacktops, apartment courts,
faded park courts) were never positives and sometimes sat in the background of training chips as "nothing".
v3 adds official courts OSM lacks (snapped to a visible box), labels v2's most certain public detections in
every chip they appear in, drops chips that show a likely court nobody labelled, and is graded on truth
sources it never trained on.

Commands
--------
    split          held-out truth sources -> data/v3/split.json, data/v3/holdout_zones.geojson (no training chip
                   may touch these) and data/v3/heldout_area.geojson (what the held-out evaluation scans)
    snap           official courts OSM lacks, training sources only, snapped to a visible court box on NAIP with
                   v2 + the verifier -> data/v3/snapped.parquet, review sheets in out/v3/snap_review/
    layers         extra label layer for ``build_dataset_us --extra-courts``: snapped official boxes, v2 pseudo-labels,
                   "maybe" boxes (likely courts nobody labelled: chips showing one are dropped) and hard-negative
                   boxes, from the national scan -> data/v3/extra_courts.parquet
    heldout-items  NAIP items of the CONUS plan that cover the held-out evaluation area -> data/v3/heldout_items.txt
    heldout-eval   v1 / v2 / v3 detections over those items against the held-out truth -> out/v3/heldout_eval.json
    tn-eval        TN holdout chip metrics (evaluate.py outputs) side by side -> out/v3/tn_eval.json
    rescore-input  slim copy of out/national/candidates_us.parquet for ``rescore`` on a node
    grid           export rules side by side on held-out evidence (in-park share, DC / Ramsey precision, recall)
    merge-rescore  national candidates + the v3 rescore -> out/national/v3/candidates_us.parquet (privacy classes
                   updated where the context CNN scored a candidate for the first time)
    calibrate      per-state verifier thresholds from known OSM courts -> out/national/v3/verify_thresholds.json
    select         mark the candidates to export at an operating point (``export`` column)
    review         blind review sheets from fresh NAIP chips (any candidate, no rescore chips needed)

Licenses: the truth records are used as internal training labels only. Nothing here copies a truth record into an
app file; the exported candidates are detections.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import shapely
from pyproj import Transformer
from shapely import STRtree

from . import config
from .common import add_common_args, read_json, setup_logging, write_json
from .geoparquet import read_geoparquet, write_geoparquet

log = logging.getLogger("v3")

V3_DATA = config.DATA_DIR / "v3"
V3_OUT = config.OUT_DIR / "v3"
NAT_OUT = config.OUT_DIR / "national"
ALBERS = "EPSG:5070"
CHIP_PX = config.CHIP_SIZE_PX
GSD = config.TARGET_GSD_M

# Whole truth sources held out for testing (plus all of Tennessee, as before). DC and Ramsey are the only all-owner
# planimetric layers big enough to score school / apartment courts; Boston, Columbus and Nashville are on-court
# point layers of park departments. Everything else may give training labels.
HELDOUT_SOURCES = ("dc_planimetrics", "ramsey_county_planimetrics", "boston_bprd", "columbus_crpd",
                   "nashville_metro_parks")
HELDOUT_STATE = "TN"
# court polygons drawn on the court; the polygon's box is the label (after the checks in `snap`)
POLYGON_SOURCES = ("nyc_parks", "seattle_spr", "pittsburgh_courts", "cambridge_sports_areas", "sf_rpd", "san_jose_prns",
                   "denver_dpr", "college_station_parks", "tempe_courts", "raleigh_prcr", "dc_planimetrics",
                   "ramsey_county_planimetrics")
# README "Location precision": Raleigh polygons sometimes sit in forest or on empty lots. The v3 snap review found
# most of its 28 courts OSM lacks in forest, back yards or a construction site, so it gives no labels.
SKIP_SOURCES = ("raleigh_prcr",)

_TO_ALB = Transformer.from_crs("EPSG:4326", ALBERS, always_xy=True)
_FROM_ALB = Transformer.from_crs(ALBERS, "EPSG:4326", always_xy=True)


def to_albers(geoms: np.ndarray) -> np.ndarray:
    geoms = np.asarray(geoms, dtype=object)
    if not len(geoms):
        return geoms
    return shapely.transform(geoms, lambda xy: np.column_stack(_TO_ALB.transform(xy[:, 0], xy[:, 1])))


def from_albers(geoms: np.ndarray) -> np.ndarray:
    geoms = np.asarray(geoms, dtype=object)
    if not len(geoms):
        return geoms
    return shapely.transform(geoms, lambda xy: np.column_stack(_FROM_ALB.transform(xy[:, 0], xy[:, 1])))


def write_polygons_geojson(path: Path, geoms: list[Any], props: list[dict[str, Any]]) -> None:
    """Polygons (lon/lat) as GeoJSON; made valid first (reprojected unions can self-touch)."""
    feats = [{"type": "Feature", "properties": p, "geometry": json.loads(shapely.to_geojson(shapely.make_valid(g)))}
             for g, p in zip(geoms, props)]
    write_json(path, {"type": "FeatureCollection", "features": feats}, indent=None)


def load_polygons(path: Path) -> np.ndarray:
    """lon/lat polygons from a GeoJSON FeatureCollection or a GeoParquet file."""
    if path.suffix.lower() == ".parquet":
        _, g = read_geoparquet(path)
        return g
    fc = read_json(path)
    return np.asarray([shapely.from_geojson(json.dumps(f["geometry"])) for f in fc["features"]], dtype=object)


# =========================================================================== truth helpers
def visible_court_mask(cols: dict[str, np.ndarray]) -> np.ndarray:
    """Live official court records visible from above (what truth.evaluate scores)."""
    from .truth import HIDDEN, LIVE_STATUS

    return (np.isin(cols["status"], LIVE_STATUS) & (cols["feature"] == "court") & ~np.isin(cols["indoor"], HIDDEN)
            & (cols["tier"] == "official"))


def not_in_osm_ids() -> set[str]:
    from .truth import OSM_MATCH_FILE

    om = pq.read_table(OSM_MATCH_FILE, columns=["truth_id", "n_osm_within_r"]).to_pydict()
    return {t for t, k in zip(om["truth_id"], om["n_osm_within_r"]) if k == 0}


def new_positive_mask(cols: dict[str, np.ndarray], not_in_osm: set[str], min_area: float) -> np.ndarray:
    """Same rule as ``truth export-labels``: official on-court records (polygon or point) with no OSM basketball
    feature within 40 m, not indoor/covered, not marked non-public, at least ``min_area`` where an area is known."""
    area = cols["area_m2"].astype(float)
    m = (visible_court_mask(cols) & np.isin(cols["status"], ("active", "unknown"))
         & np.isin(cols["precision"], ("court_polygon", "court_point"))
         & np.array([p is not False for p in cols["public"]])
         & np.isin(cols["truth_id"], list(not_in_osm)))
    if min_area > 0:
        m &= ~(np.isfinite(area) & (area < min_area))
    return m


# =========================================================================== split
def cmd_split(args: argparse.Namespace) -> None:
    from .states import States
    from .truth import COURT_LEVEL, load_truth, truth_geoms_m

    cols, _ = load_truth()
    cc, cg = read_geoparquet(config.DATA_DIR / "truth" / "coverage.parquet")
    held = list(args.heldout)
    zones_m: list[Any] = []
    area_m: list[Any] = []
    props: list[dict[str, Any]] = []
    for src in held:
        cov = to_albers(cg[cc["source"] == src])
        rows = cols["source"] == src
        recs = truth_geoms_m({k: v[rows] for k, v in cols.items()})
        both = shapely.union_all(np.concatenate([cov, recs]))
        zones_m.append(shapely.buffer(both, args.buffer_m))
        area_m.append(shapely.union_all(np.concatenate([cov, shapely.buffer(recs, 50.0)])))
        props.append({"source": src, "coverage_polygons": int(len(cov)), "records": int(rows.sum())})
    states = States.load(config.STATES_FILE)
    tn = to_albers(np.array([states.geometry(HELDOUT_STATE)], dtype=object))[0]
    zones_ll = list(from_albers(np.asarray(zones_m, dtype=object)))
    write_polygons_geojson(args.out_dir / "holdout_zones.geojson", zones_ll,
                           [{**p, "buffer_m": args.buffer_m} for p in props])
    write_polygons_geojson(args.out_dir / "heldout_area.geojson", list(from_albers(np.asarray(area_m, dtype=object))),
                           props)
    # counts per side: training sources vs held-out sources, and how many rows sit inside the zones
    vis = visible_court_mask(cols) & np.isin(cols["precision"], COURT_LEVEL)
    nio = not_in_osm_ids()
    area = cols["area_m2"].astype(float)
    big = ~(np.isfinite(area) & (area < 150))
    in_nio = np.isin(cols["truth_id"], list(nio))
    zone_union = shapely.union_all(np.asarray(zones_m + [shapely.buffer(tn, 1000.0)], dtype=object))
    shapely.prepare(zone_union)
    pts = shapely.points(*_TO_ALB.transform(cols["lon"].astype(float), cols["lat"].astype(float)))
    in_zone = shapely.contains(zone_union, pts)
    per: dict[str, Any] = {}
    for src in sorted(set(cols["source"][vis].tolist())):
        m = vis & (cols["source"] == src)
        side = "test" if src in held else "train"
        per[src] = {"side": side, "precision": str(cols["precision"][m][0]), "court_level_rows": int(m.sum()),
                    "ge150": int((m & big).sum()), "not_in_osm": int((m & in_nio).sum()),
                    "not_in_osm_ge150": int((m & in_nio & big).sum()), "inside_holdout_zones": int((m & in_zone).sum())}
    tot: dict[str, dict[str, int]] = {}
    for side in ("train", "test"):
        keys = [k for k, v in per.items() if v["side"] == side]
        tot[side] = {f: sum(per[k][f] for k in keys) for f in ("court_level_rows", "ge150", "not_in_osm", "not_in_osm_ge150")}
        tot[side]["sources"] = len(keys)
    nps = new_positive_mask(cols, nio, 150.0)
    train_np = nps & ~np.isin(cols["source"], held) & (cols["state"] != HELDOUT_STATE) & ~in_zone
    res = {"heldout_sources": held, "heldout_state": HELDOUT_STATE, "zone_buffer_m": args.buffer_m,
           "rule": "no training chip may intersect holdout_zones.geojson (held-out coverage + records, buffered), "
                   "and Tennessee is held out entirely (val split, 250 m border buffer); held-out sources give no labels",
           "totals": tot, "sources": per,
           "new_positive_candidates_ge150": {"all": int(nps.sum()), "training": int(train_np.sum()),
                                             "training_by_source": dict(Counter(cols["source"][train_np].tolist()))}}
    write_json(args.out_dir / "split.json", res)
    log.info("split: %s", json.dumps({"totals": tot, "training_new_positives": int(train_np.sum())}))


# =========================================================================== chips with georeferencing
def fetch_chips(lons: np.ndarray, lats: np.ndarray, states: np.ndarray, px: int = CHIP_PX,
                workers: int = 32) -> tuple[np.ndarray, np.ndarray, Any, Any]:
    """NAIP chips centred on points (same item choice and grid as the dataset builder), plus their WindowPlan."""
    from .naip_index import GroupedReader, plan_windows
    from .privacy import naip_catalog

    cat = naip_catalog(config.NAIP_GEOPARQUET_DIR)
    item = cat.select(lons, lats, states, half_m=px * GSD / 2)
    wp = plan_windows(cat, item, lons, lats, px, GSD)
    out = np.zeros((len(lons), px, px, 3), np.uint8)
    ok = np.zeros(len(lons), bool)

    def handle(key: Any, img: np.ndarray | None, err: str) -> int:
        i = int(key)
        if img is not None and float((img.max(axis=2) > 0).mean()) >= 0.9:
            out[i] = img
            ok[i] = True
        return i

    list(GroupedReader(cat, workers=workers).run(wp, np.arange(len(lons)), handle))
    return out, ok, wp, cat


def geom_to_px(geom: Any, epsg: int, tf: np.ndarray) -> np.ndarray:
    """lon/lat geometry -> its (x0, y0, x1, y1) box in chip pixels (chip affine ``tf`` = a, b, c, d, e, f)."""
    tr = Transformer.from_crs("EPSG:4326", f"EPSG:{int(epsg)}", always_xy=True)
    xy = shapely.get_coordinates(geom)
    x, y = tr.transform(xy[:, 0], xy[:, 1])
    a, _, c, _, e, f = tf
    col = (np.asarray(x) - c) / a
    row = (np.asarray(y) - f) / e
    return np.array([col.min(), row.min(), col.max(), row.max()])


def px_box_to_lonlat(box: np.ndarray, epsg: int, tf: np.ndarray) -> Any:
    """Chip-pixel box -> lon/lat polygon of the rectangle in the chip's CRS (axis-aligned there)."""
    a, _, c, _, e, f = tf
    xs = c + np.array([box[0], box[2], box[2], box[0]]) * a
    ys = f + np.array([box[1], box[1], box[3], box[3]]) * e
    lo, la = Transformer.from_crs(f"EPSG:{int(epsg)}", "EPSG:4326", always_xy=True).transform(xs, ys)
    return shapely.Polygon(np.column_stack([lo, la]))


def box_iou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU of one box ``a`` (4,) with boxes ``b`` (K, 4)."""
    if not len(b):
        return np.zeros(0)
    ix = np.clip(np.minimum(b[:, 2], a[2]) - np.maximum(b[:, 0], a[0]), 0, None)
    iy = np.clip(np.minimum(b[:, 3], a[3]) - np.maximum(b[:, 1], a[1]), 0, None)
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1]) - inter
    return inter / np.maximum(ua, 1e-9)


def box_ioa(inner: np.ndarray, outer: np.ndarray) -> np.ndarray:
    """Share of each ``inner`` box (K, 4) inside ``outer`` (4,)."""
    if not len(inner):
        return np.zeros(0)
    ix = np.clip(np.minimum(inner[:, 2], outer[2]) - np.maximum(inner[:, 0], outer[0]), 0, None)
    iy = np.clip(np.minimum(inner[:, 3], outer[3]) - np.maximum(inner[:, 1], outer[1]), 0, None)
    return ix * iy / np.maximum((inner[:, 2] - inner[:, 0]) * (inner[:, 3] - inner[:, 1]), 1e-9)


def point_box_dist(px: float, py: float, b: np.ndarray) -> np.ndarray:
    dx = np.maximum(np.maximum(b[:, 0] - px, 0), px - b[:, 2])
    dy = np.maximum(np.maximum(b[:, 1] - py, 0), py - b[:, 3])
    return np.hypot(dx, dy)


# =========================================================================== snap
def decide_polygon(pb: np.ndarray, dets: np.ndarray, area_m2: float, courts: int | None,
                   a: argparse.Namespace) -> tuple[str, list[np.ndarray], str]:
    """Label decision for a court polygon: (decision, label boxes in px, reason).

    The polygon's box is the label, but only where v2 sees at least a faint court there (a box with IoU >=
    --poly-iou at conf >= --poly-conf): NYC and the other park layers list courts under highway ramps, elevated
    tracks and full canopy, and on construction sites, none of which is visible on NAIP. The verifier cannot make
    that call: on arbitrary boxes (forest, ramps) it is out of distribution and often scores > 0.95. A polygon over
    several courts (``courts`` > 1, or bigger than --multi-area m^2) that v2 boxes one by one becomes those boxes."""
    ious = box_iou(pb, dets[:, :4]) if len(dets) else np.zeros(0)
    agree = (ious >= a.poly_iou) & (dets[:, 4] >= a.poly_conf) if len(dets) else np.zeros(0, bool)
    if not agree.any():
        best = float(dets[ious >= a.poly_iou, 4].max()) if len(dets) and (ious >= a.poly_iou).any() else 0.0
        return "drop", [], f"no v2 box on the polygon (best overlapping {best:.2f})"
    multi = (courts is not None and courts > 1) or area_m2 > a.multi_area
    if multi:
        inside = (box_ioa(dets[:, :4], pb) >= 0.7) & (dets[:, 4] >= a.split_conf)
        sub = dets[inside]
        if len(sub) >= 2:
            # keep non-overlapping boxes, best first
            keep: list[np.ndarray] = []
            for d in sub[np.argsort(-sub[:, 4])]:
                if not keep or box_iou(d[:4], np.array([k[:4] for k in keep])).max() < 0.3:
                    keep.append(d)
            if len(keep) >= 2:
                return "split", [k[:4] for k in keep], f"multi-court polygon split into {len(keep)} v2 boxes"
    return "keep", [pb], f"polygon box; v2 agrees at {float(dets[agree, 4].max()):.2f}"


def decide_point(px: float, py: float, dets: np.ndarray, ver_best: float, a: argparse.Namespace,
                 ) -> tuple[str, list[np.ndarray], str]:
    """Label decision for an on-court point: the best v2 box within --snap-m of the point (centre distance, or the
    point inside / next to the box). Below --point-conf the box is kept only when the verifier on it is high."""
    if not len(dets):
        return "drop", [], "no v2 box near the point"
    d_centre = np.hypot((dets[:, 0] + dets[:, 2]) / 2 - px, (dets[:, 1] + dets[:, 3]) / 2 - py) * GSD
    d_edge = point_box_dist(px, py, dets[:, :4]) * GSD
    near = (d_centre <= a.snap_m) | (d_edge <= a.snap_edge_m)
    if not near.any():
        return "drop", [], f"no v2 box within {a.snap_m:.0f} m"
    k = int(np.flatnonzero(near)[np.argmax(dets[near, 4])])
    conf = float(dets[k, 4])
    if conf >= a.point_conf:
        return "keep", [dets[k, :4]], f"v2 box conf {conf:.2f}, {d_centre[k]:.0f} m from the point"
    if ver_best >= a.point_verify:  # off by default: the review found the verifier unreliable on weak boxes
        return "keep", [dets[k, :4]], f"weak v2 box {conf:.2f} but verifier {ver_best:.2f}"
    return "drop", [], f"best v2 box {conf:.2f}, verifier {ver_best:.2f}"


def cmd_snap(args: argparse.Namespace) -> None:
    import torch
    from PIL import Image, ImageDraw

    from .build_dataset_us import montage
    from .cropcls import CropClassifier
    from .fastdet import FastDetector
    from .truth import load_truth

    t0 = time.monotonic()
    cols, _ = load_truth()
    zones = load_polygons(args.zones)
    zone_union = shapely.union_all(zones)
    shapely.prepare(zone_union)
    m = new_positive_mask(cols, not_in_osm_ids(), args.min_area)
    m &= ~np.isin(cols["source"], list(args.heldout)) & (cols["state"] != HELDOUT_STATE)
    m &= ~shapely.intersects_xy(zone_union, cols["lon"].astype(float), cols["lat"].astype(float))
    if args.sources:
        m &= np.isin(cols["source"], args.sources)
    skipped = Counter(cols["source"][m & np.isin(cols["source"], list(args.skip_sources))].tolist())
    m &= ~np.isin(cols["source"], list(args.skip_sources))
    if args.exclude and args.exclude.exists():  # reviewed by eye: roofs over courts the source does not mark covered
        bad = {ln.split()[0] for ln in args.exclude.read_text(encoding="utf-8").splitlines() if ln.strip()
               and not ln.startswith("#")}
        skipped["excluded_by_review"] = int((m & np.isin(cols["truth_id"], list(bad))).sum())
        m &= ~np.isin(cols["truth_id"], list(bad))
    log.info("skipped sources (no labels): %s", dict(skipped))
    idx = np.flatnonzero(m)
    log.info("snap: %d official courts OSM lacks in training sources (%s)", len(idx),
             dict(Counter(cols["source"][idx].tolist())))
    fp = cols["footprint_wkb"]
    polys = [shapely.from_wkb(fp[i]) if fp[i] is not None else None for i in idx]
    # chips centred on the court (polygon centroid, else the record point)
    clon = np.array([p.centroid.x if p is not None else float(cols["lon"][i]) for p, i in zip(polys, idx)])
    clat = np.array([p.centroid.y if p is not None else float(cols["lat"][i]) for p, i in zip(polys, idx)])
    imgs, ok, wp, cat = fetch_chips(clon, clat, cols["state"][idx].astype(str), workers=args.workers)
    log.info("chips: %d of %d read (%.0f s)", int(ok.sum()), len(idx), time.monotonic() - t0)
    det = FastDetector(args.weights, imgsz=args.imgsz, device="cpu", half=False)
    ver = CropClassifier.load(args.verifier, device=torch.device("cpu"))
    rows: list[dict[str, Any]] = []
    out_geoms: list[Any] = []
    review: list[tuple[Image.Image, str, str]] = []
    for j, i in enumerate(idx):
        src = str(cols["source"][i])
        area = float(cols["area_m2"][i]) if cols["area_m2"][i] is not None else float("nan")
        courts_v = cols["courts"][i]
        courts = None if courts_v is None or (isinstance(courts_v, float) and math.isnan(courts_v)) else int(courts_v)
        base = {"truth_id": str(cols["truth_id"][i]), "source": src, "state": str(cols["state"][i]),
                "precision": str(cols["precision"][i]), "area_m2": area, "courts": courts}
        if not ok[j]:
            rows.append({**base, "decision": "drop", "reason": "no NAIP chip", "n_boxes": 0, "best_conf": 0.0,
                         "verify": float("nan"), "naip_item": ""})
            continue
        x = torch.from_numpy(imgs[j:j + 1])
        x_pre = det.preprocess(x)
        d = det.detect(x, conf=args.min_det_conf, x_pre=x_pre).cpu().numpy()
        dets = d[:, 1:6] if len(d) else np.zeros((0, 5))
        epsg, tf = int(wp.epsg[j]), wp.chip_transform[j]
        rpx, rpy = (geom_to_px(shapely.Point(float(cols["lon"][i]), float(cols["lat"][i])), epsg, tf)[:2])
        poly = polys[j]
        if poly is not None:
            pb = np.clip(geom_to_px(poly, epsg, tf), 0, CHIP_PX)
            vb = torch.tensor([[0, *pb, 1.0]], dtype=torch.float32)
            ver_poly = float(ver.score_boxes(x_pre, vb, CHIP_PX).cpu().numpy()[0])
            decision, boxes, why = decide_polygon(pb, dets, area if area == area else 0.0, courts, args)
            vscore = ver_poly
        else:
            pb = None
            near = np.zeros(0, int)
            if len(dets):
                dc = np.hypot((dets[:, 0] + dets[:, 2]) / 2 - rpx, (dets[:, 1] + dets[:, 3]) / 2 - rpy) * GSD
                near = np.flatnonzero((dc <= args.snap_m) | (point_box_dist(rpx, rpy, dets[:, :4]) * GSD <= args.snap_edge_m))
            vscore = 0.0
            if len(near):
                k = int(near[np.argmax(dets[near, 4])])
                vb = torch.tensor([[0, *dets[k, :4], 1.0]], dtype=torch.float32)
                vscore = float(ver.score_boxes(x_pre, vb, CHIP_PX).cpu().numpy()[0])
            decision, boxes, why = decide_point(rpx, rpy, dets, vscore, args)
        best = float(dets[:, 4].max()) if len(dets) else 0.0
        item_id = str(cat.ids[int(wp.item[j])]) if wp.item[j] >= 0 else ""
        rows.append({**base, "decision": decision, "reason": why, "n_boxes": len(boxes), "best_conf": best,
                     "verify": vscore, "naip_item": item_id})
        for k, b in enumerate(boxes):
            out_geoms.append(px_box_to_lonlat(np.asarray(b, float), epsg, tf))
            gb = out_geoms[-1]
            wm, hm = (b[2] - b[0]) * GSD, (b[3] - b[1]) * GSD
            rows[-1].setdefault("_boxes", []).append({"ref": f"truth:{base['truth_id']}" + (f"#{k}" if len(boxes) > 1 else ""),
                                                     "lon": gb.centroid.x, "lat": gb.centroid.y, "w_m": wm, "h_m": hm})
        # review image: whole chip, polygon (red), record point (red cross), label boxes (green), v2 boxes (magenta)
        im = Image.fromarray(imgs[j]).convert("RGB")
        dr = ImageDraw.Draw(im)
        for b5 in dets:
            if b5[4] >= 0.05:
                dr.rectangle(list(b5[:4]), outline=(255, 0, 255), width=1)
                dr.text((b5[0] + 2, max(0, b5[1] - 10)), f"{b5[4]:.2f}", fill=(255, 0, 255))
        if pb is not None:
            dr.rectangle(list(pb), outline=(255, 40, 40), width=1)
        dr.line([rpx - 6, rpy, rpx + 6, rpy], fill=(255, 40, 40), width=1)
        dr.line([rpx, rpy - 6, rpx, rpy + 6], fill=(255, 40, 40), width=1)
        for b in boxes:
            dr.rectangle([float(v) for v in b], outline=(0, 255, 0), width=2)
        short = why.replace("polygon box; v2 agrees at", "agree").replace("no v2 box on the polygon", "no box")
        review.append((im, f"{len(rows) - 1:03d} {src[:10]} {decision} {short[:34]}", decision))
    # outputs
    labels: list[dict[str, Any]] = []
    for r in rows:
        for b in r.pop("_boxes", []):
            labels.append({**{k: r[k] for k in ("truth_id", "source", "state", "decision")}, **b})
    write_geoparquet(args.out_dir / "snapped.parquet", {
        "ref": [b["ref"] for b in labels], "truth_id": [b["truth_id"] for b in labels],
        "source": [b["source"] for b in labels], "state": [b["state"] for b in labels],
        "decision": [b["decision"] for b in labels], "center_lon": np.array([b["lon"] for b in labels]),
        "center_lat": np.array([b["lat"] for b in labels]), "w_m": np.array([b["w_m"] for b in labels]),
        "h_m": np.array([b["h_m"] for b in labels])}, out_geoms)
    pq.write_table(pa.Table.from_pylist(rows), args.out_dir / "snap_decisions.parquet")
    summ: dict[str, Any] = {"records": len(rows), "decisions": dict(Counter(r["decision"] for r in rows)),
                            "label_boxes": len(labels), "by_source": {}}
    for src in sorted({r["source"] for r in rows}):
        rr = [r for r in rows if r["source"] == src]
        summ["by_source"][src] = dict(Counter(r["decision"] for r in rr))
    summ["drop_reasons"] = dict(Counter(r["reason"].split(":")[0][:40] for r in rows if r["decision"] == "drop"))
    write_json(args.out_dir / "snap_summary.json", summ)
    # review sheets: every record, grouped by decision, numbered like snap_decisions rows
    sheet_dir = config.OUT_DIR / "v3" / "snap_review"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    for old in sheet_dir.glob("*.jpg"):
        old.unlink()
    for dec in ("keep", "split", "drop"):
        group = [(im, cap) for im, cap, d_ in review if d_ == dec]
        for k in range(0, len(group), 20):
            montage([g[0] for g in group[k:k + 20]], 5, sheet_dir / f"{dec}_{k // 20:02d}.jpg", tile=320,
                    captions=[g[1] for g in group[k:k + 20]])
    log.info("snap: %s (%.0f s)", json.dumps(summ), time.monotonic() - t0)


# =========================================================================== layers
def item_epsg(cat: Any, item_ids: np.ndarray) -> np.ndarray:
    """EPSG code of each NAIP item id (national result ids may carry a '~ST' suffix)."""
    pos = {str(k): i for i, k in enumerate(cat.ids)}
    out = np.zeros(len(item_ids), np.int64)
    for j, it in enumerate(item_ids):
        k = pos.get(str(it).split("~")[0])
        out[j] = int(cat.epsg[k]) if k is not None else 0
    return out


def boxes_lonlat(lon: np.ndarray, lat: np.ndarray, w_m: np.ndarray, h_m: np.ndarray, epsg: np.ndarray) -> np.ndarray:
    """Axis-aligned w x h rectangles in each box's own UTM grid (the item it was detected in), as lon/lat polygons."""
    out = np.empty(len(lon), dtype=object)
    for code in np.unique(epsg):
        sel = np.flatnonzero(epsg == code)
        crs = f"EPSG:{int(code)}" if code > 0 else ALBERS
        fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        x, y = fwd.transform(lon[sel], lat[sel])
        x, y = np.asarray(x), np.asarray(y)
        hw, hh = w_m[sel] / 2, h_m[sel] / 2
        xs = np.stack([x - hw, x + hw, x + hw, x - hw], 1)
        ys = np.stack([y - hh, y - hh, y + hh, y + hh], 1)
        lo, la = inv.transform(xs.ravel(), ys.ravel())
        ring = np.stack([np.asarray(lo).reshape(-1, 4), np.asarray(la).reshape(-1, 4)], 2)
        out[sel] = shapely.polygons(ring)
    return out


def cmd_layers(args: argparse.Namespace) -> None:
    from .privacy import naip_catalog
    from .states import States

    t0 = time.monotonic()
    zones = load_polygons(args.zones)
    states = States.load(config.STATES_FILE)
    tn = to_albers(np.array([states.geometry(HELDOUT_STATE)], dtype=object))[0]
    # no extra label may sit in a held-out zone or within 1 km of Tennessee (the val chips stay OSM-labelled)
    excl = shapely.union_all(np.concatenate([to_albers(zones), [shapely.buffer(tn, 1000.0)]]))
    shapely.prepare(excl)

    def outside(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        x, y = _TO_ALB.transform(lon, lat)
        return ~shapely.contains_xy(excl, np.asarray(x), np.asarray(y))

    cat = naip_catalog(config.NAIP_GEOPARQUET_DIR)
    parts: list[dict[str, Any]] = []
    # 1. snapped official courts
    sa, sg = read_geoparquet(args.snapped)
    keep = np.isin(sa["decision"], ("keep", "split")) & outside(sa["center_lon"], sa["center_lat"])
    # two records (same park, a few metres apart) can snap to the same v2 box: keep one
    sg_m = to_albers(sg)
    for i in np.flatnonzero(keep):
        for j in np.flatnonzero(keep[:i]):
            inter = shapely.area(shapely.intersection(sg_m[i], sg_m[j]))
            if inter / max(shapely.area(shapely.union(sg_m[i], sg_m[j])), 1e-9) > 0.5:
                keep[i] = False
                log.info("official %s duplicates %s; dropped", sa["ref"][i], sa["ref"][j])
                break
    nk = int(keep.sum())
    parts.append({"ref": sa["ref"][keep], "kind": np.full(nk, "official", dtype=object),
                  "role": np.full(nk, "label", dtype=object), "state": sa["state"][keep],
                  "center_lon": sa["center_lon"][keep], "center_lat": sa["center_lat"][keep], "conf": np.full(nk, np.nan),
                  "verify": np.full(nk, np.nan), "geom": sg[keep]})
    # 2. pseudo-labels: v2's most certain public candidates (the exported operating point; the context CNN agrees)
    t = pq.read_table(args.candidates, columns=["id", "state", "lon", "lat", "conf", "verify_scan", "p_home", "privacy",
                                                "box_w_m", "box_h_m", "naip_item"]).to_pydict()
    cd = {k: np.asarray(v) for k, v in t.items()}
    ph = cd["p_home"].astype(float)
    vs = cd["verify_scan"].astype(float)
    pseudo = ((cd["privacy"] == "public") & (cd["conf"].astype(float) >= args.pseudo_conf) & (vs >= args.pseudo_verify)
              & np.isfinite(ph) & (ph < args.pseudo_phome))
    pseudo &= outside(cd["lon"].astype(float), cd["lat"].astype(float))
    pi = np.flatnonzero(pseudo)
    pseudo_ids = set(cd["id"][pi].tolist())
    geo = boxes_lonlat(cd["lon"][pi].astype(float), cd["lat"][pi].astype(float), cd["box_w_m"][pi].astype(float),
                       cd["box_h_m"][pi].astype(float), item_epsg(cat, cd["naip_item"][pi]))
    parts.append({"ref": np.array([f"pseudo:{i}" for i in cd["id"][pi]], dtype=object),
                  "kind": np.full(len(pi), "pseudo", dtype=object), "role": np.full(len(pi), "label", dtype=object),
                  "state": cd["state"][pi], "center_lon": cd["lon"][pi].astype(float), "center_lat": cd["lat"][pi].astype(float),
                  "conf": cd["conf"][pi].astype(float), "verify": vs[pi], "geom": geo})
    del t, cd
    # 3./4. every national detection (also those near OSM courts): likely courts -> "maybe" (chip dropped unless a
    # label covers it), clear look-alikes -> "hard" (the chip is a hard negative)
    d = pq.read_table(args.detections, columns=["id", "conf", "verify", "lon", "lat", "box_w_m", "box_h_m", "item_id",
                                                "state"]).to_pydict()
    dd = {k: np.asarray(v) for k, v in d.items()}
    conf = dd["conf"].astype(float)
    ver = np.nan_to_num(dd["verify"].astype(float), nan=0.0)
    not_pseudo = ~np.isin(dd["id"], list(pseudo_ids))
    maybe = not_pseudo & (((conf >= args.maybe_conf) & (ver >= args.maybe_verify))
                          | ((conf >= args.maybe_conf_hi) & (ver >= args.maybe_verify_lo)))
    hard = not_pseudo & ~maybe & (conf >= args.hard_conf) & (ver < args.hard_verify)
    for name, msk, role in (("maybe", maybe, "ambiguous"), ("hard", hard, "hard")):
        msk &= outside(dd["lon"].astype(float), dd["lat"].astype(float))
        ii = np.flatnonzero(msk)
        geo = boxes_lonlat(dd["lon"][ii].astype(float), dd["lat"][ii].astype(float), dd["box_w_m"][ii].astype(float),
                           dd["box_h_m"][ii].astype(float), item_epsg(cat, dd["item_id"][ii]))
        parts.append({"ref": np.array([f"{name}:{i}" for i in dd["id"][ii]], dtype=object),
                      "kind": np.full(len(ii), name, dtype=object), "role": np.full(len(ii), role, dtype=object),
                      "state": dd["state"][ii], "center_lon": dd["lon"][ii].astype(float),
                      "center_lat": dd["lat"][ii].astype(float), "conf": conf[ii], "verify": ver[ii], "geom": geo})
    cols = {k: np.concatenate([p[k] for p in parts]) for k in parts[0] if k != "geom"}
    geoms = np.concatenate([p["geom"] for p in parts])
    write_geoparquet(args.out, cols, geoms)
    counts = dict(Counter(cols["kind"].tolist()))
    meta = {"counts": counts, "rules": {
        "official": "snap decisions keep/split (training sources, outside held-out zones and Tennessee + 1 km)",
        "pseudo": f"public candidate, conf >= {args.pseudo_conf}, verify_scan >= {args.pseudo_verify}, "
                  f"context CNN p_home < {args.pseudo_phome}",
        "maybe": f"any detection with (conf >= {args.maybe_conf} and verify >= {args.maybe_verify}) or "
                 f"(conf >= {args.maybe_conf_hi} and verify >= {args.maybe_verify_lo}), not a pseudo-label",
        "hard": f"any detection with conf >= {args.hard_conf} and verify < {args.hard_verify}"},
        "pseudo_by_state": dict(Counter(cols["state"][cols["kind"] == "pseudo"].tolist()).most_common())}
    write_json(args.out.with_suffix(".json"), meta)
    log.info("layers: %s -> %s (%.0f s)", counts, args.out, time.monotonic() - t0)


# =========================================================================== heldout-items
def cmd_heldout_items(args: argparse.Namespace) -> None:
    area = to_albers(load_polygons(args.area))
    u = shapely.union_all(area)
    shapely.prepare(u)
    plan = pq.read_table(args.plan / "plan.parquet", columns=["item_id", "state", "kept_tiles", "zone_wkb"])
    zones = shapely.from_wkb(plan.column("zone_wkb").to_numpy(zero_copy_only=False))
    hit = shapely.intersects(u, zones)
    ids = np.asarray(plan.column("item_id").to_pylist(), dtype=object)[hit]
    st = np.asarray(plan.column("state").to_pylist(), dtype=object)[hit]
    tiles = np.asarray(plan.column("kept_tiles").to_pylist())[hit]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(ids.tolist()) + "\n", encoding="utf-8")
    scanned = from_albers(np.array([shapely.union_all(zones[hit])], dtype=object))
    write_polygons_geojson(args.out.with_name("heldout_scanned_area.geojson"), list(scanned), [{"items": int(hit.sum())}])
    log.info("held-out items: %d (%s), %d tiles -> %s", int(hit.sum()), dict(Counter(st.tolist())), int(tiles.sum()),
             args.out)


# =========================================================================== heldout-eval
def _heldout_runs(det: Path, area: Path, sources: list[str], th: list[float]) -> dict[str, Any]:
    from . import truth

    out: dict[str, Any] = {}
    runs: dict[str, tuple[bool, float]] = {"all": (False, 0.0), "ge150": (False, 150.0), "not_in_osm": (True, 0.0),
                                           "not_in_osm_ge150": (True, 150.0)}
    for name, (nio, min_area) in runs.items():
        res = truth.evaluate(det, "conf", th, config.KNOWN_COURT_RADIUS_M, 15.0, 250.0, None, sources, False, area,
                             nio, min_area)
        out[name] = {"recall": res.get("all_court_level_recall"), "rows": res.get("all_court_level_rows"),
                     "by_source": {k: {"rows": e.get("recall_rows"), "recall": e.get("recall")}
                                   for k, e in res.get("sources", {}).items()},
                     "in_covered_parks": {k: e["in_covered_parks"] for k, e in res.get("sources", {}).items()
                                          if "in_covered_parks" in e}}
    return out


def planimetric_precision(det: dict[str, np.ndarray], sources: list[str], th: list[float],
                          verify_min: tuple[float, ...] = (0.0, 0.9, 0.98)) -> dict[str, Any]:
    """All-owner planimetric layers (DC, Ramsey) trace every visible court, so a detection inside their coverage that
    matches no traced court (15 m, any size) is a false positive or a court built after the capture."""
    from .truth import COVERAGE_FILE, load_truth, truth_geoms_m

    cols, _ = load_truth()
    cc, cg = read_geoparquet(COVERAGE_FILE)
    x, y = _TO_ALB.transform(det["lon"].astype(float), det["lat"].astype(float))
    pts = shapely.points(np.asarray(x), np.asarray(y))
    conf = det["conf"].astype(float)
    ver = np.nan_to_num(det["verify"].astype(float), nan=0.0)
    out: dict[str, Any] = {}
    for src in sources:
        cov = to_albers(cg[(cc["source"] == src) & np.isin(cc["kind"], ("city", "county"))])
        if not len(cov):
            continue
        u = shapely.union_all(cov)
        shapely.prepare(u)
        inside = shapely.contains(u, pts)
        rows = (cols["source"] == src) & (cols["status"] != "removed")
        tg = truth_geoms_m({k: v[rows] for k, v in cols.items()})
        tree = STRtree(tg)
        di, _ = tree.query(pts[inside], predicate="dwithin", distance=15.0)
        matched = np.zeros(len(pts), bool)
        matched[np.flatnonzero(inside)[np.unique(di)]] = True
        e: dict[str, Any] = {}
        for v in verify_min:
            for t in th:
                m = inside & (conf >= t) & (ver >= v)
                k = int(m.sum())
                e[f"c{t:.2f}_v{v:.2f}"] = {"detections": k, "matched": int(matched[m].sum()),
                                           "precision": round(float(matched[m].mean()), 3) if k else None}
        out[src] = e
    return out


def cmd_heldout_eval(args: argparse.Namespace) -> None:
    th = list(args.thresholds)
    sources = list(args.heldout)
    res: dict[str, Any] = {"sources": sources, "thresholds": th, "area": str(args.area), "models": {}}
    tmp = args.out.with_name(".heldout_tmp.parquet")
    for m in args.models:
        det_path = args.dir / m / "detections.parquet"
        if not det_path.exists():
            log.warning("%s: no %s", m, det_path)
            continue
        t = pq.read_table(det_path)
        d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in ("lon", "lat", "conf", "verify", "near_known_osm")}
        e: dict[str, Any] = {"detections": t.num_rows, "runs": _heldout_runs(det_path, args.area, sources, th)}
        # covered-park share of detections not near an OSM court (only courts OSM lacks can match them)
        pq.write_table(t.filter(pc.equal(t.column("near_known_osm"), "")), tmp)
        e["new_finds"] = _heldout_runs(tmp, args.area, sources, th)["all"]["in_covered_parks"]
        for v in (0.9, 0.98):
            pq.write_table(t.filter(pc.greater_equal(pc.fill_null(t.column("verify"), 0.0), v)), tmp)
            e[f"verify_ge_{v}"] = {k: r["recall"] for k, r in _heldout_runs(tmp, args.area, sources, th).items()}
        e["planimetric_precision"] = planimetric_precision(d, [s for s in sources if "planimetrics" in s], th)
        res["models"][m] = e
        log.info("%s: not-in-OSM >=150 recall %s", m, e["runs"]["not_in_osm_ge150"]["recall"])
    tmp.unlink(missing_ok=True)
    write_json(args.out, res)
    print_heldout(res)


def _park_sum(cp: dict[str, Any], key: str) -> tuple[int, int]:
    det = mat = 0
    for v in cp.values():
        r = v.get(key) or {}
        det += int(r.get("detections") or 0)
        mat += int(r.get("match_official") or 0)
    return det, mat


def print_heldout(res: dict[str, Any]) -> None:
    th = [f"{t:.2f}" for t in res["thresholds"]]
    print("\nHeld-out truth sources: court-level recall (40 m point / 15 m polygon)")
    print("| subset | model | rows | " + " | ".join(th) + " |")
    print("|---|---|---|" + "---|" * len(th))
    for run in ("all", "ge150", "not_in_osm", "not_in_osm_ge150"):
        for m, e in res["models"].items():
            r = e["runs"][run]
            if r["recall"]:
                print(f"| {run} | {m} | {r['rows']} | " + " | ".join(f"{r['recall'][t]:.2f}" for t in th) + " |")
    print("\nDetections inside covered parks matching an official court (matched / detections)")
    for m, e in res["models"].items():
        a = [_park_sum(e["runs"]["all"]["in_covered_parks"], t) for t in th]
        n = [_park_sum(e["new_finds"], t) for t in th]
        print(f"| {m} all | " + " | ".join(f"{mm}/{dd}" for dd, mm in a) + " |")
        print(f"| {m} not near OSM | " + " | ".join(f"{mm}/{dd}" for dd, mm in n) + " |")
    print("\nOperating curve: DC + Ramsey precision (all detections) vs recall on held-out courts (compare at equal "
          "precision)")
    print("| model | conf >= | DC+Ramsey precision | recall OSM lacks >=150 | recall all >=150 |")
    print("|---|---|---|---|---|")
    for m, e in res["models"].items():
        for t in th:
            mm = dd = 0
            for pe in e["planimetric_precision"].values():
                c = pe.get(f"c{t}_v0.00") or {}
                mm += int(c.get("matched") or 0)
                dd += int(c.get("detections") or 0)
            r1 = (e["runs"]["not_in_osm_ge150"]["recall"] or {}).get(t)
            r2 = (e["runs"]["ge150"]["recall"] or {}).get(t)
            print(f"| {m} | {t} | {mm / dd if dd else float('nan'):.3f} ({mm}/{dd}) | {r1} | {r2} |")
    print("\nPlanimetric precision (detections inside DC / Ramsey matching a traced court)")
    for m, e in res["models"].items():
        for src, pe in e["planimetric_precision"].items():
            cells = [pe.get(f"c{t}_v0.00", {}) for t in th]
            print(f"| {m} {src} | " + " | ".join(f"{c.get('matched')}/{c.get('detections')}" for c in cells) + " |")


# =========================================================================== tn-eval
def cmd_tn_eval(args: argparse.Namespace) -> None:
    res: dict[str, Any] = {}
    for m in args.models:
        for split in ("tn", "us"):
            p = args.dir / f"eval_{split}_{m}" / "metrics.json"
            if not p.exists():
                continue
            d = read_json(p)
            r30 = {f"{r['conf']:.2f}": r for r in d["pr_iou30"]}
            res[f"{split}_{m}"] = {
                "ultralytics": {k: v for k, v in (d.get("ultralytics") or {}).items() if k != "pr_at_conf"},
                "gt_boxes": d.get("gt_boxes"), "background_chips": d.get("background_chips"),
                "pr_iou30": {t: {k: r30[t][k] for k in ("precision", "recall", "background_chips_firing")}
                             for t in ("0.30", "0.50", "0.60", "0.70") if t in r30},
                "background_by_kind": d.get("background_by_kind")}
    write_json(args.out, res)
    print("| split | model | mAP50 | mAP50-95 | P/R @0.3 | @0.5 | @0.6 | @0.7 | bg firing @0.5 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for k, e in res.items():
        u = e["ultralytics"]
        pr = e["pr_iou30"]
        cells = [f"{pr[t]['precision']:.3f} / {pr[t]['recall']:.3f}" if t in pr else "-"
                 for t in ("0.30", "0.50", "0.60", "0.70")]
        bg = pr.get("0.50", {}).get("background_chips_firing")
        print(f"| {k.split('_')[0]} | {k.split('_')[1]} | {u.get('mAP50', '-')} | {u.get('mAP50_95', '-')} | "
              + " | ".join(cells) + f" | {bg} |")


# =========================================================================== privacy with a new p_home
_POINT_LOT = __import__("re").compile(r"^near (\S+) (\S+) point but residential lot")


def reclassify(privacy: str, site: str, reason: str, p_home: float) -> tuple[str, str, str]:
    """Privacy class once the context CNN has scored a candidate that was classed by the rules alone (p_home NaN).

    Mirrors ``privacy.classify_point`` exactly: p_home only matters inside four branches, and the rules-only class and
    reason say which branch a candidate reached (see tests/test_v3.py)."""
    from .privacy import HOME_CNN, HOUSE_LOT_CNN, VETO_POINT, VETO_POLYGON

    ph = p_home
    if ph != ph:  # still no CNN score
        return privacy, site, reason
    if privacy == "public" and reason.startswith("inside ") and reason.endswith(" polygon"):
        if ph >= VETO_POLYGON:
            return "private_home", "", f"{reason} but CNN home p={ph:.2f}"
        return privacy, site, reason
    if privacy == "public" and reason.startswith("within "):
        # rules-only public near a point site: the house-lot rule did not fire, so only the veto can
        if ph >= VETO_POINT:
            kind_src = reason.split(" of ", 1)[1].rsplit(" point", 1)[0]
            return "private_home", "", f"near {kind_src} point but residential lot (CNN home p={ph:.2f})"
        return privacy, site, reason
    mm = _POINT_LOT.match(reason) if privacy == "private_home" else None
    if mm:
        # rules-only: houses within 35 m and no larger building -> with the CNN this also needs p_home >= HOUSE_LOT_CNN
        if ph >= VETO_POINT or ph >= HOUSE_LOT_CNN:
            why = f"CNN home p={ph:.2f}" if ph >= VETO_POINT else "houses within 35 m, no larger or civic building nearby"
            return "private_home", "", f"near {mm.group(1)} {mm.group(2)} point but residential lot ({why})"
        return "public", mm.group(2), f"near {mm.group(1)} {mm.group(2)} point (houses within 35 m, CNN home p={ph:.2f})"
    if privacy == "unknown" and ph >= HOME_CNN:
        return "private_home", "", f"CNN home p={ph:.2f}"
    return privacy, site, reason


def cmd_merge_rescore(args: argparse.Namespace) -> None:
    t = pq.read_table(args.candidates)
    r = pq.read_table(args.rescore, columns=["id", "chip_ok", "conf3", "verify", "p_home"])
    pos = {cid: i for i, cid in enumerate(r.column("id").to_pylist())}
    ids = t.column("id").to_pylist()
    j = np.array([pos.get(c, -1) for c in ids])
    hit = j >= 0
    rr = {k: r.column(k).to_numpy(zero_copy_only=False) for k in ("chip_ok", "conf3", "verify", "p_home")}
    ok = np.zeros(len(ids), bool)
    ok[hit] = rr["chip_ok"][j[hit]].astype(bool)
    conf3 = np.full(len(ids), np.nan)
    ver3 = np.full(len(ids), np.nan)
    ph3 = np.full(len(ids), np.nan)
    conf3[ok] = rr["conf3"][j[ok]]
    ver3[ok] = rr["verify"][j[ok]]
    ph3[ok] = rr["p_home"][j[ok]]
    ph_old = t.column("p_home").to_numpy(zero_copy_only=False).astype(float)
    newly = ~np.isfinite(ph_old) & np.isfinite(ph3)
    ph = np.where(np.isfinite(ph_old), ph_old, ph3)
    priv = t.column("privacy").to_numpy(zero_copy_only=False).astype(object)
    site = np.asarray([s or "" for s in t.column("site").to_pylist()], dtype=object)
    why = t.column("privacy_reason").to_numpy(zero_copy_only=False).astype(object)
    changes: Counter[str] = Counter()
    for i in np.flatnonzero(newly):
        c, s, w = reclassify(str(priv[i]), str(site[i]), str(why[i]), float(ph3[i]))
        if c != priv[i]:
            changes[f"{priv[i]} -> {c}"] += 1
        priv[i], site[i], why[i] = c, s, w
    conf_v2 = t.column("conf").to_numpy().astype(float)
    cols = {k: t.column(k) for k in t.column_names}
    cols.update({"conf": pa.array(np.nan_to_num(conf3, nan=0.0)), "conf_v2": pa.array(conf_v2), "conf3": pa.array(conf3),
                 "verify3": pa.array(ver3), "p_home": pa.array(ph), "p_home_new": pa.array(newly),
                 "privacy": pa.array(priv.tolist(), pa.string()), "site": pa.array(site.tolist(), pa.string()),
                 "privacy_reason": pa.array(why.tolist(), pa.string())})
    out = pa.table(cols).sort_by([("conf", "descending")])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(out, args.out, compression="zstd")
    summ = {"candidates": len(ids), "rescored": int(hit.sum()), "chip_ok": int(ok.sum()), "p_home_new": int(newly.sum()),
            "class_changes": dict(changes),
            "conf3_ge": {f"{x:.1f}": int((np.nan_to_num(conf3) >= x).sum()) for x in (0.1, 0.3, 0.5, 0.7)}}
    write_json(args.out.with_name("merge_rescore.json"), summ)
    log.info("merge-rescore: %s", json.dumps(summ))


# =========================================================================== calibrate
def cmd_calibrate(args: argparse.Namespace) -> None:
    """Per-state verifier thresholds that pass the same share of known courts as ``--ref`` does nationally.

    Known courts are the national v2 detections near an OSM basketball feature with conf >= --min-conf. The verifier
    scores them lower in some states' flights (README "Per-state counts"), which is what makes the export lopsided."""
    t = pq.read_table(args.detections, columns=["conf", "verify", "state", "near_known_osm"])
    t = t.filter(pc.and_(pc.not_equal(t.column("near_known_osm"), ""), pc.greater_equal(t.column("conf"), args.min_conf)))
    st = t.column("state").to_numpy(zero_copy_only=False).astype(str)
    v = np.nan_to_num(t.column("verify").to_numpy(zero_copy_only=False).astype(float), nan=0.0)
    share = float((v >= args.ref).mean())
    res: dict[str, Any] = {"ref_verify": args.ref, "national_share_known_courts": round(share, 4),
                           "floor": args.floor, "min_known": args.min_known, "states": {}}
    for s in sorted(set(st.tolist())):
        vs = v[st == s]
        q = float(np.quantile(vs, 1 - share)) if len(vs) >= args.min_known else args.ref
        thr = float(min(args.ref, max(args.floor, q)))
        res["states"][s] = {"known": int(len(vs)), "share_at_ref": round(float((vs >= args.ref).mean()), 4),
                            "threshold": round(thr, 4), "share_at_threshold": round(float((vs >= thr).mean()), 4)}
    write_json(args.out, res)
    low = {s: e["threshold"] for s, e in res["states"].items() if e["threshold"] < args.ref}
    log.info("national share of known courts at verify >= %.2f: %.3f; %d states get a lower threshold: %s", args.ref,
             share, len(low), low)


# =========================================================================== select
def selection(d: dict[str, np.ndarray], a: argparse.Namespace, thresholds: dict[str, float] | None) -> np.ndarray:
    ver = np.nan_to_num(d[a.verify_col].astype(float), nan=-1.0)
    vthr = np.full(len(ver), a.min_verify)
    if thresholds:
        vthr = np.array([thresholds.get(s, a.min_verify) for s in d["state"]])
    sel = ((d["privacy"] == "public") & (d["conf"].astype(float) >= a.min_conf) & (ver >= vthr)
           & np.isfinite(d["p_home"].astype(float)))
    if a.min_conf_v2 > 0:
        sel &= d["conf_v2"].astype(float) >= a.min_conf_v2
    return sel


def cmd_select(args: argparse.Namespace) -> None:
    t = pq.read_table(args.candidates)
    d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in ("state", "conf", "conf_v2", "privacy", "p_home",
                                                                   "verify_scan", "verify3")}
    thr = None
    if args.thresholds:
        thr = {s: float(e["threshold"]) for s, e in read_json(args.thresholds)["states"].items()}
    sel = selection(d, args, thr)
    out = t.append_column("export", pa.array(sel)) if "export" not in t.column_names else \
        t.set_column(t.column_names.index("export"), "export", pa.array(sel))
    pq.write_table(out, args.candidates, compression="zstd")
    by = Counter(d["state"][sel].tolist())
    rule = {"privacy": "public", "min_conf (v3)": args.min_conf, "verify_col": args.verify_col, "min_verify": args.min_verify,
            "per_state_thresholds": args.thresholds.as_posix() if args.thresholds else None, "min_conf_v2": args.min_conf_v2,
            "require_phome": True}
    write_json(args.candidates.with_name("selection.json"), {"rule": rule, "selected": int(sel.sum()),
                                                             "by_state": dict(by.most_common())})
    log.info("selected %d candidates (%s); top states %s", int(sel.sum()), rule, by.most_common(8))


# =========================================================================== review
def cmd_review(args: argparse.Namespace) -> None:
    """Blind review sheets (fresh NAIP chips, 96 m crops at 2x, only a number on each), like ``national review``."""
    from PIL import Image, ImageDraw

    t = pq.read_table(args.candidates)
    if args.where == "export":
        t = t.filter(pc.equal(t.column("export"), True))
    elif args.where == "new":  # selected now, not exported by v2
        t = t.filter(pc.and_(pc.equal(t.column("export"), True), pc.invert(pc.is_in(
            t.column("id"), value_set=pa.array(pq.read_table(args.v2_exported, columns=["id"]).column("id").to_pylist())))))
    if args.states:
        t = t.filter(pc.is_in(t.column("state"), value_set=pa.array(args.states)))
    d = t.to_pydict()
    n = len(d["id"])
    rng = random.Random(args.seed)
    take = sorted(rng.sample(range(n), min(args.n, n)))
    lon = np.asarray(d["lon"], float)[take]
    lat = np.asarray(d["lat"], float)[take]
    imgs, ok, _, _ = fetch_chips(lon, lat, np.asarray(d["state"], dtype=object)[take].astype(str), workers=32)
    out = args.out_dir / args.name
    (out / "crops").mkdir(parents=True, exist_ok=True)
    items, thumbs = [], []
    order = list(range(len(take)))
    rng.shuffle(order)
    k = 0
    for q in order:
        if not ok[q]:
            continue
        i = take[q]
        k += 1
        c, h = CHIP_PX / 2, args.crop_px / 2
        im = Image.fromarray(imgs[q]).crop((int(c - h), int(c - h), int(c + h), int(c + h)))
        im = im.resize((args.thumb_px, args.thumb_px), Image.Resampling.BICUBIC)
        s = args.thumb_px / args.crop_px
        w, hh = d["box_w_m"][i] / GSD * s / 2, d["box_h_m"][i] / GSD * s / 2
        mid = args.thumb_px / 2
        dr = ImageDraw.Draw(im)
        dr.rectangle([mid - w - 4, mid - hh - 4, mid + w + 4, mid + hh + 4], outline=(255, 255, 0), width=2)
        dr.rectangle([0, 0, 34, 14], fill=(0, 0, 0))
        dr.text((2, 1), f"{k:03d}", fill=(255, 255, 0))
        im.save(out / "crops" / f"{k:03d}.jpg", quality=90)
        thumbs.append(im)
        items.append({"n": k, "stratum": "all", **{kk: (d[kk][i] if not isinstance(d[kk][i], float) or d[kk][i] == d[kk][i]
                                                       else None) for kk in ("id", "state", "conf", "conf_v2", "verify_scan",
                                                                             "verify3", "p_home", "site", "lon", "lat")}})
    cols, rws = 5, 4
    for s0 in range(0, len(thumbs), cols * rws):
        sheet = Image.new("RGB", (cols * args.thumb_px, rws * args.thumb_px), "white")
        for jj, th in enumerate(thumbs[s0:s0 + cols * rws]):
            sheet.paste(th, ((jj % cols) * args.thumb_px, (jj // cols) * args.thumb_px))
        sheet.save(out / f"sheet_{s0 // (cols * rws):02d}.jpg", quality=88)
    write_json(out / "sample.json", {"where": args.where, "states": args.states, "population": n,
                                     "strata": {"all": {"population": n, "sampled": len(items)}}, "items": items,
                                     "verify_col": "verify_scan",
                                     "note": "blind review (only a number on each crop); model-assistant labels, NOT gold"})
    log.info("review %s: %d crops of %d -> %s", args.name, len(items), n, out)



# =========================================================================== grid
def parse_rule(text: str) -> dict[str, Any]:
    """'conf=0.5,verify=0.98[,col=verify_scan][,states=path][,v2=0.5]' -> rule dict."""
    r: dict[str, Any] = {"min_conf": 0.5, "min_verify": 0.98, "verify_col": "verify_scan", "thresholds": None,
                         "min_conf_v2": 0.0, "score": "conf"}
    for part in text.split(","):
        k, _, v = part.partition("=")
        if k == "conf":
            r["min_conf"] = float(v)
        elif k == "verify":
            r["min_verify"] = float(v)
        elif k == "col":
            r["verify_col"] = v
        elif k == "states":
            r["thresholds"] = v
        elif k == "v2":
            r["min_conf_v2"] = float(v)
        elif k == "score":  # conf (v3) or conf_v2: rank by the v2 score instead
            r["score"] = v
    return r


def cmd_grid(args: argparse.Namespace) -> None:
    """Export rules side by side on held-out evidence: per-state counts, the share of selected candidates inside
    held-out covered parks that match an official court, DC / Ramsey planimetric precision, and recall on the held-out
    courts OSM lacks. Candidates are never near an OSM court, so only courts OSM lacks can match them."""
    t = pq.read_table(args.candidates, columns=["id", "state", "lon", "lat", "conf", "conf_v2", "privacy", "p_home",
                                                "verify_scan", "verify3"])
    d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in t.column_names}
    sources = list(args.heldout)
    tmp = args.out.with_name(".grid_tmp.parquet")
    rows = []
    for text in args.rules:
        r = parse_rule(text)
        ns = argparse.Namespace(min_conf=r["min_conf"], min_verify=r["min_verify"], verify_col=r["verify_col"],
                                min_conf_v2=r["min_conf_v2"])
        dd = dict(d)
        if r["score"] == "conf_v2":
            dd["conf"] = d["conf_v2"]
        thr = None
        if r["thresholds"]:
            thr = {s: float(e["threshold"]) for s, e in read_json(Path(r["thresholds"]))["states"].items()}
        sel = selection(dd, ns, thr)
        sub = t.filter(pa.array(sel))
        pq.write_table(sub.select(["lon", "lat", "conf", "state"]).set_column(2, "conf", pa.array(np.ones(sub.num_rows))),
                       tmp)
        res = _heldout_runs(tmp, args.area, sources, [0.5])
        park = _park_sum(res["all"]["in_covered_parks"], "0.50")
        plan = planimetric_precision({"lon": d["lon"][sel], "lat": d["lat"][sel], "conf": np.ones(int(sel.sum())),
                                      "verify": np.ones(int(sel.sum()))}, [s for s in sources if "planimetrics" in s],
                                     [0.5], (0.0,))
        pp = [(v["c0.50_v0.00"]["matched"], v["c0.50_v0.00"]["detections"]) for v in plan.values()]
        by = Counter(d["state"][sel].tolist())
        top = sum(v for _, v in by.most_common(1))
        rows.append({"rule": text, "selected": int(sel.sum()), "states_with_any": len(by),
                     "top_state": by.most_common(1)[0] if by else None, "top_state_share": round(top / max(1, sel.sum()), 3),
                     "TX": by.get("TX", 0), "MO": by.get("MO", 0), "OH": by.get("OH", 0), "CA": by.get("CA", 0),
                     "heldout_in_park_matched": park[1], "heldout_in_park": park[0],
                     "planimetric_matched": sum(m for m, _ in pp), "planimetric": sum(n for _, n in pp),
                     "recall_heldout_not_in_osm_ge150": (res["not_in_osm_ge150"]["recall"] or {}).get("0.50"),
                     "rows_heldout_not_in_osm_ge150": res["not_in_osm_ge150"]["rows"]})
    tmp.unlink(missing_ok=True)
    write_json(args.out, {"rules": rows})
    print("| rule | selected | top state share | TX | MO | OH | CA | held-out in-park matched | DC+Ramsey matched | "
          "recall held-out OSM-lacking >=150 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r_ in rows:
        print(f"| {r_['rule']} | {r_['selected']} | {r_['top_state_share']:.2f} | {r_['TX']} | {r_['MO']} | {r_['OH']} | "
              f"{r_['CA']} | {r_['heldout_in_park_matched']}/{r_['heldout_in_park']} | "
              f"{r_['planimetric_matched']}/{r_['planimetric']} | {r_['recall_heldout_not_in_osm_ge150']} |")


def cmd_rescore_input(args: argparse.Namespace) -> None:
    """The columns ``rescore`` needs, so the node upload stays small."""
    t = pq.read_table(args.candidates, columns=["id", "state", "lon", "lat", "conf", "box_w_m", "box_h_m"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(t, args.out, compression="zstd")
    log.info("%d candidates -> %s", t.num_rows, args.out)


# =========================================================================== CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("split")
    p.add_argument("--heldout", nargs="+", default=list(HELDOUT_SOURCES))
    p.add_argument("--buffer-m", type=float, default=100.0)
    p.add_argument("--out-dir", type=Path, default=V3_DATA)
    add_common_args(p)
    p = sub.add_parser("snap")
    p.add_argument("--heldout", nargs="+", default=list(HELDOUT_SOURCES))
    p.add_argument("--zones", type=Path, default=V3_DATA / "holdout_zones.geojson")
    p.add_argument("--sources", nargs="*")
    p.add_argument("--min-area", type=float, default=150.0)
    p.add_argument("--weights", type=Path, default=config.WEIGHTS_DIR / "best_v2.pt")
    p.add_argument("--imgsz", type=int, default=416)
    p.add_argument("--verifier", type=Path, default=config.WEIGHTS_DIR / "verifier_v2.pt")
    p.add_argument("--min-det-conf", type=float, default=0.01)
    p.add_argument("--snap-m", type=float, default=25.0, help="point sources: v2 box centre within this of the point")
    p.add_argument("--snap-edge-m", type=float, default=5.0, help="... or the point inside / this close to the box")
    p.add_argument("--point-conf", type=float, default=0.1, help="point sources: snapped box needs this v2 score ...")
    p.add_argument("--point-verify", type=float, default=1.01,
                   help="... or this verifier score on the box (default off: unreliable on weak boxes)")
    p.add_argument("--skip-sources", nargs="*", default=list(SKIP_SOURCES))
    p.add_argument("--exclude", type=Path, default=V3_DATA / "snap_exclude.txt", help="truth ids rejected by review")
    p.add_argument("--poly-conf", type=float, default=0.1, help="polygons: a v2 box at least this score ...")
    p.add_argument("--poly-iou", type=float, default=0.2, help="... overlapping the polygon box at least this IoU")
    p.add_argument("--multi-area", type=float, default=1100.0, help="m^2; bigger polygons may hold several courts")
    p.add_argument("--split-conf", type=float, default=0.1, help="v2 boxes used to split a multi-court polygon")
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--out-dir", type=Path, default=V3_DATA)
    add_common_args(p)
    p = sub.add_parser("layers")
    p.add_argument("--zones", type=Path, default=V3_DATA / "holdout_zones.geojson")
    p.add_argument("--snapped", type=Path, default=V3_DATA / "snapped.parquet")
    p.add_argument("--candidates", type=Path, default=NAT_OUT / "candidates_us.parquet")
    p.add_argument("--detections", type=Path, default=NAT_OUT / "detections.parquet")
    p.add_argument("--pseudo-conf", type=float, default=0.5)
    p.add_argument("--pseudo-verify", type=float, default=0.98)
    p.add_argument("--pseudo-phome", type=float, default=0.5)
    p.add_argument("--maybe-conf", type=float, default=0.25)
    p.add_argument("--maybe-verify", type=float, default=0.8)
    p.add_argument("--maybe-conf-hi", type=float, default=0.45)
    p.add_argument("--maybe-verify-lo", type=float, default=0.5)
    p.add_argument("--hard-conf", type=float, default=0.1)
    p.add_argument("--hard-verify", type=float, default=0.3)
    p.add_argument("--out", type=Path, default=V3_DATA / "extra_courts.parquet")
    add_common_args(p)
    p = sub.add_parser("heldout-items")
    p.add_argument("--area", type=Path, default=V3_DATA / "heldout_area.geojson")
    p.add_argument("--plan", type=Path, default=config.DATA_DIR / "scan2" / "US")
    p.add_argument("--out", type=Path, default=V3_DATA / "heldout_items.txt")
    add_common_args(p)
    p = sub.add_parser("heldout-eval")
    p.add_argument("--heldout", nargs="+", default=list(HELDOUT_SOURCES))
    p.add_argument("--models", nargs="+", default=["v1", "v2", "v3"])
    p.add_argument("--dir", type=Path, default=V3_OUT / "heldout", help="<dir>/<model>/detections.parquet")
    p.add_argument("--area", type=Path, default=V3_DATA / "heldout_scanned_area.geojson")
    p.add_argument("--thresholds", type=float, nargs="+", default=[0.1, 0.25, 0.4, 0.5, 0.6, 0.7])
    p.add_argument("--out", type=Path, default=V3_OUT / "heldout_eval.json")
    add_common_args(p)
    p = sub.add_parser("tn-eval")
    p.add_argument("--models", nargs="+", default=["v1", "v2", "v3"])
    p.add_argument("--dir", type=Path, default=V3_OUT)
    p.add_argument("--out", type=Path, default=V3_OUT / "tn_eval.json")
    add_common_args(p)
    p = sub.add_parser("merge-rescore")
    p.add_argument("--candidates", type=Path, default=NAT_OUT / "candidates_us.parquet")
    p.add_argument("--rescore", type=Path, default=V3_OUT / "rescore_v3.parquet")
    p.add_argument("--out", type=Path, default=NAT_OUT / "v3" / "candidates_us.parquet")
    add_common_args(p)
    p = sub.add_parser("calibrate")
    p.add_argument("--detections", type=Path, default=NAT_OUT / "detections.parquet")
    p.add_argument("--min-conf", type=float, default=0.5)
    p.add_argument("--ref", type=float, default=0.98, help="the national verifier threshold")
    p.add_argument("--floor", type=float, default=0.9, help="never below this")
    p.add_argument("--min-known", type=int, default=150, help="states with fewer known courts keep --ref")
    p.add_argument("--out", type=Path, default=NAT_OUT / "v3" / "verify_thresholds.json")
    add_common_args(p)
    for name in ("select", "review"):
        p = sub.add_parser(name)
        p.add_argument("--candidates", type=Path, default=NAT_OUT / "v3" / "candidates_us.parquet")
        if name == "select":
            p.add_argument("--min-conf", type=float, required=True, help="v3 score (conf3)")
            p.add_argument("--min-verify", type=float, required=True)
            p.add_argument("--verify-col", default="verify_scan", choices=["verify_scan", "verify3"])
            p.add_argument("--thresholds", type=Path, help="per-state verifier thresholds (calibrate output)")
            p.add_argument("--min-conf-v2", type=float, default=0.0)
        else:
            p.add_argument("--name", required=True)
            p.add_argument("--where", default="export", choices=["export", "new", "all"])
            p.add_argument("--v2-exported", type=Path, default=NAT_OUT / "app_ready" / "exported_candidates.parquet")
            p.add_argument("--states", nargs="*")
            p.add_argument("--n", type=int, default=100)
            p.add_argument("--crop-px", type=int, default=160)
            p.add_argument("--thumb-px", type=int, default=320)
            p.add_argument("--seed", type=int, default=11)
            p.add_argument("--out-dir", type=Path, default=NAT_OUT / "v3" / "review")
        add_common_args(p)
    p = sub.add_parser("grid")
    p.add_argument("--candidates", type=Path, default=NAT_OUT / "v3" / "candidates_us.parquet")
    p.add_argument("--rules", nargs="+", required=True, help="conf=0.5,verify=0.98[,col=verify3][,states=PATH][,v2=0.5]")
    p.add_argument("--heldout", nargs="+", default=list(HELDOUT_SOURCES))
    p.add_argument("--area", type=Path, default=V3_DATA / "heldout_scanned_area.geojson")
    p.add_argument("--out", type=Path, default=NAT_OUT / "v3" / "grid.json")
    add_common_args(p)
    p = sub.add_parser("rescore-input")
    p.add_argument("--candidates", type=Path, default=NAT_OUT / "candidates_us.parquet")
    p.add_argument("--out", type=Path, default=V3_DATA / "rescore_input.parquet")
    add_common_args(p)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    if hasattr(args, "out_dir"):
        args.out_dir.mkdir(parents=True, exist_ok=True)
    {"split": cmd_split, "snap": cmd_snap, "layers": cmd_layers, "heldout-items": cmd_heldout_items,
     "heldout-eval": cmd_heldout_eval, "tn-eval": cmd_tn_eval, "merge-rescore": cmd_merge_rescore,
     "calibrate": cmd_calibrate, "select": cmd_select, "review": cmd_review,
     "rescore-input": cmd_rescore_input, "grid": cmd_grid}[args.cmd](args)


if __name__ == "__main__":
    main()
