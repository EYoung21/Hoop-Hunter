"""Build a YOLO-format court-detection dataset from OSM court polygons + NAIP chips.

Positives: one chip per OSM court polygon (randomly offset so the court is not
always dead-center). Every known court polygon that falls inside a chip is
labelled, not just the one the chip was cut for. Labels are axis-aligned boxes
of the polygon reprojected into chip pixels (class 0 = "court").

Negatives: chips that contain no known court, sampled from OSM parks, parking
lots, playgrounds and non-basketball pitches (if ``fetch_osm --negatives`` was
run) and from random spots 150-1000 m from known courts (streets, roofs, yards).
Empty label files mark them as background for Ultralytics.

Train/val split is spatial (~1 km cells) so overlapping chips never straddle
the split; ``--val-regions`` instead holds out whole regions.

Examples
--------
    python -m hoop_pipeline.build_dataset --regions training chattanooga \
        --max-positives 60 --neg-ratio 1.0 --out datasets/courts_smoke --overwrite
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import math
import random
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from shapely import STRtree
from shapely.geometry import Point, box, shape
from shapely.geometry.base import BaseGeometry

from . import config
from .common import (
    M_PER_DEG_LAT,
    add_common_args,
    m_per_deg_lon,
    offset_latlon,
    read_json,
    setup_logging,
    write_json,
)
from .naip import Chip, NaipIndex, from_lonlat, read_chip
from .osm_tags import EXCLUDED_LEISURE, is_outdoor  # noqa: F401  (re-exported for older callers)

log = logging.getLogger("build_dataset")


# --------------------------------------------------------------------------- known courts
@dataclass
class Court:
    ref: str                     # "way/123"
    region: str
    lat: float                   # center
    lon: float
    geom: BaseGeometry | None    # lon/lat polygon, None for node-only courts
    area_m2: float | None
    outdoor: bool
    half_extent_m: float         # half of the larger side of the bbox, metres

    @property
    def ambiguous(self) -> bool:
        """Visible court we cannot draw a box for (no polygon) -> never in a chip."""
        return self.outdoor and self.geom is None


def load_courts(region: str) -> list[Court]:
    path = config.osm_path(region)
    courts: list[Court] = []
    for f in read_json(path)["features"]:
        p = f["properties"]
        geom = shape(f["geometry"]) if p.get("has_polygon") else None
        if geom is not None:
            minx, miny, maxx, maxy = geom.bounds
            half = max((maxx - minx) * m_per_deg_lon(p["center_lat"]), (maxy - miny) * M_PER_DEG_LAT) / 2
        else:
            half = 0.0
        courts.append(Court(
            ref=p["osm_ref"], region=region, lat=p["center_lat"], lon=p["center_lon"], geom=geom,
            area_m2=p.get("area_m2"), outdoor=is_outdoor(p.get("tags", {})), half_extent_m=half,
        ))
    return courts


def load_negative_points(region: str) -> list[tuple[float, float, str]]:
    path = config.osm_negatives_path(region)
    if not path.exists():
        return []
    out = []
    for f in read_json(path)["features"]:
        lon, lat = f["geometry"]["coordinates"]
        out.append((lat, lon, f["properties"]["kind"]))
    return out


class CourtIndex:
    def __init__(self, courts: list[Court]) -> None:
        self.courts = courts
        self.geoms = [c.geom if c.geom is not None else Point(c.lon, c.lat) for c in courts]
        self.tree = STRtree(self.geoms)

    def query(self, region_geom: BaseGeometry) -> list[Court]:
        return [self.courts[i] for i in self.tree.query(region_geom, predicate="intersects")]


# --------------------------------------------------------------------------- samples
@dataclass
class Sample:
    name: str
    kind: str          # "pos" | "neg"
    region: str
    lat: float
    lon: float
    source: str        # osm ref for positives, negative kind for negatives
    split: str = ""
    labels: list[tuple[float, float, float, float]] = field(default_factory=list)  # yolo xc,yc,w,h
    label_refs: list[str] = field(default_factory=list)
    chip_meta: dict[str, Any] = field(default_factory=dict)


def chip_lonlat_box(lat: float, lon: float, half_m: float, pad_m: float = 0.0) -> BaseGeometry:
    h = half_m + pad_m
    dlat, dlon = h / M_PER_DEG_LAT, h / m_per_deg_lon(lat)
    return box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def polygon_pixel_bbox(chip: Chip, geom: BaseGeometry) -> tuple[float, float, float, float]:
    """Axis-aligned pixel bbox (x0, y0, x1, y1) of a lon/lat polygon in chip pixels."""
    fwd = from_lonlat(chip.crs)
    inv = ~chip.transform
    polys = getattr(geom, "geoms", [geom])
    xs: list[float] = []
    ys: list[float] = []
    for poly in polys:
        lons, lats = poly.exterior.coords.xy
        px, py = fwd.transform(list(lons), list(lats))
        for x, y in zip(px, py):
            c, r = inv * (x, y)
            xs.append(c)
            ys.append(r)
    return min(xs), min(ys), max(xs), max(ys)


def label_chip(
    chip: Chip, sample: Sample, index: CourtIndex, min_visible: float, min_box_px: float,
) -> bool:
    """Fill sample.labels from all known courts inside the chip. False = discard chip."""
    w, h = chip.size
    half_m = w * chip.gsd / 2
    nearby = index.query(chip_lonlat_box(sample.lat, sample.lon, half_m, pad_m=5))
    for court in nearby:
        if not court.outdoor:
            continue  # arenas / indoor courts: roof is legitimately background
        if court.geom is None:
            return False  # visible court we can't draw a box for (node only) -> drop the chip
        x0, y0, x1, y1 = polygon_pixel_bbox(chip, court.geom)
        full = max(1e-6, (x1 - x0) * (y1 - y0))
        cx0, cy0, cx1, cy1 = max(0.0, x0), max(0.0, y0), min(float(w), x1), min(float(h), y1)
        if cx1 <= cx0 or cy1 <= cy0:
            continue
        visible = (cx1 - cx0) * (cy1 - cy0) / full
        if sample.kind == "neg":
            return False  # a known court in a negative chip -> discard
        if visible < min_visible:
            if visible > 0.15:
                return False  # big partial court: ambiguous either way, drop the chip
            continue
        if (cx1 - cx0) < min_box_px or (cy1 - cy0) < min_box_px:
            continue
        sample.labels.append(((cx0 + cx1) / 2 / w, (cy0 + cy1) / 2 / h, (cx1 - cx0) / w, (cy1 - cy0) / h))
        sample.label_refs.append(court.ref)
    return sample.kind == "neg" or bool(sample.labels)


def spatial_split(lat: float, lon: float, val_frac: float, seed: int, cell_deg: float = 0.01) -> str:
    key = f"{seed}:{math.floor(lat / cell_deg)}:{math.floor(lon / cell_deg)}"
    hv = int(hashlib.md5(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "val" if hv < val_frac else "train"


# --------------------------------------------------------------------------- planning
def plan_positives(
    courts_by_region: dict[str, list[Court]], max_positives: int, chips_per_court: int,
    min_area: float, max_area: float, chip_half_m: float, jitter: float, rng: random.Random,
) -> list[Sample]:
    queues: dict[str, list[Court]] = {}
    for region, courts in courts_by_region.items():
        usable = [
            c for c in courts
            if c.outdoor and c.geom is not None and c.area_m2 is not None and min_area <= c.area_m2 <= max_area
            and c.half_extent_m < chip_half_m * 0.8
        ]
        rng.shuffle(usable)
        queues[region] = usable
        log.info("%s: %d known courts, %d usable as positive labels", region, len(courts), len(usable))
    # round-robin across regions so a small cap still sees every region
    out: list[Sample] = []
    while len(out) < max_positives and any(queues.values()):
        for region, q in queues.items():
            if not q or len(out) >= max_positives:
                continue
            c = q.pop()
            for k in range(chips_per_court):
                room = max(0.0, chip_half_m - c.half_extent_m - 8.0) * jitter
                dx, dy = rng.uniform(-room, room), rng.uniform(-room, room)
                lat, lon = offset_latlon(c.lat, c.lon, dx, dy)
                name = f"{region}_pos_{c.ref.replace('/', '')}_{k}"
                out.append(Sample(name, "pos", region, lat, lon, c.ref))
    return out[:max_positives] if chips_per_court == 1 else out


def plan_negatives(
    courts_by_region: dict[str, list[Court]], n_total: int, index: CourtIndex, chip_half_m: float,
    rng: random.Random,
) -> list[Sample]:
    regions = list(courts_by_region)
    per_region = max(1, math.ceil(n_total / max(1, len(regions))))
    out: list[Sample] = []
    for region in regions:
        courts = [c for c in courts_by_region[region]]
        osm_negs = load_negative_points(region)
        rng.shuffle(osm_negs)
        want_osm = per_region // 2 if osm_negs else 0
        cands: list[tuple[float, float, str]] = []
        for lat, lon, kind in osm_negs:
            if len(cands) >= want_osm:
                break
            dx, dy = rng.uniform(-60, 60), rng.uniform(-60, 60)
            la, lo = offset_latlon(lat, lon, dx, dy)
            cands.append((la, lo, kind))
        tries = 0
        while len(cands) < per_region * 3 and courts and tries < per_region * 20:
            tries += 1
            c = rng.choice(courts)
            dist, ang = rng.uniform(150, 1000), rng.uniform(0, 2 * math.pi)
            la, lo = offset_latlon(c.lat, c.lon, dist * math.cos(ang), dist * math.sin(ang))
            cands.append((la, lo, "near_court"))
        n = 0
        for la, lo, kind in cands:
            if n >= per_region:
                break
            if index.query(chip_lonlat_box(la, lo, chip_half_m, pad_m=30)):
                continue
            out.append(Sample(f"{region}_neg_{len(out):05d}", "neg", region, la, lo, kind))
            n += 1
    rng.shuffle(out)
    return out[:n_total]


# --------------------------------------------------------------------------- main
def draw_preview(img_path: Path, labels: list[tuple[float, float, float, float]], out_path: Path) -> None:
    im = Image.open(img_path).convert("RGB")
    w, h = im.size
    d = ImageDraw.Draw(im)
    for xc, yc, bw, bh in labels:
        d.rectangle([(xc - bw / 2) * w, (yc - bh / 2) * h, (xc + bw / 2) * w, (yc + bh / 2) * h], outline=(255, 0, 255), width=2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regions", nargs="+", default=["training", "chattanooga"],
                    help="regions/groups whose OSM courts become labels (default: training chattanooga)")
    ap.add_argument("--out", type=Path, default=config.DATASETS_DIR / "courts")
    ap.add_argument("--overwrite", action="store_true", help="delete an existing dataset at --out first")
    ap.add_argument("--max-positives", type=int, default=2000)
    ap.add_argument("--chips-per-court", type=int, default=1, help="random-offset chips per court (augmentation)")
    ap.add_argument("--neg-ratio", type=float, default=1.0, help="negatives per positive")
    ap.add_argument("--max-negatives", type=int, help="hard cap on negatives (default: neg-ratio * positives)")
    ap.add_argument("--chip-size", type=int, default=config.CHIP_SIZE_PX)
    ap.add_argument("--gsd", type=float, default=config.TARGET_GSD_M)
    ap.add_argument("--jitter", type=float, default=0.8, help="0 = court centered, 1 = anywhere it still fits")
    ap.add_argument("--min-area", type=float, default=60.0, help="m^2; smaller OSM polygons are skipped")
    ap.add_argument("--max-area", type=float, default=6000.0, help="m^2; bigger OSM polygons are skipped")
    ap.add_argument("--min-visible", type=float, default=0.5, help="min fraction of a court inside the chip to label it")
    ap.add_argument("--min-box-px", type=float, default=6.0)
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--val-regions", nargs="*", default=[], help="hold out these regions as val instead of a spatial split")
    ap.add_argument("--min-valid", type=float, default=0.98, help="min fraction of chip pixels with imagery")
    ap.add_argument("--workers", type=int, default=6, help="parallel NAIP reads")
    ap.add_argument("--previews", type=int, default=12, help="write N label-overlay previews")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-fetch", action="store_true", help="fail instead of fetching missing OSM region files")
    add_common_args(ap)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    rng = random.Random(args.seed)
    t0 = time.monotonic()

    regions = config.resolve_regions(args.regions)
    val_regions = {r.name for r in config.resolve_regions(args.val_regions)} if args.val_regions else set()
    courts_by_region: dict[str, list[Court]] = {}
    for r in regions:
        if not config.osm_path(r.name).exists():
            if args.no_fetch:
                raise SystemExit(f"missing {config.osm_path(r.name)}; run fetch_osm --regions {r.name}")
            from .fetch_osm import run_region
            log.info("%s: no cached OSM file, fetching", r.name)
            run_region(r, config.OSM_DIR, False, False, None, config.OVERPASS_MAX_TILE_DEG,
                       config.OVERPASS_SLEEP_S, config.OVERPASS_TIMEOUT_S)
            time.sleep(config.OVERPASS_SLEEP_S)
        courts_by_region[r.name] = load_courts(r.name)
    all_courts = [c for cs in courts_by_region.values() for c in cs]
    index = CourtIndex(all_courts)
    chip_half_m = args.chip_size * args.gsd / 2

    positives = plan_positives(courts_by_region, args.max_positives, args.chips_per_court,
                               args.min_area, args.max_area, chip_half_m, args.jitter, rng)
    n_neg = args.max_negatives if args.max_negatives is not None else int(round(len(positives) * args.neg_ratio))
    negatives = plan_negatives(courts_by_region, n_neg, index, chip_half_m, rng) if n_neg > 0 else []
    samples = positives + negatives
    log.info("planned %d positive + %d negative chips", len(positives), len(negatives))

    out: Path = args.out
    if out.exists() and any(out.iterdir()):
        if not args.overwrite:
            raise SystemExit(f"{out} exists; pass --overwrite to rebuild it")
        shutil.rmtree(out)
    for sub in ("images/train", "images/val", "labels/train", "labels/val", "meta"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    naip_indexes: dict[str, NaipIndex] = {r.name: NaipIndex.for_bbox(r.bbox) for r in regions}

    def fetch(sample: Sample) -> tuple[Sample, Chip | None, str]:
        try:
            chip = read_chip(sample.lat, sample.lon, args.chip_size, index=naip_indexes[sample.region],
                             target_gsd=args.gsd)
        except LookupError as exc:
            return sample, None, f"no imagery ({exc})"
        except Exception as exc:  # network hiccups etc. shouldn't kill a long build
            return sample, None, f"{type(exc).__name__}: {exc}"
        return sample, chip, ""

    kept: list[Sample] = []
    skipped: dict[str, int] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch, s) for s in samples]
        for fut in as_completed(futures):
            sample, chip, err = fut.result()
            done += 1
            if chip is None:
                skipped["read_error"] = skipped.get("read_error", 0) + 1
                log.warning("skip %s: %s", sample.name, err)
                continue
            if chip.valid_fraction < args.min_valid:
                skipped["partial_imagery"] = skipped.get("partial_imagery", 0) + 1
                continue
            if not label_chip(chip, sample, index, args.min_visible, args.min_box_px):
                key = "ambiguous_pos" if sample.kind == "pos" else "court_in_negative"
                skipped[key] = skipped.get(key, 0) + 1
                continue
            if sample.region in val_regions:
                sample.split = "val"
            elif val_regions:
                sample.split = "train"
            else:
                sample.split = spatial_split(sample.lat, sample.lon, args.val_frac, args.seed)
            img_path = out / "images" / sample.split / f"{sample.name}.png"
            Image.fromarray(chip.rgb).save(img_path)
            with open(out / "labels" / sample.split / f"{sample.name}.txt", "w", encoding="utf-8", newline="\n") as fh:
                for xc, yc, bw, bh in sample.labels:
                    fh.write(f"0 {xc:.6f} {yc:.6f} {bw:.6f} {bh:.6f}\n")
            sample.chip_meta = chip.metadata()
            write_json(out / "meta" / f"{sample.name}.json", {
                "name": sample.name, "kind": sample.kind, "region": sample.region, "split": sample.split,
                "source": sample.source, "labels_yolo": sample.labels, "label_osm_refs": sample.label_refs,
                "chip": sample.chip_meta,
            })
            kept.append(sample)
            if done % 25 == 0:
                log.info("  %d/%d chips processed (%d kept)", done, len(samples), len(kept))

    # never leave val empty on tiny smoke datasets
    if not val_regions and kept and not any(s.split == "val" for s in kept if s.kind == "pos"):
        for s in [s for s in kept if s.kind == "pos"][: max(1, len(kept) // 10)]:
            for sub, ext in (("images", ".png"), ("labels", ".txt")):
                (out / sub / "train" / f"{s.name}{ext}").rename(out / sub / "val" / f"{s.name}{ext}")
            s.split = "val"

    kept.sort(key=lambda s: s.name)
    with open(out / "manifest.csv", "w", encoding="utf-8", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["name", "split", "kind", "region", "lat", "lon", "source", "n_labels", "naip_item", "naip_date"])
        for s in kept:
            wr.writerow([s.name, s.split, s.kind, s.region, f"{s.lat:.6f}", f"{s.lon:.6f}", s.source,
                         len(s.labels), s.chip_meta.get("item_id", ""), s.chip_meta.get("date", "")])
    (out / "data.yaml").write_text(
        f"# Hoop Hunter court detector dataset (built by hoop_pipeline.build_dataset)\n"
        f"path: {out.resolve().as_posix()}\n"
        f"train: images/train\nval: images/val\n"
        f"names:\n  0: court\n",
        encoding="utf-8",
    )
    # label-overlay previews, spread across regions, for eyeballing alignment
    per_region = {r: [s for s in kept if s.kind == "pos" and s.region == r] for r in courts_by_region}
    picked: list[Sample] = []
    while len(picked) < args.previews and any(per_region.values()):
        for q in per_region.values():
            if q and len(picked) < args.previews:
                picked.append(q.pop(0))
    for s in picked:
        draw_preview(out / "images" / s.split / f"{s.name}.png", s.labels, out / "previews" / f"{s.name}.png")

    stats: dict[str, Any] = {}
    for split in ("train", "val"):
        ss = [s for s in kept if s.split == split]
        stats[split] = {
            "images": len(ss),
            "positives": sum(1 for s in ss if s.kind == "pos"),
            "negatives": sum(1 for s in ss if s.kind == "neg"),
            "boxes": sum(len(s.labels) for s in ss),
        }
    stats["skipped"] = skipped
    stats["by_region"] = {r: sum(1 for s in kept if s.region == r) for r in courts_by_region}
    stats["naip_years"] = sorted({s.chip_meta.get("year") for s in kept})
    stats["seconds"] = round(time.monotonic() - t0, 1)
    write_json(out / "stats.json", stats)
    log.info("dataset at %s: %s", out, stats)


if __name__ == "__main__":
    main()
