"""Accuracy on random land from the 300 gold tiles, labelled by the assistant (NOT gold). See docs/imagery.md.

The gold set (``gold.py``: 300 random 640 px NAIP tiles, 25 per Census region x urban / suburban / rural stratum) is
meant for human labels. Until those exist, the assistant labels every tile by eye. These are **model-assistant
labels, not gold**: they are written to out/v4/gold_assist/ and never to the labelling page's data or to any
human label file.

Commands
--------
    views     tile images with a pixel grid for labelling by eye (urban / suburban at 2x, rural as 2 x 2 sheets)
    context   everything the pipeline knows inside each tile, in tile pixels: OSM basketball features, every national
              detection (also near OSM courts), the v3 and v4 exports, the gold pre-annotations
              -> out/v4/gold_assist/context.json, plus 3x crops of each for checking (check/)
    zoom      3x crops of given boxes (TILE:x0,y0,x1,y1 ...) to confirm a court and refine its box
    score     assistant labels -> recall of OSM / all candidates (by v2 score) / the exports, precision of the exports and
              of candidates, per stratum and CONUS-weighted (area shares of the strata), with bootstrap CIs over tiles

Label file (same shape as tools/label/index.html's download, so ``gold score --labels`` reads it too):
    {"note": "...", "labeler": "assistant", "tiles": {"g000_no_urb": {"reviewed": true, "boxes": [
        {"x0": .., "y0": .., "x1": .., "y1": .., "label": "court", "access": "public|home|residents|unsure",
         "kind": "full|half|multi|unmarked|key", "certainty": "sure|likely", "note": "..."}]}}}
"""

from __future__ import annotations

import argparse
import logging
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from . import config
from .common import add_common_args, read_json, setup_logging, write_json

log = logging.getLogger("gold_assist")
GOLD = config.DATA_DIR / "gold"
OUT = config.OUT_DIR / "v4" / "gold_assist"
NAT = config.OUT_DIR / "national"
TILE = 640
GSD = config.TARGET_GSD_M


def tiles() -> list[dict[str, Any]]:
    return [t for t in read_json(GOLD / "tiles.json")["tiles"] if (GOLD / "images" / f"{t['id']}.jpg").exists()]


