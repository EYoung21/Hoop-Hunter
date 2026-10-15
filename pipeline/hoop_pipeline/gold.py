"""Gold test set: random NAIP tiles across CONUS, pre-annotated, for human labelling and scoring.

Precision measured against OSM is misleading (OSM misses courts), so the gold set is a
stratified random sample of 640 px (384 m) tiles that a person labels completely.

``sample``    draws uniform random points in CONUS (area-weighted), measures each point's
              development from USDA CDL 2021 (NLCD developed classes 121-124, 30 m) in the
              384 m window, and keeps ``--per-cell`` tiles per (Census region x stratum):
              urban (>= 30 % medium/high intensity), suburban (>= 20 % any developed) and
              rural. The share of each stratum among all drawn points is kept as its CONUS
              area weight.
``fetch``     reads the 640 px tiles from NAIP (latest own-state flight) as JPEG q92.
``annotate``  cuts every tile into 320 px tiles with the scan's 224 px stride, runs FastDetector
              (same model, input size and merging as the scan engine) and stores boxes >= --conf.
``export``    writes the labelling page's data (``tools/label/data/tasks.js`` + images); see
              tools/label/index.html.
``score``     reads the labels JSON exported by the page and reports precision / recall per
              stratum and CONUS-weighted, at several thresholds, plus the public/home tags.

Every tile records whether it overlaps a training chip (``train_overlap``); scores are given
for all tiles and for tiles without overlap.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import math
import random
import shutil
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from . import config
from .common import add_common_args, read_json, setup_logging, write_json

log = logging.getLogger("gold")
GOLD_DIR = config.DATA_DIR / "gold"
TOOL_DIR = config.PIPELINE_ROOT / "tools" / "label"
REGIONS = {
    "northeast": {"CT", "ME", "MA", "NH", "RI", "VT", "NJ", "NY", "PA"},
    "midwest": {"IL", "IN", "MI", "OH", "WI", "IA", "KS", "MN", "MO", "NE", "ND", "SD"},
    "south": {"DE", "DC", "FL", "GA", "MD", "NC", "SC", "VA", "WV", "AL", "KY", "MS", "TN", "AR", "LA", "OK", "TX"},
    "west": {"AZ", "CO", "ID", "MT", "NV", "NM", "UT", "WY", "CA", "OR", "WA"},
}
STRATA = ("urban", "suburban", "rural")


def region_of(state: str) -> str:
    for r, s in REGIONS.items():
        if state in s:
            return r
    return ""


# --------------------------------------------------------------------------- sample
def cdl_grid(year: int) -> list[tuple[float, float, float, float, str, float, float]]:
    import requests

    stac = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
    body: dict[str, Any] | None = {"collections": ["usda-cdl"], "datetime": f"{year}-01-01T00:00:00Z/{year}-12-31T23:59:59Z",
                                   "query": {"usda_cdl:type": {"eq": "cropland"}}, "limit": 1000}
    url = stac
    items: list[dict[str, Any]] = []
    method = "POST"
    while url:
        r = requests.post(url, json=body, timeout=120) if method == "POST" else requests.get(url, timeout=120)
        r.raise_for_status()
        d = r.json()
        items += d["features"]
        nxt = [ln for ln in d.get("links", []) if ln.get("rel") == "next"]
        if not nxt or not d["features"]:
            break
        url, method, body = nxt[0]["href"], nxt[0].get("method", "GET"), nxt[0].get("body")
    grid = []
    for it in items:
        a, _, c, _, e, f = it["properties"]["proj:transform"][:6]
        h, w = it["properties"]["proj:shape"]
        grid.append((c, f + h * e, c + w * a, f, it["assets"]["cropland"]["href"], a, e))
    return grid


def cmd_sample(args: argparse.Namespace) -> None:
    import rasterio
    import shapely
    from pyproj import Transformer
    from rasterio.windows import Window

    from .naip_index import _signed
    from .states import States

    t0 = time.monotonic()
    states = States.load(args.states_file)
    to_alb = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    to_ll = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)
    conus = shapely.union_all([shapely.transform(g, lambda xy: np.column_stack(to_alb.transform(xy[:, 0], xy[:, 1])))
                               for g in states.geoms])
    shapely.prepare(conus)
    grid = cdl_grid(args.cdl_year)
    log.info("%d CDL tiles", len(grid))
    rng = random.Random(args.seed)
    minx, miny, maxx, maxy = conus.bounds
    pts: list[tuple[float, float]] = []
    while len(pts) < args.draws:
        x, y = rng.uniform(minx, maxx), rng.uniform(miny, maxy)
        if shapely.intersects_xy(conus, x, y):
            pts.append((x, y))
    half = int(round(args.tile_px * args.gsd / 30.0 / 2))  # CDL pixels (30 m) per half window

    def dev(pt: tuple[float, float]) -> tuple[float, float] | None:
        x, y = pt
        for x0, y0, x1, y1, href, a, e in grid:
            if x0 <= x < x1 and y0 <= y < y1:
                col, row = int((x - x0) / a), int((y - y1) / e)
                with rasterio.open(_signed(href)) as src:
                    arr = src.read(1, window=Window(col - half, row - half, 2 * half, 2 * half), boundless=True,
                                   fill_value=0)
                valid = arr > 0
                if valid.mean() < 0.5:
                    return None
                return float(np.isin(arr, (121, 122, 123, 124))[valid].mean()), float(np.isin(arr, (123, 124))[valid].mean())
        return None

    with ThreadPoolExecutor(args.threads) as pool:
        devs = list(pool.map(dev, pts))
    lons, lats = to_ll.transform(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]))
    st = states.assign(lons, lats)
    rows: list[dict[str, Any]] = []
    for i, dv in enumerate(devs):
        if dv is None or not st[i]:
            continue
        any_dev, mid_high = dv
        stratum = "urban" if mid_high >= 0.30 else ("suburban" if any_dev >= 0.20 else "rural")
        rows.append({"lon": float(lons[i]), "lat": float(lats[i]), "state": str(st[i]), "region": region_of(str(st[i])),
                     "stratum": stratum, "developed": round(any_dev, 3), "mid_high": round(mid_high, 3)})
    weights = Counter((r["region"], r["stratum"]) for r in rows)
    total = sum(weights.values())
    picked = []
    for reg in REGIONS:
        for s in STRATA:
            pool_ = [r for r in rows if r["region"] == reg and r["stratum"] == s]
            rng.shuffle(pool_)
            picked += pool_[: args.per_cell]
    for k, r in enumerate(picked):
        r["id"] = f"g{k:03d}_{r['region'][:2]}_{r['stratum'][:3]}"
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "tiles.json", {"tiles": picked, "strata_weights": {f"{a}|{b}": round(c / total, 5)
                                                                         for (a, b), c in sorted(weights.items())},
                                    "draws": len(pts), "classified": len(rows), "cdl_year": args.cdl_year,
                                    "seed": args.seed, "tile_px": args.tile_px, "gsd": args.gsd})
    log.info("picked %d tiles %s from %d classified draws (%.0f s); stratum shares %s", len(picked),
             dict(Counter((r["region"], r["stratum"]) for r in picked)), len(rows), time.monotonic() - t0,
             {s: round(sum(c for (a, b), c in weights.items() if b == s) / total, 4) for s in STRATA})


# --------------------------------------------------------------------------- fetch
def cmd_fetch(args: argparse.Namespace) -> None:
    from PIL import Image

    from .naip_index import GroupedReader, NaipCatalog, plan_windows

    meta = read_json(args.out / "tiles.json")
    tiles = meta["tiles"]
    cat = NaipCatalog.load(args.naip_dir)
    lons = np.array([t["lon"] for t in tiles])
    lats = np.array([t["lat"] for t in tiles])
    item = cat.select(lons, lats, np.array([t["state"] for t in tiles]), half_m=meta["tile_px"] * meta["gsd"] / 2)
    wp = plan_windows(cat, item, lons, lats, meta["tile_px"], meta["gsd"])
    img_dir = args.out / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    def handle(key: Any, img: np.ndarray | None, err: str) -> int:
        i = int(key)
        t = tiles[i]
        if img is None:
            t["error"] = err[:200]
            return i
        t["valid"] = round(float((img.max(axis=2) > 0).mean()), 3)
        Image.fromarray(img).save(img_dir / f"{t['id']}.jpg", quality=92)
        it = int(item[i])
        t.update({"naip_item": str(cat.ids[it]), "naip_date": cat.date_str(it), "naip_gsd": float(cat.gsd[it]),
                  "transform": [float(v) for v in wp.chip_transform[i]], "epsg": int(wp.epsg[i])})
        return i

    list(GroupedReader(cat, workers=32).run(wp, np.arange(len(tiles)), handle))
    write_json(args.out / "tiles.json", meta)
    log.info("fetched %d / %d tiles", sum(1 for t in tiles if "naip_item" in t), len(tiles))


# --------------------------------------------------------------------------- annotate
def detect_big_tiles(det: Any, imgs: np.ndarray, conf: float, tile: int = 320, overlap: int = 96) -> list[np.ndarray]:
    """Run the detector on big tiles exactly as the scan does: 320 px sub-tiles, stride 224, merge."""
    import torch

    from .scan import tile_offsets
    from .scan_engine import merge_boxes

    out = []
    for img in imgs:
        h, w = img.shape[:2]
        offs = [(c, r) for r in tile_offsets(h, tile, tile - overlap) for c in tile_offsets(w, tile, tile - overlap)]
        sub = np.stack([img[r:r + tile, c:c + tile] for c, r in offs])
        d = det.detect(torch.from_numpy(sub), conf=conf).cpu().numpy()
        if not len(d):
            out.append(np.zeros((0, 5), np.float32))
            continue
        ti = d[:, 0].astype(int)
        cs = np.array([offs[k][0] for k in ti])
        rs = np.array([offs[k][1] for k in ti])
        boxes = d[:, 1:5] + np.column_stack([cs, rs, cs, rs])
        edge = ((d[:, 1] <= 2) & (cs > 0)) | ((d[:, 2] <= 2) & (rs > 0)) | ((d[:, 3] >= tile - 2) & (cs + tile < w)) \
            | ((d[:, 4] >= tile - 2) & (rs + tile < h))
        wm = (boxes[:, 2] - boxes[:, 0]) * config.TARGET_GSD_M
        hm = (boxes[:, 3] - boxes[:, 1]) * config.TARGET_GSD_M
        ok = np.flatnonzero((np.minimum(wm, hm) >= 6) & (np.maximum(wm, hm) <= 120))
        keep = ok[merge_boxes(boxes[ok], d[ok, 5], edge[ok].astype(float))]
        out.append(np.column_stack([boxes[keep], d[keep, 5]]).astype(np.float32))
    return out


def cmd_annotate(args: argparse.Namespace) -> None:
    from PIL import Image

    from .fastdet import FastDetector

    meta = read_json(args.out / "tiles.json")
    det = FastDetector(args.weights, imgsz=args.imgsz, tta=args.tta)
    tiles = [t for t in meta["tiles"] if (args.out / "images" / f"{t['id']}.jpg").exists()]
    imgs = np.stack([np.asarray(Image.open(args.out / "images" / f"{t['id']}.jpg").convert("RGB")) for t in tiles])
    res = detect_big_tiles(det, imgs, args.conf)
    preds: dict[str, list[dict[str, Any]]] = {}
    for t, r in zip(tiles, res):
        preds[t["id"]] = [{"id": f"{t['id']}_p{k}", "x0": round(float(b[0]), 1), "y0": round(float(b[1]), 1),
                           "x1": round(float(b[2]), 1), "y1": round(float(b[3]), 1), "conf": round(float(b[4]), 4)}
                          for k, b in enumerate(r)]
    write_json(args.preds_out or args.out / "preds.json", {"weights": str(args.weights), "imgsz": det.imgsz, "tta": args.tta,
                                                            "conf": args.conf,
                                         "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                         "preds": preds})
    n = sum(len(v) for v in preds.values())
    log.info("pre-annotated %d tiles: %d boxes >= %.2f (%d >= 0.5)", len(tiles), n, args.conf,
             sum(1 for v in preds.values() for b in v if b["conf"] >= 0.5))


def train_overlap(tiles: list[dict[str, Any]], manifests: list[Path], half_m: float) -> None:
    """Flag tiles that overlap any training chip (from the dataset manifests, e.g. us2 + us2_extra)."""
    import csv
    import gzip

    import shapely

    from .common import M_PER_DEG_LAT

    lons, lats = [], []
    for manifest in manifests:
        if not manifest.exists():
            log.warning("manifest %s not found; train_overlap ignores it", manifest)
            continue
        with gzip.open(manifest, "rt", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if r["split"] == "train":
                    lons.append(float(r["lon"]))
                    lats.append(float(r["lat"]))
    if not lons:
        return
    lo, la = np.array(lons), np.array(lats)
    chip_half = 96.0
    dlat = chip_half / M_PER_DEG_LAT
    boxes = shapely.box(lo - dlat / np.cos(np.radians(la)), la - dlat, lo + dlat / np.cos(np.radians(la)), la + dlat)
    tree = shapely.STRtree(boxes)
    for t in tiles:
        d = half_m / M_PER_DEG_LAT
        q = shapely.box(t["lon"] - d / math.cos(math.radians(t["lat"])), t["lat"] - d,
                        t["lon"] + d / math.cos(math.radians(t["lat"])), t["lat"] + d)
        t["train_overlap"] = bool(len(tree.query(q, predicate="intersects")))


# --------------------------------------------------------------------------- export to the labelling page
def cmd_export(args: argparse.Namespace) -> None:
    meta = read_json(args.out / "tiles.json")
    preds = read_json(args.out / "preds.json")["preds"] if (args.out / "preds.json").exists() else {}
    tiles = [t for t in meta["tiles"] if (args.out / "images" / f"{t['id']}.jpg").exists()]
    train_overlap(tiles, args.manifest, meta["tile_px"] * meta["gsd"] / 2)
    data = args.tool / "data"
    (data / "images").mkdir(parents=True, exist_ok=True)
    for t in tiles:
        shutil.copy2(args.out / "images" / f"{t['id']}.jpg", data / "images" / f"{t['id']}.jpg")
    tasks = {
        # the browser keeps work per tasks_id: a different set of pre-annotations must get a different id
        "tasks_id": f"gold-v2-{len(tiles)}-" + hashlib.sha1(json.dumps(preds, sort_keys=True).encode()).hexdigest()[:8],
        "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "tile_px": meta["tile_px"], "gsd": meta["gsd"], "strata_weights": meta.get("strata_weights", {}),
        "tiles": [{"id": t["id"], "image": f"data/images/{t['id']}.jpg", "lat": round(t["lat"], 6), "lon": round(t["lon"], 6),
                   "state": t["state"], "region": t["region"], "stratum": t["stratum"], "naip_date": t.get("naip_date"),
                   "train_overlap": t.get("train_overlap"), "preds": preds.get(t["id"], [])} for t in tiles],
    }
    js = "// generated by: python -m hoop_pipeline.gold export\nwindow.HH_TASKS = " + json.dumps(tasks) + ";\n"
    (data / "tasks.js").write_text(js, encoding="utf-8", newline="\n")
    write_json(args.out / "tiles.json", meta)
    log.info("exported %d tiles (%d pre-annotations) to %s", len(tiles), sum(len(t["preds"]) for t in tasks["tiles"]), data)
    if args.zip:
        import zipfile

        args.zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.zip, "w", compression=zipfile.ZIP_STORED) as zf:
            page = args.tool / "index.html"
            if page.exists():  # the zip alone is a complete offline package: unzip anywhere, double-click index.html
                zf.write(page, "hoop_gold_labels/index.html")
            for f in sorted(data.rglob("*")):
                if f.is_file():
                    zf.write(f, "hoop_gold_labels/" + f.relative_to(args.tool).as_posix())
        log.info("wrote %s (unzip anywhere, then double-click hoop_gold_labels/index.html)", args.zip)


# --------------------------------------------------------------------------- score
def cmd_score(args: argparse.Namespace) -> None:
    from .evaluate import greedy_match

    lab = read_json(args.labels)
    tasks = json.loads((args.tool / "data" / "tasks.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";"))
    weights = tasks.get("strata_weights", {})
    by_tile = {t["id"]: t for t in tasks["tiles"]}
    if args.preds:  # score another model's boxes (e.g. v1) against the same human labels
        alt = read_json(args.preds)["preds"]
        for tid, t in by_tile.items():
            t["preds"] = alt.get(tid, [])
    out: dict[str, Any] = {"labels": str(args.labels), "labeler": lab.get("labeler"), "thresholds": {}}
    reviewed = [tid for tid, v in lab["tiles"].items() if v.get("reviewed") and tid in by_tile]
    out["tiles_reviewed"] = len(reviewed)
    for thr in args.thresholds:
        cells: dict[str, Counter[str]] = {}
        for subset in ("all", "no_train_overlap"):
            for tid in reviewed:
                t = by_tile[tid]
                if subset == "no_train_overlap" and t.get("train_overlap"):
                    continue
                gt = [b for b in lab["tiles"][tid].get("boxes", []) if b.get("label", "court") == "court"]
                g = np.array([[b["x0"], b["y0"], b["x1"], b["y1"]] for b in gt]) if gt else np.zeros((0, 4))
                p = [b for b in t["preds"] if b["conf"] >= thr]
                pb = np.array([[b["x0"], b["y0"], b["x1"], b["y1"]] for b in p]) if p else np.zeros((0, 4))
                pc = np.array([b["conf"] for b in p]) if p else np.zeros(0)
                tp, matched = greedy_match(pb, pc, g, 0.3)
                key = f"{subset}|{t['region']}|{t['stratum']}"
                c = cells.setdefault(key, Counter())
                c["tp"] += int(tp.sum())
                c["fp"] += int((~tp).sum())
                c["fn"] += int((~matched).sum())
                c["tiles"] += 1
        res: dict[str, Any] = {}
        for subset in ("all", "no_train_overlap"):
            per_stratum: dict[str, Any] = {}
            for s in ("urban", "suburban", "rural"):
                tot: Counter[str] = Counter()
                for key, c in cells.items():
                    if key.startswith(subset + "|") and key.endswith("|" + s):
                        tot.update(c)
                per_stratum[s] = {"tp": tot["tp"], "fp": tot["fp"], "fn": tot["fn"], "tiles": tot["tiles"],
                                  "precision": round(tot["tp"] / max(1, tot["tp"] + tot["fp"]), 4),
                                  "recall": round(tot["tp"] / max(1, tot["tp"] + tot["fn"]), 4)}
            # CONUS-weighted: per (region, stratum) rates of TP/FP/FN per tile, weighted by area share
            wtp = wfp = wfn = 0.0
            for key, c in cells.items():
                if not key.startswith(subset + "|"):
                    continue
                _, reg, s = key.split("|")
                w = weights.get(f"{reg}|{s}", 0.0)
                wtp += w * c["tp"] / max(1, c["tiles"])
                wfp += w * c["fp"] / max(1, c["tiles"])
                wfn += w * c["fn"] / max(1, c["tiles"])
            res[subset] = {"by_stratum": per_stratum,
                           "conus_weighted": {"precision": round(wtp / max(1e-12, wtp + wfp), 4),
                                              "recall": round(wtp / max(1e-12, wtp + wfn), 4),
                                              "courts_per_tile": round(wtp + wfn, 5)}}
        out["thresholds"][f"{thr:.2f}"] = res
    tags = Counter(b.get("access", "unsure") for tid in reviewed for b in lab["tiles"][tid].get("boxes", [])
                   if b.get("label", "court") == "court")
    out["access_tags"] = dict(tags)
    write_json(args.out_json, out)
    log.info("gold score: %s", json.dumps({k: v for k, v in out.items() if k != "thresholds"}))
    for thr, r in out["thresholds"].items():
        log.info("  conf %s: all %s | no-overlap %s", thr, r["all"]["conus_weighted"], r["no_train_overlap"]["conus_weighted"])


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--out", type=Path, default=GOLD_DIR)
    p.add_argument("--draws", type=int, default=12000)
    p.add_argument("--per-cell", type=int, default=25, help="tiles per (region x stratum); 4 x 3 cells")
    p.add_argument("--tile-px", type=int, default=640)
    p.add_argument("--gsd", type=float, default=config.TARGET_GSD_M)
    p.add_argument("--cdl-year", type=int, default=2021)
    p.add_argument("--threads", type=int, default=48)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--states-file", type=Path, default=config.STATES_FILE)
    add_common_args(p)
    p = sub.add_parser("fetch")
    p.add_argument("--out", type=Path, default=GOLD_DIR)
    p.add_argument("--naip-dir", type=Path, default=config.NAIP_GEOPARQUET_DIR)
    add_common_args(p)
    p = sub.add_parser("annotate")
    p.add_argument("--out", type=Path, default=GOLD_DIR)
    p.add_argument("--weights", type=Path, default=config.DEFAULT_WEIGHTS)
    p.add_argument("--imgsz", type=int)
    p.add_argument("--tta", default="none")
    p.add_argument("--conf", type=float, default=0.15)
    p.add_argument("--preds-out", type=Path, help="default <out>/preds.json (what `export` shows in the page)")
    add_common_args(p)
    p = sub.add_parser("export")
    p.add_argument("--out", type=Path, default=GOLD_DIR)
    p.add_argument("--tool", type=Path, default=TOOL_DIR)
    p.add_argument("--manifest", type=Path, nargs="+", default=[config.DATA_DIR / "dataset_us2" / "manifest.csv.gz"],
                   help="dataset manifests whose train chips flag train_overlap (us2 and us2_extra)")
    p.add_argument("--zip", type=Path, help="also pack data/ into this zip (e.g. out/gold_label_data.zip)")
    add_common_args(p)
    p = sub.add_parser("score")
    p.add_argument("--labels", type=Path, required=True, help="JSON downloaded from tools/label/index.html")
    p.add_argument("--tool", type=Path, default=TOOL_DIR)
    p.add_argument("--thresholds", type=float, nargs="+", default=[0.15, 0.25, 0.35, 0.5, 0.6, 0.7])
    p.add_argument("--preds", type=Path, help="preds JSON from `annotate --preds-out` of another model (default: the "
                                              "boxes shown in the page)")
    p.add_argument("--out-json", type=Path, default=config.OUT_DIR / "gold_score.json")
    add_common_args(p)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    {"sample": cmd_sample, "fetch": cmd_fetch, "annotate": cmd_annotate, "export": cmd_export, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    main()
