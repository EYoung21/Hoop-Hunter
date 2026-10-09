"""Nationwide YOLO court dataset from the OSM extract (osm_extract) and NAIP (naip_index).

Positives: one chip per labelable OSM court (outdoor, court-like polygon of
60-6,000 m^2), randomly offset so the court is not always centred but stays
whole inside the chip.

Negatives (about ``--neg-ratio`` x positives, per state): chips centred on
hard negatives (tennis and other non-basketball pitches, parking lots, pools)
and on random points inside parks and schools that are at least
``--min-court-dist`` metres from any known basketball feature.

Labels: every chip, positive or negative, gets a box for **every** labelable
court whose box intersects it. Boxes are axis-aligned in the chip's pixel
grid, clipped at the chip edge; slivers (visible fraction below
``--min-visible`` or a side under ``--min-box-px``) are dropped. A chip that
shows a court we can't box (a node-only court, an oversized court polygon, or
a park/school feature tagged basketball whose court location is unknown) is
discarded, because it would teach the model that a court is background.
Chip georeferencing comes from the NAIP catalog's ``proj:transform``, so all
labels are computed before any imagery is fetched.

Split: chips whose feature is in ``--val-state`` (default TN) form ``val``,
the honest held-out state. Other chips whose box comes within
``--holdout-buffer-m`` of that state are dropped so no court appears on both
sides. A small spatial slice (``--val-us-frac`` of ~5 km cells) of the rest
becomes ``val_us`` for an in-distribution comparison; everything else is
``train``.

Imagery: chips are 320 x 320 px at 0.6 m (0.3 m states are read from the
0.6 m overview), JPEG quality 95, read with ``GroupedReader`` (one COG open
per group of windows, thread pool, retries with backoff).

Outputs (``--out``, default datasets/us): images/{train,val,val_us}/*.jpg,
labels/..., data.yaml (val = held-out state), data_val_us.yaml,
manifest.csv.gz, stats.json, previews/.

Example
-------
    python -m hoop_pipeline.build_dataset_us --out datasets/us --workers 64
    python -m hoop_pipeline.build_dataset_us --out datasets/us_pilot --pilot 2000 --overwrite
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import logging
import math
import random
import shutil
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from PIL import Image, ImageDraw
from pyproj import Transformer
from shapely import STRtree

from . import config
from .common import M_PER_DEG_LAT, add_common_args, setup_logging, write_json
from .geoparquet import read_geoparquet
from .naip_index import GroupedReader, NaipCatalog, WindowPlan, plan_windows
from .states import States

log = logging.getLogger("build_dataset_us")

ALBERS = "EPSG:5070"  # CONUS equal-area metres, for distance rules
DEFAULT_NEG_MIX = "pitch_tennis=0.30,pitch_other=0.10,parking=0.20,pool=0.10,park=0.15,school=0.15"
RANDOM_POINT_KINDS = {"park", "school"}
LABEL_MAX_AREA_M2 = 6000.0


# --------------------------------------------------------------------------- courts
@dataclass
class CourtLayer:
    ref: np.ndarray
    state: np.ndarray
    lon: np.ndarray
    lat: np.ndarray
    geoms: np.ndarray            # lon/lat
    role: np.ndarray             # "label" | "ambiguous" | "ignore"
    half_extent_m: np.ndarray
    tree: STRtree = field(repr=False)


def load_court_layer(path: Path, container_max_area_m2: float) -> CourtLayer:
    a, g = read_geoparquet(path, columns=["osm_ref", "state", "center_lon", "center_lat", "area_m2", "geom_kind",
                                          "outdoor", "court_like", "label_ok"])
    outdoor = a["outdoor"].astype(bool)
    clike = a["court_like"].astype(bool)
    label = a["label_ok"].astype(bool)
    poly = a["geom_kind"] == "polygon"
    area = np.nan_to_num(a["area_m2"].astype(float), nan=0.0)
    amb = outdoor & ~label & (
        (clike & poly & (area > LABEL_MAX_AREA_M2))          # multi-court complex / mis-tagged area
        | (clike & ~poly)                                     # node-only or unclosed-way court
        | (~clike & poly & (area <= container_max_area_m2))   # small park/school "has a court somewhere"
    )
    role = np.where(label, "label", np.where(amb, "ambiguous", "ignore")).astype(object)
    b = shapely.bounds(g)
    lat = a["center_lat"].astype(float)
    half = np.maximum((b[:, 2] - b[:, 0]) * M_PER_DEG_LAT * np.cos(np.radians(lat)), (b[:, 3] - b[:, 1]) * M_PER_DEG_LAT) / 2
    log.info("courts: %d features; roles %s", len(role), dict(Counter(role.tolist())))
    return CourtLayer(a["osm_ref"], a["state"], a["center_lon"].astype(float), lat, g, role, half, STRtree(g))


def to_albers(geoms: np.ndarray) -> np.ndarray:
    tr = Transformer.from_crs("EPSG:4326", ALBERS, always_xy=True)
    return shapely.transform(geoms, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))


def lonlat_boxes(lons: np.ndarray, lats: np.ndarray, half_m: float) -> np.ndarray:
    dlat = half_m / M_PER_DEG_LAT
    dlon = half_m / (M_PER_DEG_LAT * np.cos(np.radians(lats)))
    return shapely.box(lons - dlon, lats - dlat, lons + dlon, lats + dlat)


def offset(lons: np.ndarray, lats: np.ndarray, dx: np.ndarray, dy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return lons + dx / (M_PER_DEG_LAT * np.cos(np.radians(lats))), lats + dy / M_PER_DEG_LAT


# --------------------------------------------------------------------------- sampling
@dataclass
class Plan:
    name: list[str] = field(default_factory=list)
    kind: list[str] = field(default_factory=list)          # "pos" | "neg"
    center_kind: list[str] = field(default_factory=list)   # "court" | negative kind
    state: list[str] = field(default_factory=list)
    lon: list[float] = field(default_factory=list)
    lat: list[float] = field(default_factory=list)
    source: list[str] = field(default_factory=list)

    def add(self, name: str, kind: str, ckind: str, state: str, lon: float, lat: float, source: str) -> None:
        self.name.append(name)
        self.kind.append(kind)
        self.center_kind.append(ckind)
        self.state.append(state)
        self.lon.append(float(lon))
        self.lat.append(float(lat))
        self.source.append(source)

    def __len__(self) -> int:
        return len(self.name)


def plan_positives(courts: CourtLayer, plan: Plan, chip_half_m: float, jitter: float, max_positives: int,
                   rng: np.random.Generator) -> Counter[str]:
    usable = np.flatnonzero((courts.role == "label") & (courts.half_extent_m < 0.8 * chip_half_m))
    if max_positives and len(usable) > max_positives:
        usable = np.sort(rng.choice(usable, max_positives, replace=False))
    room = np.maximum(0.0, chip_half_m - courts.half_extent_m[usable] - 8.0) * jitter
    dx = rng.uniform(-1, 1, len(usable)) * room
    dy = rng.uniform(-1, 1, len(usable)) * room
    lons, lats = offset(courts.lon[usable], courts.lat[usable], dx, dy)
    per_state: Counter[str] = Counter()
    for k, i in enumerate(usable):
        ref = str(courts.ref[i])
        st = str(courts.state[i])
        plan.add(f"{st}_pos_{ref.replace('/', '')}", "pos", "court", st, lons[k], lats[k], ref)
        per_state[st] += 1
    log.info("positives: %d of %d labelable courts", len(usable), int((courts.role == "label").sum()))
    return per_state


def _random_point_in(geom: Any, rng: random.Random, tries: int = 40) -> tuple[float, float] | None:
    if geom is None or geom.geom_type == "Point":
        return None
    minx, miny, maxx, maxy = geom.bounds
    for _ in range(tries):
        x, y = rng.uniform(minx, maxx), rng.uniform(miny, maxy)
        if shapely.intersects_xy(geom, x, y):
            return x, y
    return None


def plan_negatives(
    context_path: Path, courts: CourtLayer, plan: Plan, pos_per_state: Counter[str], neg_ratio: float,
    mix: dict[str, float], min_court_dist_m: float, hard_min_dist_m: float, seed: int,
) -> Counter[str]:
    kinds = sorted(mix)
    a, g = read_geoparquet(context_path, columns=["kind", "osm_type", "osm_id", "state", "center_lon", "center_lat",
                                                  "area_m2", "geom_kind"], filters=[("kind", "in", kinds)])
    area = a["area_m2"].astype(float)
    ok = np.ones(len(area), dtype=bool)
    small_pitch = np.isin(a["kind"], ["pitch_tennis", "pitch_other"]) & ~(area > LABEL_MAX_AREA_M2)
    ok &= ~np.isin(a["kind"], ["pitch_tennis", "pitch_other"]) | small_pitch
    ok &= ~((a["kind"] == "parking") & ((area < 150) | (area > 60_000)))
    ok &= ~((a["kind"] == "park") & (area > 5e6))  # skip huge wilderness parks for random points
    log.info("context candidates: %s", dict(Counter(a["kind"][ok].tolist())))

    court_albers = to_albers(courts.geoms)
    court_tree = STRtree(court_albers)
    tr = Transformer.from_crs("EPSG:4326", ALBERS, always_xy=True)
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    by_state_kind: dict[tuple[str, str], np.ndarray] = {}
    st_arr = a["state"]
    kind_arr = a["kind"]
    order = np.lexsort((kind_arr.astype(str), st_arr.astype(str)))
    order = order[ok[order]]
    keys = list(zip(st_arr[order].tolist(), kind_arr[order].tolist()))
    start = 0
    for k in range(1, len(order) + 1):
        if k == len(order) or keys[k] != keys[start]:
            by_state_kind[keys[start]] = order[start:k]
            start = k

    made: Counter[str] = Counter()
    for st, n_pos in sorted(pos_per_state.items()):
        want = int(round(n_pos * neg_ratio))
        if want <= 0:
            continue
        avail = {kd: len(by_state_kind.get((st, kd), ())) for kd in kinds}
        quota = {kd: int(round(want * mix[kd])) for kd in kinds}
        # hand the quota of kinds this state lacks to the kinds it has
        short = sum(max(0, quota[kd] - avail[kd]) for kd in kinds)
        for kd in kinds:
            quota[kd] = min(quota[kd], avail[kd])
        spare = [kd for kd in kinds if avail[kd] > quota[kd]]
        while short > 0 and spare:
            for kd in list(spare):
                if short <= 0:
                    break
                if avail[kd] > quota[kd]:
                    quota[kd] += 1
                    short -= 1
                else:
                    spare.remove(kd)
        for kd in kinds:
            pool = by_state_kind.get((st, kd))
            if pool is None or quota[kd] <= 0:
                continue
            picks = nrng.choice(pool, min(len(pool), quota[kd] * 2), replace=False)
            lons: list[float] = []
            lats: list[float] = []
            srcs: list[str] = []
            for i in picks:
                if kd in RANDOM_POINT_KINDS:
                    pt = _random_point_in(g[i], rng)
                    if pt is None:  # node or degenerate: random spot within 80 m of it
                        ang, dist = rng.uniform(0, 2 * math.pi), rng.uniform(0, 80)
                        lo, la = offset(np.array([a["center_lon"][i]]), np.array([a["center_lat"][i]]),
                                        np.array([dist * math.cos(ang)]), np.array([dist * math.sin(ang)]))
                        pt = (float(lo[0]), float(la[0]))
                else:
                    lo, la = offset(np.array([a["center_lon"][i]]), np.array([a["center_lat"][i]]),
                                    np.array([rng.uniform(-30, 30)]), np.array([rng.uniform(-30, 30)]))
                    pt = (float(lo[0]), float(la[0]))
                lons.append(pt[0])
                lats.append(pt[1])
                srcs.append(f"{a['osm_type'][i]}/{a['osm_id'][i]}")
            if not lons:
                continue
            x, y = tr.transform(np.array(lons), np.array(lats))
            pts = shapely.points(x, y)
            dist = min_court_dist_m if kd in RANDOM_POINT_KINDS else hard_min_dist_m
            near_i, _ = court_tree.query(pts, predicate="dwithin", distance=dist)
            bad = np.zeros(len(pts), dtype=bool)
            bad[near_i] = True
            taken = 0
            for j in np.flatnonzero(~bad):
                if taken >= quota[kd]:
                    break
                plan.add(f"{st}_neg_{kd}_{len(plan):07d}", "neg", kd, st, lons[j], lats[j], srcs[j])
                taken += 1
            made[f"{st}:{kd}"] += taken
    by_kind: Counter[str] = Counter()
    for key, v in made.items():
        by_kind[key.split(":", 1)[1]] += v
    log.info("negatives planned: %d %s", sum(by_kind.values()), dict(by_kind))
    return by_kind


# --------------------------------------------------------------------------- labels
@dataclass
class LabelResult:
    boxes: list[list[tuple[float, float, float, float]]]
    refs: list[list[str]]
    ambiguous: np.ndarray


def compute_labels(wp: WindowPlan, lons: np.ndarray, lats: np.ndarray, courts: CourtLayer, gsd: float,
                   min_visible: float, min_box_px: float, point_pad_m: float = 15.0) -> LabelResult:
    n = len(lons)
    S = float(wp.out_size)
    boxes: list[list[tuple[float, float, float, float]]] = [[] for _ in range(n)]
    refs: list[list[str]] = [[] for _ in range(n)]
    ambiguous = np.zeros(n, dtype=bool)
    q = lonlat_boxes(lons, lats, wp.out_size * gsd / 2 + point_pad_m + 10)
    ci, cj = courts.tree.query(q, predicate="intersects")
    keep = (courts.role[cj] != "ignore") & (wp.item[ci] >= 0)
    ci, cj = ci[keep], cj[keep]
    if not len(ci):
        return LabelResult(boxes, refs, ambiguous)
    xmin = np.empty(len(ci))
    xmax = np.empty(len(ci))
    ymin = np.empty(len(ci))
    ymax = np.empty(len(ci))
    pair_epsg = wp.epsg[ci]
    for code in np.unique(pair_epsg):
        sel = pair_epsg == code
        uniq = np.unique(cj[sel])
        coords, gidx = shapely.get_coordinates(courts.geoms[uniq], return_index=True)
        tr = Transformer.from_crs("EPSG:4326", f"EPSG:{int(code)}", always_xy=True)
        x, y = tr.transform(coords[:, 0], coords[:, 1])
        bx0 = np.full(len(uniq), np.inf)
        bx1 = np.full(len(uniq), -np.inf)
        by0 = np.full(len(uniq), np.inf)
        by1 = np.full(len(uniq), -np.inf)
        np.minimum.at(bx0, gidx, x)
        np.maximum.at(bx1, gidx, x)
        np.minimum.at(by0, gidx, y)
        np.maximum.at(by1, gidx, y)
        pos = np.searchsorted(uniq, cj[sel])
        xmin[sel], xmax[sel], ymin[sel], ymax[sel] = bx0[pos], bx1[pos], by0[pos], by1[pos]
    tf = wp.chip_transform[ci]
    sx, X0, sy, Y0 = tf[:, 0], tf[:, 2], tf[:, 4], tf[:, 5]
    c0, c1 = (xmin - X0) / sx, (xmax - X0) / sx
    r0, r1 = (ymax - Y0) / sy, (ymin - Y0) / sy
    full_w, full_h = c1 - c0, r1 - r0
    vc0, vc1 = np.clip(c0, 0, S), np.clip(c1, 0, S)
    vr0, vr1 = np.clip(r0, 0, S), np.clip(r1, 0, S)
    vis_w = np.maximum(0.0, vc1 - vc0)
    vis_h = np.maximum(0.0, vr1 - vr0)
    role = courts.role[cj]
    is_point = (full_w * full_h) < 1e-6
    pad_px = point_pad_m / gsd
    point_in = is_point & (c0 > -pad_px) & (c0 < S + pad_px) & (r0 > -pad_px) & (r0 < S + pad_px)
    amb_hit = (role == "ambiguous") & (((vis_w > 0) & (vis_h > 0) & ~is_point) | point_in)
    ambiguous[np.unique(ci[amb_hit])] = True
    visible = vis_w * vis_h / np.maximum(full_w * full_h, 1e-6)
    lab = (role == "label") & (visible >= min_visible) & (vis_w >= min_box_px) & (vis_h >= min_box_px)
    for k in np.flatnonzero(lab):
        i = ci[k]
        boxes[i].append((float((vc0[k] + vc1[k]) / 2 / S), float((vr0[k] + vr1[k]) / 2 / S),
                         float(vis_w[k] / S), float(vis_h[k] / S)))
        refs[i].append(str(courts.ref[cj[k]]))
    return LabelResult(boxes, refs, ambiguous)


def cell_split(lat: float, lon: float, frac: float, seed: int, cell_deg: float = 0.05) -> bool:
    key = f"{seed}:{math.floor(lat / cell_deg)}:{math.floor(lon / cell_deg)}"
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < frac


def draw_box_image(img: Image.Image, boxes: list[tuple[float, float, float, float]], color: tuple[int, int, int]) -> Image.Image:
    im = img.convert("RGB")
    w, h = im.size
    d = ImageDraw.Draw(im)
    for xc, yc, bw, bh in boxes:
        d.rectangle([(xc - bw / 2) * w, (yc - bh / 2) * h, (xc + bw / 2) * w, (yc + bh / 2) * h], outline=color, width=2)
    return im


def montage(images: Sequence[Image.Image], cols: int, path: Path, tile: int = 320, captions: list[str] | None = None) -> None:
    rows = max(1, math.ceil(len(images) / cols))
    sheet = Image.new("RGB", (cols * tile, rows * tile), "white")
    d = ImageDraw.Draw(sheet)
    for k, im in enumerate(images):
        x, y = (k % cols) * tile, (k // cols) * tile
        sheet.paste(im.resize((tile, tile)), (x, y))
        if captions:
            d.rectangle([x, y, x + tile, y + 14], fill=(0, 0, 0))
            d.text((x + 3, y + 1), captions[k][:60], fill=(255, 255, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, quality=90)


# --------------------------------------------------------------------------- main
def parse_mix(text: str) -> dict[str, float]:
    mix = {}
    for part in text.split(","):
        k, v = part.split("=")
        mix[k.strip()] = float(v)
    tot = sum(mix.values())
    return {k: v / tot for k, v in mix.items() if v > 0}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--courts", type=Path, default=config.OSM_US_DIR / "courts_us.parquet")
    ap.add_argument("--context", type=Path, default=config.OSM_US_DIR / "context_us.parquet")
    ap.add_argument("--states-file", type=Path, default=config.STATES_FILE)
    ap.add_argument("--out", type=Path, default=config.DATASETS_DIR / "us")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--resume", action="store_true", help="keep existing chips in --out and only fetch missing ones")
    ap.add_argument("--val-state", default="TN", help="held-out validation state")
    ap.add_argument("--holdout-buffer-m", type=float, default=250.0)
    ap.add_argument("--val-us-frac", type=float, default=0.02, help="fraction of ~5 km cells outside the val state -> val_us")
    ap.add_argument("--max-positives", type=int, default=0, help="0 = every labelable court")
    ap.add_argument("--neg-ratio", type=float, default=1.0)
    ap.add_argument("--neg-mix", default=DEFAULT_NEG_MIX)
    ap.add_argument("--min-court-dist", type=float, default=60.0, help="m; park/school random points this far from courts")
    ap.add_argument("--hard-min-dist", type=float, default=20.0, help="m; hard-negative centres this far from courts")
    ap.add_argument("--container-max-area", type=float, default=200_000.0,
                    help="m^2; park/school features tagged basketball up to this size make chips ambiguous")
    ap.add_argument("--chip-size", type=int, default=config.CHIP_SIZE_PX)
    ap.add_argument("--gsd", type=float, default=config.TARGET_GSD_M)
    ap.add_argument("--jitter", type=float, default=0.8)
    ap.add_argument("--min-visible", type=float, default=0.35)
    ap.add_argument("--min-box-px", type=float, default=6.0)
    ap.add_argument("--min-valid", type=float, default=0.98)
    ap.add_argument("--min-year", type=int, default=config.NAIP_MIN_YEAR)
    ap.add_argument("--naip-dir", type=Path, default=config.NAIP_GEOPARQUET_DIR, help="NAIP GeoParquet cache")
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--jpeg-quality", type=int, default=95)
    ap.add_argument("--pilot", type=int, default=0, help="only fetch a random sample of N planned chips")
    ap.add_argument("--previews", type=int, default=48)
    ap.add_argument("--seed", type=int, default=0)
    add_common_args(ap)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    t0 = time.monotonic()
    rng = np.random.default_rng(args.seed)
    out: Path = args.out
    if out.exists() and any(out.iterdir()) and not args.resume:
        if not args.overwrite:
            raise SystemExit(f"{out} exists; pass --overwrite (rebuild) or --resume")
        shutil.rmtree(out)
    for sub in ("images/train", "images/val", "images/val_us", "labels/train", "labels/val", "labels/val_us"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    chip_half_m = args.chip_size * args.gsd / 2
    courts = load_court_layer(args.courts, args.container_max_area)
    plan = Plan()
    pos_per_state = plan_positives(courts, plan, chip_half_m, args.jitter, args.max_positives, rng)
    neg_by_kind = plan_negatives(args.context, courts, plan, pos_per_state, args.neg_ratio, parse_mix(args.neg_mix),
                                 args.min_court_dist, args.hard_min_dist, args.seed)
    lons = np.array(plan.lon)
    lats = np.array(plan.lat)
    st = np.array(plan.state, dtype=object)
    kind = np.array(plan.kind, dtype=object)
    names = np.array(plan.name, dtype=object)
    n = len(plan)
    skipped: Counter[str] = Counter()

    cat = NaipCatalog.load(args.naip_dir, min_year=args.min_year)
    item = cat.select(lons, lats, st, half_m=chip_half_m)
    skipped["no_naip"] = int((item < 0).sum())
    wp = plan_windows(cat, item, lons, lats, args.chip_size, args.gsd)
    labels = compute_labels(wp, lons, lats, courts, args.gsd, args.min_visible, args.min_box_px)
    keep = item >= 0
    skipped["ambiguous_court_in_chip"] = int((labels.ambiguous & keep).sum())
    keep &= ~labels.ambiguous
    n_lab = np.array([len(b) for b in labels.boxes])
    lost_target = keep & (kind == "pos") & (n_lab == 0)
    skipped["positive_target_not_boxed"] = int(lost_target.sum())
    keep &= ~lost_target

    # held-out state: val; buffer around it dropped from train
    states = States.load(args.states_file)
    hold = states.geometry(args.val_state)
    hold_buf = shapely.buffer(hold, args.holdout_buffer_m / M_PER_DEG_LAT)
    shapely.prepare(hold_buf)
    in_val = st == args.val_state.upper()
    boxes = lonlat_boxes(lons, lats, chip_half_m)
    near_hold = ~in_val & shapely.intersects(hold_buf, boxes)
    skipped["near_holdout_border"] = int((near_hold & keep).sum())
    keep &= ~near_hold
    split = np.where(in_val, "val", "train").astype(object)
    for i in np.flatnonzero(~in_val):
        if cell_split(lats[i], lons[i], args.val_us_frac, args.seed):
            split[i] = "val_us"

    idx = np.flatnonzero(keep)
    if args.pilot and len(idx) > args.pilot:
        idx = np.sort(rng.choice(idx, args.pilot, replace=False))
        log.info("pilot: fetching %d of %d planned chips", len(idx), int(keep.sum()))
    label_text = ["".join(f"0 {xc:.6f} {yc:.6f} {bw:.6f} {bh:.6f}\n" for xc, yc, bw, bh in labels.boxes[i]) for i in range(n)]
    log.info("planned %d chips (%d pos, %d neg) -> fetching %d; skipped so far %s", n, int((kind == "pos").sum()),
             int((kind == "neg").sum()), len(idx), dict(skipped))

    status = np.full(n, "", dtype=object)
    valid_frac = np.zeros(n)
    to_fetch = []
    for i in idx:
        img_path = out / "images" / split[i] / f"{names[i]}.jpg"
        if args.resume and img_path.exists() and (out / "labels" / split[i] / f"{names[i]}.txt").exists():
            status[i] = "ok"
            valid_frac[i] = 1.0
        else:
            to_fetch.append(i)
    to_fetch_arr = np.array(to_fetch, dtype=np.int64)
    fetch_plan = WindowPlan(item=wp.item[to_fetch_arr], col_off=wp.col_off[to_fetch_arr], row_off=wp.row_off[to_fetch_arr],
                     win=wp.win[to_fetch_arr], out_size=wp.out_size, chip_transform=wp.chip_transform[to_fetch_arr],
                     epsg=wp.epsg[to_fetch_arr])

    def handle(key: Any, img: np.ndarray | None, err: str) -> tuple[int, str, float, str]:
        i = int(key)
        if img is None:
            return i, "read_error", 0.0, err
        vf = float((img.max(axis=2) > 0).mean())
        if vf < args.min_valid:
            return i, "partial_imagery", vf, ""
        Image.fromarray(img).save(out / "images" / split[i] / f"{names[i]}.jpg", quality=args.jpeg_quality)
        with open(out / "labels" / split[i] / f"{names[i]}.txt", "w", encoding="utf-8", newline="\n") as fh:
            fh.write(label_text[i])
        return i, "ok", vf, ""

    reader = GroupedReader(cat, workers=args.workers)
    errors: Counter[str] = Counter()
    t_read = time.monotonic()
    for res in reader.run(fetch_plan, to_fetch_arr, handle, progress_every=max(1000, len(to_fetch_arr) // 40)):
        if res is None:
            continue
        i, s, vf, err = res
        status[i] = s
        valid_frac[i] = vf
        if err:
            errors[err.split(":")[0][:80]] += 1
    read_s = time.monotonic() - t_read
    for s, c in Counter(status[idx].tolist()).items():
        if s != "ok":
            skipped[s or "unknown"] += c
    if errors:
        log.warning("read errors: %s", dict(errors.most_common(10)))

    ok_idx = np.array([i for i in idx if status[i] == "ok"], dtype=np.int64)
    # ---- manifest + stats
    with gzip.open(out / "manifest.csv.gz", "wt", encoding="utf-8", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["name", "split", "kind", "center_kind", "state", "lat", "lon", "source", "n_labels", "label_refs",
                     "naip_item", "naip_year", "naip_date", "native_gsd", "valid"])
        for i in ok_idx:
            it = int(item[i])
            wr.writerow([names[i], split[i], kind[i], plan.center_kind[i], st[i], f"{lats[i]:.6f}", f"{lons[i]:.6f}",
                         plan.source[i], len(labels.boxes[i]), ";".join(labels.refs[i]), cat.ids[it], int(cat.years[it]),
                         cat.date_str(it), float(cat.gsd[it]), round(float(valid_frac[i]), 4)])
    stats: dict[str, Any] = {"splits": {}}
    for sp in ("train", "val", "val_us"):
        m = ok_idx[split[ok_idx] == sp]
        stats["splits"][sp] = {
            "images": len(m),
            "positive_chips": int((kind[m] == "pos").sum()),
            "negative_chips": int((kind[m] == "neg").sum()),
            "chips_with_boxes": int((n_lab[m] > 0).sum()),
            "background_only_chips": int((n_lab[m] == 0).sum()),
            "boxes": int(n_lab[m].sum()),
            "negative_centre_kinds": dict(Counter(np.array(plan.center_kind, dtype=object)[m][kind[m] == "neg"].tolist())),
        }
    years_by_state: dict[str, Counter[int]] = defaultdict(Counter)
    for i in ok_idx:
        years_by_state[str(st[i])][int(cat.years[int(item[i])])] += 1
    stats["naip_years_by_state"] = {s: dict(sorted(c.items())) for s, c in sorted(years_by_state.items())}
    stats["naip_year_used_by_state"] = {s: max(c, key=lambda y: c[y]) for s, c in sorted(years_by_state.items())}
    stats["planned"] = {"chips": n, "positives": int((kind == "pos").sum()), "negatives": int((kind == "neg").sum()),
                        "negatives_by_kind": dict(neg_by_kind)}
    stats["skipped"] = dict(skipped)
    stats["read"] = {"seconds": round(read_s, 1), "chips_per_s": round(len(to_fetch_arr) / max(read_s, 1e-6), 1),
                     "retries": reader.stats.retries, "transform_mismatch": reader.stats.transform_mismatch,
                     "errors": dict(errors.most_common(10))}
    stats["by_state_images"] = dict(Counter(st[ok_idx].tolist()).most_common())
    stats["pilot"] = args.pilot
    stats["seconds"] = round(time.monotonic() - t0, 1)
    write_json(out / "stats.json", stats)
    (out / "data.yaml").write_text(
        f"# Hoop Hunter court detector, nationwide (build_dataset_us); val = held-out {args.val_state}\n"
        f"path: {out.resolve().as_posix()}\ntrain: images/train\nval: images/val\nnames:\n  0: court\n", encoding="utf-8")
    (out / "data_val_us.yaml").write_text(
        f"# same dataset, val = random ~5 km cells outside {args.val_state}\n"
        f"path: {out.resolve().as_posix()}\ntrain: images/train\nval: images/val_us\nnames:\n  0: court\n", encoding="utf-8")

    # ---- previews for eyeballing label alignment
    prng = random.Random(args.seed)
    with_boxes = [i for i in ok_idx if n_lab[i] > 0]
    without = [i for i in ok_idx if n_lab[i] == 0]
    pick = prng.sample(with_boxes, min(len(with_boxes), args.previews * 3 // 4))
    pick += prng.sample(without, min(len(without), args.previews - len(pick)))
    ims, caps = [], []
    for i in pick:
        im = Image.open(out / "images" / split[i] / f"{names[i]}.jpg")
        ims.append(draw_box_image(im, labels.boxes[i], (255, 0, 255)))
        caps.append(f"{names[i]} {cat.date_str(int(item[i]))}")
    for k in range(0, len(ims), 16):
        montage(ims[k:k + 16], 4, out / "previews" / f"labels_{k // 16:02d}.jpg", captions=caps[k:k + 16])
    log.info("dataset %s: %s", out, json.dumps(stats["splits"]))
    log.info("skipped: %s; read %.1f chips/s; total %.1f min", dict(skipped), stats["read"]["chips_per_s"],
             stats["seconds"] / 60)


if __name__ == "__main__":
    main()
