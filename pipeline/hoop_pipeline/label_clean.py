"""Label cleaning and hard-negative mining with a trained detector.

OSM labels are noisy (pools, parking lots or pickleball blocks tagged basketball,
shifted polygons, one polygon over several courts), and the OSM-derived
"background" chips sometimes contain unmapped courts. This step runs a detector
over every dataset chip and finds where it strongly disagrees with the labels.

``score``    FastDetector over images/<split>/ -> one parquet row per detection
             (conf >= 0.01): chip, split, x0, y0, x1, y1, conf.
``analyze``  joins detections with the YOLO labels and the manifest:
             * per label (OSM ref): the best matching score (IoU >= 0.3) in its own
               positive chip and in any chip, plus polygon heuristics from
               courts_us (area, rotated-rectangle aspect, surface, sport, name);
             * per chip: the best *unmatched* score and whether that box sits at the
               chip centre (the hard-negative object itself) or elsewhere;
             writes label_scores.parquet, chip_scores.parquet, summary.json and
             montages (green = label, magenta = detection) of every bucket, to be
             looked at before choosing thresholds.
``select``   applies the chosen rules: labels to remove (box deleted, chip kept: the
             object is background), labels to exclude (chips showing them dropped),
             negative chips to drop (a confident detection away from the centre of
             a random-point chip is usually an unmapped court), and hard negatives to
             repeat; rewrites the train label files (originals kept in
             labels_orig/) and writes train_v2.txt plus data_v2.yaml.

Example
-------
    python -m hoop_pipeline.label_clean score --data datasets/us2 --weights weights/best.pt --out out/clean/preds_v1.parquet
    python -m hoop_pipeline.label_clean analyze --data datasets/us2 --preds out/clean/preds_v1.parquet --out out/clean
    python -m hoop_pipeline.label_clean select --data datasets/us2 --analysis out/clean --remove-below 0.02
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import random
import shutil
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image, ImageDraw

from . import config
from .common import add_common_args, setup_logging, write_json
from .evaluate import iou_matrix

log = logging.getLogger("label_clean")
CENTERED_KINDS = {"pitch_tennis", "pitch_pickleball", "pitch_volleyball", "pitch_other", "parking", "pool",
                  "playground", "extra", "tn_fp"}
# Negative chips are dropped when the detector's best unlabelled box scores >= DROP_ANY[kind] (anywhere), or
# >= DROP_OFF[kind] away from the chip centre (centred kinds). Chosen from the v1 montages (2026-10-09): school,
# park and apartment "background" boxes >= 0.45 are mostly real unmapped courts; on tennis/pickleball/volleyball
# chips, confident boxes are often a real basketball court next to (or mis-tagged as) the pitch; on residential,
# parking, pool and random-land chips they are mostly roofs, pads and parking (kept as hard negatives).
DROP_ANY = {"school": 0.45, "park": 0.45, "apartments": 0.45, "playground": 0.5, "pitch_tennis": 0.6,
            "pitch_pickleball": 0.6, "pitch_volleyball": 0.6, "pitch_other": 0.6, "parking": 0.7, "pool": 0.7,
            "residential": 0.6, "random_land": 0.6, "tn_fp": 1.1, "extra": 1.1}
DROP_OFF = {"pitch_tennis": 0.45, "pitch_pickleball": 0.45, "pitch_volleyball": 0.45, "pitch_other": 0.45,
            "parking": 0.45, "pool": 0.45, "playground": 0.45}


# --------------------------------------------------------------------------- io
def read_manifest(data: Path) -> dict[str, dict[str, str]]:
    with gzip.open(data / "manifest.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        return {r["name"]: r for r in csv.DictReader(fh)}


def load_label_boxes(path: Path, size: int) -> np.ndarray:
    if not path.exists():
        return np.zeros((0, 4))
    rows = [ln.split() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not rows:
        return np.zeros((0, 4))
    arr = np.array([[float(v) for v in r[1:5]] for r in rows]) * size
    xc, yc, w, h = arr.T
    return np.column_stack([xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2])


def iter_images(paths: list[Path], batch: int, threads: int) -> Iterator[tuple[list[Path], np.ndarray]]:
    def load(p: Path) -> np.ndarray:
        with Image.open(p) as im:
            return np.asarray(im.convert("RGB"))

    with ThreadPoolExecutor(threads) as pool:
        for k in range(0, len(paths), batch):
            chunk = paths[k:k + batch]
            yield chunk, np.stack(list(pool.map(load, chunk)))


# --------------------------------------------------------------------------- score
def cmd_score(args: argparse.Namespace) -> None:
    import torch

    from .fastdet import FastDetector

    det = FastDetector(args.weights, imgsz=args.imgsz, tta=args.tta)
    rows: dict[str, list[Any]] = {"chip": [], "split": [], "x0": [], "y0": [], "x1": [], "y1": [], "conf": []}
    t0 = time.monotonic()
    n = 0
    for split in args.splits:
        paths = sorted((args.data / "images" / split).glob("*.jpg"))
        log.info("%s: %d chips", split, len(paths))
        pinned = None
        for chunk, arr in iter_images(paths, args.batch, args.threads):
            t = torch.from_numpy(arr)
            if torch.cuda.is_available():
                if pinned is None or pinned.shape[0] < len(arr):
                    pinned = torch.empty((args.batch, *arr.shape[1:]), dtype=torch.uint8).pin_memory()
                pinned[: len(arr)].copy_(t)
                t = pinned[: len(arr)]
            d = det.detect(t, conf=args.conf, iou=0.5).cpu().numpy()
            for row in d:
                rows["chip"].append(chunk[int(row[0])].stem)
                rows["split"].append(split)
                rows["x0"].append(float(row[1]))
                rows["y0"].append(float(row[2]))
                rows["x1"].append(float(row[3]))
                rows["y1"].append(float(row[4]))
                rows["conf"].append(float(row[5]))
            n += len(chunk)
            if n % (args.batch * 100) < len(chunk):
                log.info("  %d chips, %.0f chips/s, %d detections", n, n / (time.monotonic() - t0), len(rows["chip"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(rows), args.out, compression="zstd")
    meta = {"weights": str(args.weights), "imgsz": det.imgsz, "tta": args.tta, "chips": n, "detections": len(rows["chip"]),
            "seconds": round(time.monotonic() - t0, 1), "chips_per_s": round(n / max(1e-6, time.monotonic() - t0), 1)}
    write_json(args.out.with_suffix(".json"), meta)
    log.info("scored %s", meta)


# --------------------------------------------------------------------------- analyze
def court_heuristics(courts_path: Path) -> dict[str, dict[str, Any]]:
    """Polygon facts per OSM ref: area, rotated-rectangle aspect and fill, tags of interest."""
    import shapely

    from .geoparquet import read_geoparquet

    a, g = read_geoparquet(courts_path, columns=["osm_ref", "area_m2", "tags", "label_ok", "state"])
    out: dict[str, dict[str, Any]] = {}
    sel = np.flatnonzero(a["label_ok"].astype(bool))
    rects = shapely.minimum_rotated_rectangle(g[sel])
    for k, i in enumerate(sel):
        tags = json.loads(a["tags"][i])
        r = rects[k]
        coords = np.asarray(r.exterior.coords) if r.geom_type == "Polygon" else np.zeros((0, 2))
        aspect = fill = float("nan")
        if len(coords) >= 4:
            lat = float(coords[:, 1].mean())
            kx = 111_320.0 * np.cos(np.radians(lat))
            e1 = np.hypot((coords[1, 0] - coords[0, 0]) * kx, (coords[1, 1] - coords[0, 1]) * 111_320.0)
            e2 = np.hypot((coords[2, 0] - coords[1, 0]) * kx, (coords[2, 1] - coords[1, 1]) * 111_320.0)
            if min(e1, e2) > 0:
                aspect = max(e1, e2) / min(e1, e2)
                fill = float(a["area_m2"][i]) / (e1 * e2)
        out[str(a["osm_ref"][i])] = {
            "area_m2": float(a["area_m2"][i]), "aspect": aspect, "rect_fill": fill, "state": str(a["state"][i]),
            "surface": tags.get("surface"), "sport": tags.get("sport"), "access": tags.get("access"),
            "name": tags.get("name"), "hoops": tags.get("hoops"),
        }
    return out


def cmd_analyze(args: argparse.Namespace) -> None:
    data: Path = args.data
    size = args.chip_size
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    man = read_manifest(data)
    preds = pq.read_table(args.preds).to_pydict()
    by_chip: dict[str, list[int]] = {}
    for i, c in enumerate(preds["chip"]):
        by_chip.setdefault(c, []).append(i)
    px = np.column_stack([preds["x0"], preds["y0"], preds["x1"], preds["y1"]]) if preds["chip"] else np.zeros((0, 4))
    pc = np.asarray(preds["conf"], dtype=float)
    heur = court_heuristics(args.courts)

    lab_rows: list[dict[str, Any]] = []
    chip_rows: list[dict[str, Any]] = []
    for name, m in man.items():
        split = m["split"]
        if args.splits and split not in args.splits:
            continue
        gt = load_label_boxes(data / "labels" / split / f"{name}.txt", size)
        refs = [r for r in m["label_refs"].split(";") if r] if m["label_refs"] else []
        di = by_chip.get(name, [])
        db, dc = px[di], pc[di]
        ious = iou_matrix(gt, db) if len(gt) and len(db) else np.zeros((len(gt), len(db)))
        for j in range(len(gt)):
            ok = ious[j] >= 0.3 if len(db) else np.zeros(0, bool)
            best = float(dc[ok].max()) if ok.any() else 0.0
            ref = refs[j] if j < len(refs) else ""
            own = m["kind"] == "pos" and bool(ref) and name.endswith("_pos_" + ref.replace("/", ""))
            w, h = gt[j, 2] - gt[j, 0], gt[j, 3] - gt[j, 1]
            lab_rows.append({"ref": ref, "chip": name, "split": split, "own_chip": bool(own), "conf": best,
                             "best_iou": float(ious[j].max()) if len(db) else 0.0, "box_w_px": float(w),
                             "box_h_px": float(h)})
        unmatched = np.ones(len(db), bool)
        if len(gt) and len(db):
            unmatched = ious.max(0) < 0.1
        uc = dc[unmatched]
        ub = db[unmatched]
        best_u = float(uc.max()) if len(uc) else 0.0
        centre_d = float("nan")
        if len(uc):
            k = int(np.argmax(uc))
            # distance from the chip centre to the box (0 if the centre is inside the box)
            dx = max(ub[k, 0] - size / 2, 0.0, size / 2 - ub[k, 2])
            dy = max(ub[k, 1] - size / 2, 0.0, size / 2 - ub[k, 3])
            centre_d = float(np.hypot(dx, dy))
        chip_rows.append({"chip": name, "split": split, "kind": m["kind"], "center_kind": m["center_kind"],
                          "state": m["state"], "n_labels": len(gt), "best_unmatched": best_u,
                          "unmatched_centre_dist_px": centre_d, "n_unmatched_025": int((uc >= 0.25).sum())})
    # per label: own-chip score and the max over all chips
    agg: dict[str, dict[str, Any]] = {}
    for r in lab_rows:
        if not r["ref"]:
            continue
        a = agg.setdefault(r["ref"], {"ref": r["ref"], "own_conf": float("nan"), "max_conf": 0.0, "chips": 0,
                                      "own_chip": "", "split": r["split"]})
        a["chips"] += 1
        a["max_conf"] = max(a["max_conf"], r["conf"])
        if r["own_chip"]:
            a["own_conf"] = r["conf"]
            a["own_chip"] = r["chip"]
            a["box_w_px"], a["box_h_px"] = r["box_w_px"], r["box_h_px"]
    for ref, a in agg.items():
        a.update({f"h_{k}": v for k, v in heur.get(ref, {}).items()})
    lab_table = pa.Table.from_pylist(list(agg.values()))
    pq.write_table(lab_table, out / "label_scores.parquet")
    pq.write_table(pa.Table.from_pylist(chip_rows), out / "chip_scores.parquet")

    labs = list(agg.values())
    own_arr = np.array([a["own_conf"] for a in labs], dtype=float)
    buckets = [0.0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 1.01]
    summary: dict[str, Any] = {"labels": len(labs), "labels_with_own_chip": int(np.isfinite(own_arr).sum())}
    summary["own_conf_hist"] = {f"{lo:.2f}-{hi:.2f}": int(((own_arr >= lo) & (own_arr < hi)).sum())
                                for lo, hi in zip(buckets[:-1], buckets[1:])}
    neg = [c for c in chip_rows if c["kind"] == "neg"]
    summary["neg_chips"] = len(neg)
    summary["neg_best_unmatched_by_kind"] = {}
    for kd in sorted({c["center_kind"] for c in neg}):
        v = np.array([c["best_unmatched"] for c in neg if c["center_kind"] == kd])
        summary["neg_best_unmatched_by_kind"][kd] = {
            "chips": len(v), ">=0.1": int((v >= 0.1).sum()), ">=0.25": int((v >= 0.25).sum()),
            ">=0.5": int((v >= 0.5).sum()), ">=0.7": int((v >= 0.7).sum())}
    pos = [c for c in chip_rows if c["kind"] == "pos"]
    v = np.array([c["best_unmatched"] for c in pos])
    summary["pos_chips_unmatched"] = {"chips": len(v), ">=0.25": int((v >= 0.25).sum()), ">=0.5": int((v >= 0.5).sum())}
    write_json(out / "summary.json", summary)
    log.info("summary: %s", json.dumps(summary))

    # ---- montages
    rng = random.Random(args.seed)
    labs_own = [a for a in labs if a["own_chip"]]

    def sample(rows: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
        return rng.sample(rows, min(k, len(rows)))

    groups = {
        "labels_conf_lt_0.01": [a for a in labs_own if a["own_conf"] < 0.01],
        "labels_conf_0.01_0.05": [a for a in labs_own if 0.01 <= a["own_conf"] < 0.05],
        "labels_conf_0.05_0.15": [a for a in labs_own if 0.05 <= a["own_conf"] < 0.15],
        "labels_conf_lt_0.05_area_gt_1200": [a for a in labs_own if a["own_conf"] < 0.05 and (a.get("h_area_m2") or 0) > 1200],
    }
    for kd in sorted({c["center_kind"] for c in neg}):
        far = [c for c in neg if c["center_kind"] == kd and c["best_unmatched"] >= 0.5]
        groups[f"neg_{kd}_unmatched_ge_0.5"] = [{"own_chip": c["chip"], "split": c["split"], "own_conf": c["best_unmatched"],
                                                 "ref": f"d={c['unmatched_centre_dist_px']:.0f}px"} for c in far]
    groups["pos_unmatched_ge_0.6"] = [{"own_chip": c["chip"], "split": c["split"], "own_conf": c["best_unmatched"], "ref": ""}
                                      for c in pos if c["best_unmatched"] >= 0.6]
    counts = {}
    for gname, rows in groups.items():
        counts[gname] = len(rows)
        pick = sample(rows, args.montage)
        ims, caps = [], []
        for a in pick:
            chip = a["own_chip"]
            split = a.get("split") or man[chip]["split"]
            p = data / "images" / split / f"{chip}.jpg"
            if not p.exists():
                continue
            gt = load_label_boxes(data / "labels" / split / f"{chip}.txt", size)
            di = by_chip.get(chip, [])
            ims.append(draw_chip(p, gt, px[di], pc[di]))
            caps.append(f"{a.get('ref', '')} {a['own_conf']:.2f} {str(a.get('h_area_m2', ''))[:6]}")
        from .build_dataset_us import montage

        for k in range(0, len(ims), 20):
            montage(ims[k:k + 20], 5, out / "montages" / f"{gname}_{k // 20:02d}.jpg", tile=320, captions=caps[k:k + 20])
    write_json(out / "montage_groups.json", counts)
    log.info("montage groups: %s", counts)


def draw_chip(path: Path, gt: np.ndarray, det: np.ndarray, conf: np.ndarray, min_conf: float = 0.05) -> Image.Image:
    im = Image.open(path).convert("RGB")
    d = ImageDraw.Draw(im)
    for x0, y0, x1, y1 in gt:
        d.rectangle([x0, y0, x1, y1], outline=(0, 255, 0), width=2)
    for (x0, y0, x1, y1), c in zip(det, conf):
        if c >= min_conf:
            d.rectangle([x0, y0, x1, y1], outline=(255, 0, 255), width=1)
            d.text((x0 + 2, max(0, y0 - 10)), f"{c:.2f}", fill=(255, 0, 255))
    return im


# --------------------------------------------------------------------------- select
def cmd_select(args: argparse.Namespace) -> None:
    data: Path = args.data
    lab = pq.read_table(args.analysis / "label_scores.parquet").to_pylist()
    chips = pq.read_table(args.analysis / "chip_scores.parquet").to_pylist()
    rng = random.Random(args.seed)

    def own(a: dict[str, Any]) -> float:
        v = a.get("own_conf")
        return float(v) if v is not None and np.isfinite(v) else float(a.get("max_conf") or 0.0)

    remove: set[str] = set()
    exclude: set[str] = set()
    reasons: Counter[str] = Counter()
    chip_best = {c["chip"]: float(c["best_unmatched"]) for c in chips}
    for a in lab:
        c = own(a)
        area = a.get("h_area_m2") or 0.0
        surface = (a.get("h_surface") or "").lower()
        # a confident unlabelled box in the label's own chip means the polygon is shifted or covers a block of
        # courts the model boxes one by one: drop the chip instead of deleting the box
        neighbour = chip_best.get(a.get("own_chip") or "", 0.0) >= args.neighbour_conf
        if surface in args.bad_surfaces:
            exclude.add(a["ref"])
            reasons[f"surface_{surface}"] += 1
        elif c < args.remove_below and max(c, a.get("max_conf") or 0.0) < args.remove_below and not neighbour:
            remove.add(a["ref"])
            reasons["remove_low_conf"] += 1
        elif c < args.exclude_below:
            exclude.add(a["ref"])
            reasons["exclude_low_conf"] += 1
        elif area > args.big_area and c < args.big_area_conf:
            exclude.add(a["ref"])
            reasons["exclude_big_low_conf"] += 1
    log.info("labels: %d removed (box deleted), %d excluded (chips dropped); %s", len(remove), len(exclude), dict(reasons))

    man = read_manifest(data)
    # rewrite train labels: drop removed boxes; chips with excluded labels are skipped
    lbl_dir = data / "labels" / "train"
    orig = data / "labels_orig" / "train"
    if not orig.exists():
        orig.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(lbl_dir, orig)
    keep_names: list[str] = []
    dropped: Counter[str] = Counter()
    boxes_removed = 0
    chip_by = {c["chip"]: c for c in chips}
    hard: list[str] = []
    for name, m in man.items():
        if m["split"] != "train":
            continue
        refs = [r for r in m["label_refs"].split(";") if r] if m["label_refs"] else []
        if any(r in exclude for r in refs):
            dropped["excluded_label_in_chip"] += 1
            continue
        src = orig / f"{name}.txt"
        lines = src.read_text(encoding="utf-8").splitlines() if src.exists() else []
        if any(r in remove for r in refs):
            kept = [ln for ln, r in zip(lines, refs) if r not in remove]
            boxes_removed += len(lines) - len(kept)
            lines = kept
        (lbl_dir / f"{name}.txt").write_text("".join(ln + "\n" for ln in lines), encoding="utf-8", newline="\n")
        crow = chip_by.get(name)
        if m["kind"] == "neg" and crow is not None:
            bu = crow["best_unmatched"]
            dist = crow["unmatched_centre_dist_px"]
            kd = m["center_kind"]
            off_centre = not (dist == dist and dist <= args.centre_px)  # NaN-safe
            t_any = DROP_ANY.get(kd, args.drop_neg_conf)
            t_off = DROP_OFF.get(kd, t_any)
            if bu >= t_any or (off_centre and bu >= t_off):
                dropped[f"neg_confident_box_{kd}"] += 1
                continue
            if bu >= args.hard_conf:
                hard.append(name)
        if m["kind"] == "pos" and crow is not None and crow["best_unmatched"] >= args.drop_pos_unmatched:
            dropped["pos_with_confident_unlabelled_box"] += 1
            continue
        keep_names.append(name)
    extra_lists: list[str] = []
    for extra in args.extra_data:
        for p in sorted((extra / "images" / "train").glob("*.jpg")):
            extra_lists += [p.resolve().as_posix()] * args.extra_repeat
    lines_out = [(data / "images" / "train" / f"{n}.jpg").resolve().as_posix() for n in keep_names]
    rep = [(data / "images" / "train" / f"{n}.jpg").resolve().as_posix() for n in hard] * max(0, args.hard_repeat - 1)
    rng.shuffle(rep)
    all_lines = lines_out + rep + extra_lists
    (data / "train_v2.txt").write_text("\n".join(all_lines) + "\n", encoding="utf-8", newline="\n")
    for cache in (data / "labels").glob("*.cache"):
        cache.unlink()
    (data / "data_v2.yaml").write_text(
        f"# v2 training list (label_clean select); model selection on val_us\npath: {data.resolve().as_posix()}\n"
        f"train: train_v2.txt\nval: images/val_us\nnames:\n  0: court\n", encoding="utf-8")
    (args.analysis / "removed_labels.txt").write_text("\n".join(sorted(remove)) + "\n", encoding="utf-8")
    (args.analysis / "excluded_labels.txt").write_text("\n".join(sorted(exclude)) + "\n", encoding="utf-8")
    stats = {"train_chips": len(keep_names), "hard_negatives": len(hard), "hard_repeat": args.hard_repeat,
             "extra_chips": len(extra_lists), "list_lines": len(all_lines), "dropped": dict(dropped),
             "labels_removed": len(remove), "boxes_removed": boxes_removed, "labels_excluded": len(exclude),
             "reasons": dict(reasons), "rules": json.loads(json.dumps(vars(args), default=str)),
             "drop_any": DROP_ANY, "drop_off": DROP_OFF}
    write_json(args.analysis / "select_stats.json", stats)
    log.info("train_v2.txt: %s", {k: v for k, v in stats.items() if k != "rules"})


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("score")
    p.add_argument("--data", type=Path, default=config.DATASETS_DIR / "us2")
    p.add_argument("--splits", nargs="+", default=["train", "val_us", "val"])
    p.add_argument("--weights", type=Path, default=config.DEFAULT_WEIGHTS)
    p.add_argument("--imgsz", type=int, default=None)
    p.add_argument("--tta", default="none")
    p.add_argument("--conf", type=float, default=0.01)
    p.add_argument("--batch", type=int, default=512)
    p.add_argument("--threads", type=int, default=24)
    p.add_argument("--out", type=Path, default=config.OUT_DIR / "clean" / "preds_v1.parquet")
    add_common_args(p)
    p = sub.add_parser("analyze")
    p.add_argument("--data", type=Path, default=config.DATASETS_DIR / "us2")
    p.add_argument("--preds", type=Path, default=config.OUT_DIR / "clean" / "preds_v1.parquet")
    p.add_argument("--courts", type=Path, default=config.OSM_US_DIR / "courts_us.parquet")
    p.add_argument("--splits", nargs="*", default=["train"])
    p.add_argument("--chip-size", type=int, default=config.CHIP_SIZE_PX)
    p.add_argument("--montage", type=int, default=40)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path, default=config.OUT_DIR / "clean")
    add_common_args(p)
    p = sub.add_parser("select")
    p.add_argument("--data", type=Path, default=config.DATASETS_DIR / "us2")
    p.add_argument("--analysis", type=Path, default=config.OUT_DIR / "clean")
    p.add_argument("--remove-below", type=float, default=0.02,
                   help="labels the detector scores below this in every chip: box deleted (object is background)")
    p.add_argument("--exclude-below", type=float, default=0.05,
                   help="labels below this (but not removed): chips showing them dropped")
    p.add_argument("--big-area", type=float, default=1500.0, help="m^2; big polygons (multi-court blocks) ...")
    p.add_argument("--big-area-conf", type=float, default=0.15, help="... scored below this are excluded")
    p.add_argument("--bad-surfaces", nargs="*", default=["grass", "sand", "clay", "dirt", "artificial_turf", "gravel"])
    p.add_argument("--drop-neg-conf", type=float, default=0.5,
                   help="drop threshold for negative kinds not in DROP_ANY (see module constants)")
    p.add_argument("--neighbour-conf", type=float, default=0.15,
                   help="labels whose own chip holds an unlabelled box >= this are excluded, never removed")
    p.add_argument("--centre-px", type=float, default=60.0, help="box within this of the chip centre = the hard object")
    p.add_argument("--drop-pos-unmatched", type=float, default=0.5,
                   help="positive chips with another confident unlabelled box are dropped (unmapped neighbour court)")
    p.add_argument("--hard-conf", type=float, default=0.1, help="negatives scoring >= this are hard negatives")
    p.add_argument("--hard-repeat", type=int, default=2, help="list hard negatives this many times")
    p.add_argument("--extra-data", type=Path, nargs="*", default=[], help="extra datasets whose train images are added")
    p.add_argument("--extra-repeat", type=int, default=3)
    p.add_argument("--seed", type=int, default=0)
    add_common_args(p)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    {"score": cmd_score, "analyze": cmd_analyze, "select": cmd_select}[args.cmd](args)


if __name__ == "__main__":
    main()