def to_px(t: dict[str, Any], lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    from pyproj import Transformer

    x, y = Transformer.from_crs("EPSG:4326", f"EPSG:{t['epsg']}", always_xy=True).transform(lon, lat)
    a, _, c, _, e, f = t["transform"]
    return (np.asarray(x) - c) / a, (np.asarray(y) - f) / e


def tile_bbox_ll(t: dict[str, Any], pad_px: float = 0.0) -> tuple[float, float, float, float]:
    from pyproj import Transformer

    a, _, c, _, e, f = t["transform"]
    xs = c + np.array([-pad_px, TILE + pad_px]) * a
    ys = f + np.array([-pad_px, TILE + pad_px]) * e
    lo, la = Transformer.from_crs(f"EPSG:{t['epsg']}", "EPSG:4326", always_xy=True).transform(
        np.array([xs[0], xs[1], xs[0], xs[1]]), np.array([ys[0], ys[0], ys[1], ys[1]]))
    return float(min(lo)), float(min(la)), float(max(lo)), float(max(la))


# =========================================================================== views
def grid_view(img: Any, scale: int, step: int = 64) -> Any:
    """Image scaled by ``scale`` inside a margin with tick labels every ``step`` tile pixels and a faint grid."""
    from PIL import Image, ImageDraw

    w = img.width * scale
    m = 22
    im = img.resize((w, w), Image.Resampling.BICUBIC) if scale != 1 else img.copy()
    canvas = Image.new("RGB", (w + 2 * m, w + 2 * m), (255, 255, 255))
    canvas.paste(im, (m, m))
    over = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(over)
    for v in range(0, TILE + 1, step):
        p = m + v * scale
        major = v % (2 * step) == 0
        if 0 < v < TILE and major:
            dr.line([(p, m), (p, m + w)], fill=(255, 255, 255, 60), width=1)
            dr.line([(m, p), (m + w, p)], fill=(255, 255, 255, 60), width=1)
    canvas = Image.alpha_composite(canvas.convert("RGBA"), over).convert("RGB")
    dr = ImageDraw.Draw(canvas)
    for v in range(0, TILE + 1, step):
        p = m + v * scale
        dr.line([(p, m - 5), (p, m)], fill=(0, 0, 0))
        dr.line([(p, m + w), (p, m + w + 5)], fill=(0, 0, 0))
        dr.line([(m - 5, p), (m, p)], fill=(0, 0, 0))
        dr.line([(m + w, p), (m + w + 5, p)], fill=(0, 0, 0))
        dr.text((p - 8, 1), str(v), fill=(0, 0, 0))
        dr.text((p - 8, m + w + 8), str(v), fill=(0, 0, 0))
        dr.text((0, p - 6), str(v), fill=(0, 0, 0))
    return canvas


def cmd_views(args: argparse.Namespace) -> None:
    from PIL import Image, ImageDraw

    out = args.out / "views"
    out.mkdir(parents=True, exist_ok=True)
    ts = tiles()
    rural = []
    for t in ts:
        img = Image.open(GOLD / "images" / f"{t['id']}.jpg").convert("RGB")
        if t["stratum"] == "rural":
            rural.append((t, img))
            continue
        grid_view(img, args.scale).save(out / f"{t['id']}.jpg", quality=92)
    # rural tiles: 2 x 2 sheets at 1x (each with its own ticks), then zoom where something looks like pavement
    for k in range(0, len(rural), 4):
        grp = rural[k:k + 4]
        cells = [grid_view(im, 1) for _, im in grp]
        cw = cells[0].width
        sheet = Image.new("RGB", (2 * cw, 2 * cw + 2 * 16), "white")
        dr = ImageDraw.Draw(sheet)
        for j, (cell, (t, _)) in enumerate(zip(cells, grp)):
            x, y = (j % 2) * cw, (j // 2) * (cw + 16)
            dr.text((x + 30, y + 2), t["id"], fill=(200, 0, 0))
            sheet.paste(cell, (x, y + 16))
        sheet.save(out / f"rural_{k // 4:02d}.jpg", quality=92)
    write_json(out / "index.json", {"urban_suburban": [t["id"] for t in ts if t["stratum"] != "rural"],
                                    "rural_sheets": {f"rural_{k // 4:02d}": [t["id"] for t, _ in rural[k:k + 4]]
                                                     for k in range(0, len(rural), 4)}})
    log.info("views: %d urban/suburban at %dx, %d rural sheets -> %s", len(ts) - len(rural), args.scale,
             math.ceil(len(rural) / 4), out)


# =========================================================================== context
def cmd_context(args: argparse.Namespace) -> None:
    import shapely
    from PIL import Image, ImageDraw

    from .geoparquet import read_geoparquet

    ts = tiles()
    bbs = {t["id"]: tile_bbox_ll(t, 40) for t in ts}
    oc, og = read_geoparquet(config.OSM_US_DIR / "courts_us.parquet",
                             columns=["osm_ref", "center_lon", "center_lat", "outdoor", "label_ok", "leisure", "sport"])
    det = pq.read_table(NAT / "detections.parquet", columns=["id", "conf", "lon", "lat", "box_w_m", "box_h_m", "verify",
                                                             "near_known_osm"]).to_pydict()
    dd = {k: np.asarray(v) for k, v in det.items()}
    cand = pq.read_table(args.candidates, columns=[c for c in ("id", "privacy", "conf_v2", "export", "export_v4",
                                                               "verify_scan", "years_seen", "years_checked")
                                                   if c in pq.read_schema(args.candidates).names]).to_pydict()
    cpos = {k: i for i, k in enumerate(cand["id"])}
    preds = read_json(GOLD / "preds.json")["preds"]
    res: dict[str, Any] = {}
    check = args.out / "check"
    check.mkdir(parents=True, exist_ok=True)
    for t in ts:
        w, s, e, n = bbs[t["id"]]
        items: list[dict[str, Any]] = []
        # OSM basketball features (polygon box or point)
        m = (oc["center_lon"] >= w) & (oc["center_lon"] <= e) & (oc["center_lat"] >= s) & (oc["center_lat"] <= n)
        for i in np.flatnonzero(m):
            g = og[i]
            b = shapely.bounds(g)
            px, py = to_px(t, np.array([b[0], b[2]]), np.array([b[1], b[3]]))
            items.append({"src": "osm", "ref": str(oc["osm_ref"][i]), "x0": float(min(px)), "y0": float(min(py)),
                          "x1": float(max(px)), "y1": float(max(py)), "outdoor": bool(oc["outdoor"][i]),
                          "label_ok": bool(oc["label_ok"][i])})
        m = (dd["lon"] >= w) & (dd["lon"] <= e) & (dd["lat"] >= s) & (dd["lat"] <= n)
        for i in np.flatnonzero(m):
            cx, cy = to_px(t, np.array([dd["lon"][i]]), np.array([dd["lat"][i]]))
            hw, hh = float(dd["box_w_m"][i]) / GSD / 2, float(dd["box_h_m"][i]) / GSD / 2
            rec = {"src": "det", "ref": str(dd["id"][i]), "conf": round(float(dd["conf"][i]), 3),
                   "verify": None if dd["verify"][i] != dd["verify"][i] else round(float(dd["verify"][i]), 3),
                   "near_osm": bool(dd["near_known_osm"][i]), "x0": float(cx[0] - hw), "y0": float(cy[0] - hh),
                   "x1": float(cx[0] + hw), "y1": float(cy[0] + hh)}
            j = cpos.get(rec["ref"])
            if j is not None:
                for k in ("privacy", "conf_v2", "export", "export_v4", "years_seen", "years_checked"):
                    if k in cand:
                        v = cand[k][j]
                        rec[k] = v if not (isinstance(v, float) and v != v) else None
            items.append(rec)
        for p in preds.get(t["id"], []):
            items.append({"src": "gold_pred", "ref": p["id"], "conf": p["conf"], "x0": p["x0"], "y0": p["y0"],
                          "x1": p["x1"], "y1": p["y1"]})
        items = [r for r in items if r["x1"] > -10 and r["y1"] > -10 and r["x0"] < TILE + 10 and r["y0"] < TILE + 10]
        res[t["id"]] = {"stratum": t["stratum"], "region": t["region"], "state": t["state"], "items": items}
        # 3x crops of everything, for checking by eye (number = index in items)
        if items:
            img = Image.open(GOLD / "images" / f"{t['id']}.jpg").convert("RGB")
            for k, r in enumerate(items):
                cx, cy = (r["x0"] + r["x1"]) / 2, (r["y0"] + r["y1"]) / 2
                half = max(40.0, 0.8 * max(r["x1"] - r["x0"], r["y1"] - r["y0"]))
                box = (int(cx - half), int(cy - half), int(cx + half), int(cy + half))
                crop = img.crop(box).resize((300, 300), Image.Resampling.BICUBIC)
                sc = 300 / (2 * half)
                dr = ImageDraw.Draw(crop)
                dr.rectangle([(r["x0"] - box[0]) * sc - 3, (r["y0"] - box[1]) * sc - 3, (r["x1"] - box[0]) * sc + 3,
                              (r["y1"] - box[1]) * sc + 3], outline=(255, 255, 0), width=1)
                crop.save(check / f"{t['id']}_{k:02d}.jpg", quality=90)
    write_json(args.out / "context.json", res)
    n = Counter(r["src"] for v in res.values() for r in v["items"])
    log.info("context: %s in %d tiles -> %s", dict(n), len(res), args.out / "context.json")


def cmd_zoom(args: argparse.Namespace) -> None:
    from PIL import Image, ImageDraw

    out = args.out / "zoom"
    out.mkdir(parents=True, exist_ok=True)
    for spec in args.boxes:
        tid, _, rest = spec.partition(":")
        x0, y0, x1, y1 = (float(v) for v in rest.split(","))
        img = Image.open(GOLD / "images" / f"{tid}.jpg").convert("RGB")
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        half = max(args.min_half, 0.8 * max(x1 - x0, y1 - y0))
        box = (int(cx - half), int(cy - half), int(cx + half), int(cy + half))
        crop = img.crop(box).resize((args.px, args.px), Image.Resampling.BICUBIC)
        sc = args.px / (2 * half)
        if not args.no_box:
            dr = ImageDraw.Draw(crop)
            dr.rectangle([(x0 - box[0]) * sc - 3, (y0 - box[1]) * sc - 3, (x1 - box[0]) * sc + 3, (y1 - box[1]) * sc + 3],
                         outline=(255, 255, 0), width=1)
        name = f"{tid}_{int(x0)}_{int(y0)}.jpg"
        crop.save(out / name, quality=92)
        print(out / name)


# =========================================================================== score
def iou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if not len(b):
        return np.zeros(0)
    ix = np.clip(np.minimum(b[:, 2], a[2]) - np.maximum(b[:, 0], a[0]), 0, None)
    iy = np.clip(np.minimum(b[:, 3], a[3]) - np.maximum(b[:, 1], a[1]), 0, None)
    inter = ix * iy
    return inter / np.maximum((a[2] - a[0]) * (a[3] - a[1]) + (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1]) - inter, 1e-9)


def matches(lab: np.ndarray, pred: np.ndarray, centre_px: float = 25.0) -> np.ndarray:
    """(L, P) bool: a prediction matches a labelled court if IoU >= 0.2 or its centre lies within the court box grown by
    ``centre_px`` / its centre is within ``centre_px`` of the court centre (points and loose OSM boxes)."""
    out = np.zeros((len(lab), len(pred)), bool)
    for i, a in enumerate(lab):
        if not len(pred):
            break
        cx, cy = (pred[:, 0] + pred[:, 2]) / 2, (pred[:, 1] + pred[:, 3]) / 2
        inside = (cx >= a[0] - 5) & (cx <= a[2] + 5) & (cy >= a[1] - 5) & (cy <= a[3] + 5)
        near = np.hypot(cx - (a[0] + a[2]) / 2, cy - (a[1] + a[3]) / 2) <= centre_px
        out[i] = (iou(a, pred) >= 0.2) | inside | near
    return out


def wilson(k: int, n: int) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    z = 1.96
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(c - h, 3), round(c + h, 3)


def cmd_score(args: argparse.Namespace) -> None:
    lab = read_json(args.labels)
    ctx = read_json(args.out / "context.json")
    weights = read_json(GOLD / "tiles.json").get("strata_weights", {})
    tl = {t["id"]: t for t in tiles()}
    reviewed = [tid for tid, v in lab["tiles"].items() if v.get("reviewed") and tid in ctx]
    th = list(args.thresholds)

    def boxes(items: list[dict[str, Any]]) -> np.ndarray:
        return np.array([[r["x0"], r["y0"], r["x1"], r["y1"]] for r in items]) if items else np.zeros((0, 4))

    # per tile: labelled courts (inside the tile) and what each method found
    per: list[dict[str, Any]] = []
    for tid in reviewed:
        courts = [b for b in lab["tiles"][tid].get("boxes", []) if b.get("label", "court") == "court"]
        # a court counts for the tile if its centre is inside (predictions near the edge are matched over the margin)
        courts = [b for b in courts if 0 <= (b["x0"] + b["x1"]) / 2 < TILE and 0 <= (b["y0"] + b["y1"]) / 2 < TILE]
        L = boxes(courts)
        items = ctx[tid]["items"]
        osm = [r for r in items if r["src"] == "osm"]
        dets = [r for r in items if r["src"] == "det"]
        e: dict[str, Any] = {"tile": tid, "stratum": ctx[tid]["stratum"], "region": ctx[tid]["region"],
                             "train_overlap": bool(tl[tid].get("train_overlap")), "courts": len(courts),
                             "court_kinds": [c.get("kind", "") for c in courts],
                             "court_access": [c.get("access", "unsure") for c in courts]}
        mo = matches(L, boxes(osm)).any(1) if len(L) else np.zeros(0, bool)
        e["found_osm"] = mo.tolist()
        dconf = np.array([r["conf"] for r in dets])
        e["best_det_conf"] = []
        for i in range(len(L)):
            mm = matches(L[i:i + 1], boxes(dets))[0] if dets else np.zeros(0, bool)
            e["best_det_conf"].append(float(dconf[mm].max()) if mm.any() else 0.0)
        for key in ("export", "export_v4"):
            ex = [r for r in dets if r.get(key)]
            m = matches(L, boxes(ex)) if len(L) else np.zeros((0, len(ex)), bool)
            e[f"found_{key}"] = (m.any(1) if len(L) else np.zeros(0, bool)).tolist()
            e[f"{key}_n"] = len(ex)
            e[f"{key}_tp"] = int(m.any(0).sum()) if len(ex) else 0
        # precision of candidates (not near OSM) at thresholds: a candidate is right if it matches a labelled court
        cands = [r for r in dets if not r["near_osm"]]
        cm = matches(L, boxes(cands)).any(0) if len(L) and cands else np.zeros(len(cands), bool)
        e["cand"] = [{"conf": r["conf"], "verify": r.get("verify"), "tp": bool(cm[k]), "privacy": r.get("privacy")}
                     for k, r in enumerate(cands)]
        per.append(e)

    def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
        n_c = sum(r["courts"] for r in rows)
        out: dict[str, Any] = {"tiles": len(rows), "courts": n_c}
        osm = sum(sum(r["found_osm"]) for r in rows)
        out["recall_osm"] = {"found": osm, "rate": round(osm / n_c, 3) if n_c else None, "ci": wilson(osm, n_c)}
        out["recall_detections"] = {}
        for t in th:
            k = sum(sum(c >= t for c in r["best_det_conf"]) for r in rows)
            out["recall_detections"][f"{t:.2f}"] = {"found": k, "rate": round(k / n_c, 3) if n_c else None,
                                                    "ci": wilson(k, n_c)}
        for key in ("export", "export_v4"):
            k = sum(sum(r[f"found_{key}"]) for r in rows)
            out[f"recall_{key}"] = {"found": k, "rate": round(k / n_c, 3) if n_c else None, "ci": wilson(k, n_c)}
            ne = sum(r[f"{key}_n"] for r in rows)
            tp = sum(r[f"{key}_tp"] for r in rows)
            out[f"precision_{key}"] = {"exported_in_tiles": ne, "tp": tp, "rate": round(tp / ne, 3) if ne else None,
                                       "ci": wilson(tp, ne)}
        osm_or_det = sum(sum((o or c >= 0.5) for o, c in zip(r["found_osm"], r["best_det_conf"])) for r in rows)
        out["recall_osm_or_det50"] = {"found": osm_or_det, "rate": round(osm_or_det / n_c, 3) if n_c else None}
        out["precision_candidates"] = {}
        for t in th:
            cs = [c for r in rows for c in r["cand"] if c["conf"] >= t]
            tp = sum(c["tp"] for c in cs)
            out["precision_candidates"][f"{t:.2f}"] = {"n": len(cs), "tp": tp, "rate": round(tp / len(cs), 3) if cs else None,
                                                       "ci": wilson(tp, len(cs))}
            cs_p = [c for c in cs if c["privacy"] == "public"]
            tpp = sum(c["tp"] for c in cs_p)
            out["precision_candidates"][f"{t:.2f}_public"] = {"n": len(cs_p), "tp": tpp,
                                                              "rate": round(tpp / len(cs_p), 3) if cs_p else None}
        return out

    def weighted(rows: list[dict[str, Any]], seed: int = 0, n_boot: int = 2000) -> dict[str, Any]:
        """CONUS-weighted rates: per (region, stratum) cell, mean per tile, weighted by the cell's area share; bootstrap
        over tiles within cells for a 95% CI."""
        cells: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            cells.setdefault(f"{r['region']}|{r['stratum']}", []).append(r)

        def rates(sample: dict[str, list[dict[str, Any]]]) -> dict[str, float]:
            acc: Counter[str] = Counter()
            for cell, rr in sample.items():
                w = weights.get(cell, 0.0)
                nt = max(1, len(rr))
                acc["courts"] += w * sum(r["courts"] for r in rr) / nt
                acc["osm"] += w * sum(sum(r["found_osm"]) for r in rr) / nt
                for t in th:
                    acc[f"det{t:.2f}"] += w * sum(sum(c >= t for c in r["best_det_conf"]) for r in rr) / nt
                    cs = [c for r in rr for c in r["cand"] if c["conf"] >= t]
                    acc[f"cand_n{t:.2f}"] += w * len(cs) / nt
                    acc[f"cand_tp{t:.2f}"] += w * sum(c["tp"] for c in cs) / nt
            o = {"courts_per_km2": acc["courts"] / (TILE * GSD / 1000) ** 2,
                 "recall_osm": acc["osm"] / acc["courts"] if acc["courts"] else float("nan")}
            for t in th:
                o[f"recall_det{t:.2f}"] = acc[f"det{t:.2f}"] / acc["courts"] if acc["courts"] else float("nan")
                o[f"precision_cand{t:.2f}"] = acc[f"cand_tp{t:.2f}"] / acc[f"cand_n{t:.2f}"] if acc[f"cand_n{t:.2f}"] \
                    else float("nan")
            return o

        point = rates(cells)
        rng = random.Random(seed)
        boots: dict[str, list[float]] = {k: [] for k in point}
        for _ in range(n_boot):
            smp = {c: [rng.choice(rr) for _ in rr] for c, rr in cells.items()}
            for k, v in rates(smp).items():
                if v == v:
                    boots[k].append(v)
        return {k: {"value": round(v, 4) if v == v else None,
                    "ci95": [round(float(np.quantile(boots[k], 0.025)), 4), round(float(np.quantile(boots[k], 0.975)), 4)]
                    if len(boots[k]) > 20 else None} for k, v in point.items()}

    res: dict[str, Any] = {"note": "ASSISTANT LABELS, NOT GOLD: courts labelled by eye by the assistant on 0.6 m NAIP",
                           "labels": str(args.labels), "tiles_reviewed": len(reviewed), "thresholds": th,
                           "all": summarise(per), "by_stratum": {s: summarise([r for r in per if r["stratum"] == s])
                                                                 for s in ("urban", "suburban", "rural")},
                           "no_train_overlap": summarise([r for r in per if not r["train_overlap"]]),
                           "conus_weighted": weighted(per)}
    # what kinds of courts are missed (detections < 0.5 and not in OSM)
    missed: Counter[str] = Counter()
    for r in per:
        for k, a, o, c in zip(r["court_kinds"], r["court_access"], r["found_osm"], r["best_det_conf"]):
            missed[f"{'osm' if o else 'not_osm'}|{'det>=0.5' if c >= 0.5 else ('det0.1-0.5' if c >= 0.1 else 'no_det')}|"
                   f"{k or '?'}|{a}"] += 1
    res["courts_by_outcome"] = dict(sorted(missed.items()))
    write_json(args.out / "score.json", res)
    write_json(args.out / "per_tile.json", per)
    a = res["all"]
    log.info("assistant-label score: %d tiles, %d courts; recall OSM %s, det@0.5 %s, export %s / v4 %s; precision "
             "export %s, candidates@0.5 %s", a["tiles"], a["courts"], a["recall_osm"]["rate"],
             a["recall_detections"].get("0.50", {}).get("rate"), a["recall_export"]["rate"], a["recall_export_v4"]["rate"],
             a["precision_export"], a["precision_candidates"].get("0.50"))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("views")
    p.add_argument("--scale", type=int, default=2)
    p.add_argument("--out", type=Path, default=OUT)
    add_common_args(p)
    p = sub.add_parser("context")
    p.add_argument("--candidates", type=Path, default=NAT / "v3" / "candidates_us.parquet")
    p.add_argument("--out", type=Path, default=OUT)
    add_common_args(p)
    p = sub.add_parser("zoom")
    p.add_argument("boxes", nargs="+", help="TILE:x0,y0,x1,y1")
    p.add_argument("--px", type=int, default=480)
    p.add_argument("--min-half", type=float, default=48.0)
    p.add_argument("--no-box", action="store_true")
    p.add_argument("--out", type=Path, default=OUT)
    add_common_args(p)
    p = sub.add_parser("score")
    p.add_argument("--labels", type=Path, default=OUT / "assistant_labels.json")
    p.add_argument("--thresholds", type=float, nargs="+", default=[0.1, 0.25, 0.5, 0.7])
    p.add_argument("--out", type=Path, default=OUT)
    add_common_args(p)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    {"views": cmd_views, "context": cmd_context, "zoom": cmd_zoom, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    main()
