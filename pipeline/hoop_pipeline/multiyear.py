"""v4: re-check candidates against several NAIP years (persistence / change evidence). See docs/imagery.md.

The national v2 scan used one NAIP flight per location (the newest own-state flight). Planetary Computer also
hosts the 2016-2021 flights of every state, so the same box can be looked at in 3-5 years. A real court is
paved and painted in every flight since it was built; transient look-alikes (parked trailers, tarps, piles,
construction pads, event tents) are not. A persistent look-alike (tennis court, sand volleyball, parking) still
persists, so persistence is evidence to combine with the detector and verifier, not a filter on its own.

Commands
--------
    input      the candidates that matter + calibration groups -> data/v4/multiyear_input.parquet
               (public / unknown candidates with v2 conf >= 0.3 or in-scan verifier >= 0.9; every v3 export; every
               reviewed candidate; a sample of detections on known OSM courts; official truth courts no detection
               matched, i.e. the scan's misses)
    run        (GPU node) for each row and each available NAIP year (the scan's year + up to --max-other others,
               newest first): a 192 m 4-band chip centred on the box, v2 detector + verifier on the box, NDVI and
               brightness in the box, and a 32 x 32 patch correlated with the scan year's patch
               -> <out>/parts/part_NNNNN.parquet (one row per id x year), restartable per chunk
    merge      parts -> <out>/years.parquet
    features   per-id features (years_checked, years_seen, conf / verify across years, slab appeared / gone, change)
               joined onto out/national/v3/candidates_us.parquet -> out/national/v4/candidates_v4.parquet
    labelled   features of the reviewed candidates (C / N / U blind labels of earlier runs) by label
    grid       export rules side by side: counts per state, labelled-set precision, held-out truth evidence
    select     mark the v4 export (column ``export_v4``) at a rule
    app-ready  out/national/v4/app_ready/<ST>.geojson (v3 schema + optional evidence fields) + index.json
    review     blind review sheets of any subset (fresh NAIP chips, scan-year item), optionally with a year strip

Privacy is never relaxed: every rule here requires privacy == public and a context-CNN score, as v3 did.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import config
from .common import add_common_args, read_json, setup_logging, write_json

log = logging.getLogger("multiyear")

V4_DATA = config.DATA_DIR / "v4"
V4_OUT = config.OUT_DIR / "v4"
NAT = config.OUT_DIR / "national"
NAT4 = NAT / "v4"
CHIP_PX = config.CHIP_SIZE_PX     # 320 px = 192 m at 0.6 m
GSD = config.TARGET_GSD_M
SEEN = 0.25                        # v2 score at the box that counts as "a court is there this year"
REVIEW_SETS = {                    # earlier blind reviews (model-assistant labels, not gold): C / N / U
    "v2_public_strata": NAT / "review" / "public_strata",
    "v2_exported_sample": NAT / "review" / "exported_sample",
    "v3_exported_sample": NAT / "v3" / "review" / "exported_sample",
    "v3_inc_states": NAT / "v3" / "review" / "inc_states",
    "v3_inc_verify95": NAT / "v3" / "review" / "inc_verify95",
}


# =========================================================================== helpers
def morton(lon: np.ndarray, lat: np.ndarray, bits: int = 16) -> np.ndarray:
    """Z-order key, so a chunk of rows shares NAIP items (fewer COG opens)."""
    x = np.clip(((lon + 125.0) / 59.0 * (2 ** bits)).astype(np.int64), 0, 2 ** bits - 1)
    y = np.clip(((lat - 24.0) / 26.0 * (2 ** bits)).astype(np.int64), 0, 2 ** bits - 1)
    key = np.zeros(len(lon), np.int64)
    for b in range(bits):
        key |= ((x >> b) & 1) << (2 * b) | ((y >> b) & 1) << (2 * b + 1)
    return key


def load_review_labels() -> dict[str, tuple[str, str]]:
    """candidate id -> (label C/N/U, review set) from the earlier blind reviews (first label wins)."""
    out: dict[str, tuple[str, str]] = {}
    for name, d in REVIEW_SETS.items():
        if not (d / "labels.json").exists():
            continue
        labs = read_json(d / "labels.json")["labels"]
        items = read_json(d / "sample.json")["items"]
        for it in items:
            lab = labs.get(str(it["n"]))
            if lab and it["id"] not in out:
                out[it["id"]] = (lab.split(":")[0], name)
    return out


def best_overlap(dets: np.ndarray, box: np.ndarray) -> tuple[float, int]:
    """(max score, index) of detections (K, 5: x0 y0 x1 y1 s) with IoU >= 0.3 to ``box`` or containing its centre."""
    if not len(dets):
        return 0.0, -1
    ix = np.clip(np.minimum(dets[:, 2], box[2]) - np.maximum(dets[:, 0], box[0]), 0, None)
    iy = np.clip(np.minimum(dets[:, 3], box[3]) - np.maximum(dets[:, 1], box[1]), 0, None)
    inter = ix * iy
    union = (dets[:, 2] - dets[:, 0]) * (dets[:, 3] - dets[:, 1]) + (box[2] - box[0]) * (box[3] - box[1]) - inter
    iou = inter / np.maximum(union, 1e-9)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    inside = (dets[:, 0] <= cx) & (cx <= dets[:, 2]) & (dets[:, 1] <= cy) & (cy <= dets[:, 3])
    m = (iou >= 0.3) | inside
    if not m.any():
        return 0.0, -1
    k = int(np.flatnonzero(m)[np.argmax(dets[m, 4])])
    return float(dets[k, 4]), k


def near_best(dets: np.ndarray, cx: float, cy: float, r_px: float) -> tuple[float, int]:
    """(max score, index) of detections whose centre is within r_px of (cx, cy)."""
    if not len(dets):
        return 0.0, -1
    d = np.hypot((dets[:, 0] + dets[:, 2]) / 2 - cx, (dets[:, 1] + dets[:, 3]) / 2 - cy)
    m = d <= r_px
    if not m.any():
        return 0.0, -1
    k = int(np.flatnonzero(m)[np.argmax(dets[m, 4])])
    return float(dets[k, 4]), k


def box_px(w_m: float, h_m: float, px: int = CHIP_PX) -> np.ndarray:
    c = px / 2
    w = float(np.clip(w_m / GSD, 6, px - 2))
    h = float(np.clip(h_m / GSD, 6, px - 2))
    return np.array([c - w / 2, c - h / 2, c + w / 2, c + h / 2])


def patch32(gray: np.ndarray, box: np.ndarray, grow: float = 1.25) -> np.ndarray:
    """32 x 32 float patch of the box (grown by ``grow``) for year-to-year correlation."""
    import cv2

    c = (box[:2] + box[2:]) / 2
    half = np.maximum((box[2:] - box[:2]) * grow / 2, 8)
    x0, y0 = np.floor(c - half).astype(int)
    x1, y1 = np.ceil(c + half).astype(int)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(gray.shape[1], x1), min(gray.shape[0], y1)
    return cv2.resize(gray[y0:y1, x0:x1].astype(np.float32), (32, 32), interpolation=cv2.INTER_AREA)


def corr(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation of two patches; NaN when either is nearly flat (std < 3 grey levels)."""
    a = a.ravel() - a.mean()
    b = b.ravel() - b.mean()
    if a.std() < 3 or b.std() < 3:
        return float("nan")
    return float((a * b).sum() / (np.sqrt((a * a).sum()) * np.sqrt((b * b).sum())))


# =========================================================================== input
def cmd_input(args: argparse.Namespace) -> None:
    import shapely
    from shapely import STRtree

    from .truth import COURT_LEVEL, HIDDEN, LIVE_STATUS, load_truth, pts_m, truth_geoms_m

    rng = np.random.default_rng(args.seed)
    cols = ["id", "state", "lon", "lat", "box_w_m", "box_h_m", "naip_item", "naip_date", "conf", "conf_v2",
            "verify_scan", "privacy", "export"]
    t = pq.read_table(args.candidates, columns=cols)
    d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in cols}
    pu = np.isin(d["privacy"], ("public", "unknown"))
    c2 = d["conf_v2"].astype(float)
    vs = np.nan_to_num(d["verify_scan"].astype(float), nan=0.0)
    scope = pu & ((c2 >= args.min_conf_v2) | (vs >= args.min_verify))
    labels = load_review_labels()
    labelled = np.isin(d["id"], list(labels))
    exported = d["export"].astype(bool)
    keep = scope | exported | labelled
    log.info("candidates: scope %d (public/unknown, v2 >= %.2f or verify_scan >= %.2f), export %d (%d outside scope), "
             "reviewed %d (%d outside scope) -> %d", int(scope.sum()), args.min_conf_v2, args.min_verify,
             int(exported.sum()), int((exported & ~scope).sum()), int(labelled.sum()), int((labelled & ~scope).sum()),
             int(keep.sum()))
    ii = np.flatnonzero(keep)
    parts = [{"id": d["id"][ii], "kind": np.full(len(ii), "cand", dtype=object), "state": d["state"][ii],
              "lon": d["lon"][ii].astype(float), "lat": d["lat"][ii].astype(float),
              "box_w_m": d["box_w_m"][ii].astype(float), "box_h_m": d["box_h_m"][ii].astype(float),
              "ref_item": d["naip_item"][ii], "conf_v2": c2[ii], "verify_scan": vs[ii]}]
    # known courts: national detections near an OSM court (calibration of persistence on real courts)
    dt_ = pq.read_table(args.detections, columns=["id", "conf", "lon", "lat", "box_w_m", "box_h_m", "verify", "item_id",
                                                  "state", "near_known_osm"])
    dd = {k: dt_.column(k).to_numpy(zero_copy_only=False) for k in dt_.column_names}
    known = np.flatnonzero((dd["near_known_osm"] != "") & (dd["conf"].astype(float) >= args.known_min_conf))
    known = np.sort(rng.choice(known, size=min(args.known_n, len(known)), replace=False))
    parts.append({"id": dd["id"][known], "kind": np.full(len(known), "known", dtype=object), "state": dd["state"][known],
                  "lon": dd["lon"][known].astype(float), "lat": dd["lat"][known].astype(float),
                  "box_w_m": dd["box_w_m"][known].astype(float), "box_h_m": dd["box_h_m"][known].astype(float),
                  "ref_item": np.array([str(x).split("~")[0] for x in dd["item_id"][known]], dtype=object),
                  "conf_v2": dd["conf"][known].astype(float),
                  "verify_scan": np.nan_to_num(dd["verify"][known].astype(float), nan=0.0)})
    log.info("known-court sample: %d of %d detections near OSM with conf >= %.2f", len(known),
             int(((dd["near_known_osm"] != "") & (dd["conf"].astype(float) >= args.known_min_conf)).sum()),
             args.known_min_conf)
    # official truth courts the national scan missed (no detection >= 0.1 within 40 m of the point / 15 m of the
    # polygon, the truth.evaluate rule): does another year show them?
    tc, _ = load_truth()
    tm = (np.isin(tc["status"], LIVE_STATUS) & (tc["feature"] == "court") & ~np.isin(tc["indoor"], HIDDEN)
          & (tc["tier"] == "official") & np.isin(tc["precision"], COURT_LEVEL))
    ti = np.flatnonzero(tm)
    tg = truth_geoms_m({k: v[ti] for k, v in tc.items()})
    is_poly = np.array([b is not None for b in tc["footprint_wkb"][ti]])
    dp = pts_m(dd["lon"].astype(float), dd["lat"].astype(float))
    tree = STRtree(dp)
    hit = np.zeros(len(ti), bool)
    for flag, r in ((False, 40.0), (True, 15.0)):
        sel = np.flatnonzero(is_poly == flag)
        q, _ = tree.query(tg[sel], predicate="dwithin", distance=r)
        hit[sel[np.unique(q)]] = True
    miss = ti[~hit]
    gm = tg[~hit]
    b = shapely.bounds(gm)
    w = np.where(is_poly[~hit], np.clip(b[:, 2] - b[:, 0], 10, 60), 26.0)
    h = np.where(is_poly[~hit], np.clip(b[:, 3] - b[:, 1], 10, 60), 26.0)
    cen = shapely.centroid(gm)
    from pyproj import Transformer

    inv = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)
    lo, la = inv.transform(shapely.get_x(cen), shapely.get_y(cen))
    parts.append({"id": np.array([f"truth:{x}" for x in tc["truth_id"][miss]], dtype=object),
                  "kind": np.full(len(miss), "truth_miss", dtype=object), "state": tc["state"][miss].astype(object),
                  "lon": np.asarray(lo, float), "lat": np.asarray(la, float), "box_w_m": w.astype(float),
                  "box_h_m": h.astype(float), "ref_item": np.full(len(miss), "", dtype=object),
                  "conf_v2": np.zeros(len(miss)), "verify_scan": np.zeros(len(miss))})
    log.info("truth courts the scan missed: %d of %d court-level official records", len(miss), len(ti))
    out = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    out["labelled"] = np.array([labels.get(str(i), ("", ""))[0] for i in out["id"]], dtype=object)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({k: (pa.array(v.tolist(), pa.string()) if v.dtype == object else pa.array(v))
                             for k, v in out.items()}), args.out, compression="zstd")
    summ = {"rows": len(out["id"]), "by_kind": dict(Counter(out["kind"].tolist())),
            "labelled": dict(Counter(x for x in out["labelled"].tolist() if x)),
            "rule": {"cand": f"privacy in (public, unknown) and (conf_v2 >= {args.min_conf_v2} or verify_scan >= "
                             f"{args.min_verify}), plus every v3 export and every reviewed candidate",
                     "known": f"random {args.known_n} national detections near an OSM court with conf >= "
                              f"{args.known_min_conf} (seed {args.seed})",
                     "truth_miss": "official court-level truth records with no national detection >= 0.1 within 40 m "
                                   "(points) / 15 m (polygons)"}}
    write_json(args.out.with_suffix(".json"), summ)
    log.info("input: %s -> %s", json.dumps(summ["by_kind"]), args.out)


# =========================================================================== run (node)
class Fetcher:
    def __init__(self, cat: Any, workers: int) -> None:
        self.cat = cat
        self.workers = workers

    def fetch(self, lon: np.ndarray, lat: np.ndarray, st: np.ndarray, years: np.ndarray) -> dict[str, np.ndarray]:
        """4-band 320 px chips for each (point, year): rgb, nir, ok, valid, item index."""
        from .naip_index import GroupedReader, plan_windows

        n = len(lon)
        rgb = np.zeros((n, CHIP_PX, CHIP_PX, 3), np.uint8)
        nir = np.zeros((n, CHIP_PX, CHIP_PX), np.uint8)
        ok = np.zeros(n, bool)
        valid = np.zeros(n, np.float32)
        item = np.full(n, -1, np.int64)
        for y in np.unique(years):
            rows = np.flatnonzero(years == y)
            it = self.cat.select(lon[rows], lat[rows], st[rows], half_m=CHIP_PX * GSD / 2, year=int(y))
            item[rows] = it
            wp = plan_windows(self.cat, it, lon[rows], lat[rows], CHIP_PX, GSD)

            def handle(key: Any, img: np.ndarray | None, err: str, rows: np.ndarray = rows) -> int:
                j = int(rows[int(key)])
                if img is not None:
                    v = float((img.max(axis=2) > 0).mean())
                    valid[j] = v
                    if v >= 0.9:
                        rgb[j] = img[:, :, :3]
                        nir[j] = img[:, :, 3] if img.shape[2] > 3 else 0
                        ok[j] = True
                return j

            reader = GroupedReader(self.cat, workers=self.workers, bands=(1, 2, 3, 4))
            list(reader.run(wp, np.arange(len(rows)), handle, progress_every=10 ** 9))
        return {"rgb": rgb, "nir": nir, "ok": ok, "valid": valid, "item": item}


def year_jobs(cat: Any, lon: np.ndarray, lat: np.ndarray, ref_item: np.ndarray, max_other: int,
              min_year: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(row, year, is_ref) for every row: the scan's year (from its NAIP item, else the newest) + up to
    ``max_other`` other years with an item covering the point, newest first."""
    import shapely

    pos = {str(k): i for i, k in enumerate(cat.ids)}
    pts = shapely.points(lon, lat)
    p, it = cat.tree.query(pts, predicate="intersects")
    yrs = cat.years[it]
    keep = yrs >= min_year
    p, yrs = p[keep], yrs[keep]
    order = np.lexsort((-yrs, p))
    p, yrs = p[order], yrs[order]
    rows, years, isref = [], [], []
    starts = np.searchsorted(p, np.arange(len(lon)))
    ends = np.searchsorted(p, np.arange(len(lon)), side="right")
    for i in range(len(lon)):
        ys = list(dict.fromkeys(yrs[starts[i]:ends[i]].tolist()))  # unique, newest first
        if not ys:
            continue
        k = pos.get(str(ref_item[i]).split("~")[0]) if ref_item[i] else None
        ref = int(cat.years[k]) if k is not None else ys[0]
        if ref not in ys:
            ys.insert(0, ref)
        others = [y for y in ys if y != ref][:max_other]
        for y in [ref, *others]:
            rows.append(i)
            years.append(y)
            isref.append(y == ref)
    return np.asarray(rows, np.int64), np.asarray(years, np.int64), np.asarray(isref, bool)


def score_chunk(det: Any, ver: Any, f: dict[str, np.ndarray], bw: np.ndarray, bh: np.ndarray, kind: np.ndarray,
                batch: int) -> dict[str, np.ndarray]:
    import torch

    n = len(f["ok"])
    out = {k: np.full(n, np.nan, np.float32) for k in ("conf_box", "conf_near", "det_dx_m", "det_dy_m", "det_w_m",
                                                        "det_h_m", "verify_box", "verify_det", "ndvi_box", "ndvi_ring",
                                                        "bright_box", "std_box", "blue_box")}
    good = np.flatnonzero(f["ok"])
    c = CHIP_PX / 2
    for b0 in range(0, len(good), batch):
        gb = good[b0:b0 + batch]
        x = torch.from_numpy(f["rgb"][gb])
        x_pre = det.preprocess(x)
        dets = det.detect(x, conf=0.05, x_pre=x_pre).cpu().numpy()
        cand_boxes = np.zeros((len(gb), 6), np.float32)
        det_boxes, det_rows = [], []
        for q, j in enumerate(gb.tolist()):
            dq = dets[dets[:, 0] == q, 1:6]
            box = box_px(bw[j], bh[j])
            cb, kb = best_overlap(dq, box)
            r_m = 25.0 if kind[j] == "truth_miss" else max(10.0, 0.5 * math.hypot(bw[j], bh[j]))
            cn, kn = near_best(dq, c, c, r_m / GSD)
            out["conf_box"][j] = cb
            out["conf_near"][j] = cn
            k = kb if cb >= cn else kn
            if k >= 0:
                db = dq[k]
                out["det_dx_m"][j] = ((db[0] + db[2]) / 2 - c) * GSD
                out["det_dy_m"][j] = ((db[1] + db[3]) / 2 - c) * GSD
                out["det_w_m"][j] = (db[2] - db[0]) * GSD
                out["det_h_m"][j] = (db[3] - db[1]) * GSD
                det_boxes.append([q, *db[:4], db[4]])
                det_rows.append(j)
            cand_boxes[q] = [q, *box, 1.0]
        if ver is not None:
            out["verify_box"][gb] = ver.score_boxes(x_pre, torch.from_numpy(cand_boxes).to(ver.device),
                                                    CHIP_PX).float().cpu().numpy()
            if det_boxes:
                out["verify_det"][np.asarray(det_rows)] = ver.score_boxes(
                    x_pre, torch.tensor(det_boxes, dtype=torch.float32, device=ver.device), CHIP_PX).float().cpu().numpy()
    # pixel statistics in the box and in a ring around it (NDVI from NAIP's NIR band)
    for j in good.tolist():
        b = box_px(bw[j], bh[j])
        x0, y0, x1, y1 = int(b[0]), int(b[1]), int(math.ceil(b[2])), int(math.ceil(b[3]))
        r = f["rgb"][j, y0:y1, x0:x1].astype(np.float32)
        nr = f["nir"][j, y0:y1, x0:x1].astype(np.float32)
        red = r[..., 0]
        ndvi = (nr - red) / np.maximum(nr + red, 1.0)
        out["ndvi_box"][j] = float(ndvi.mean())
        g = r.mean(axis=2)
        out["bright_box"][j] = float(g.mean())
        out["std_box"][j] = float(g.std())
        out["blue_box"][j] = float((r[..., 2] - r[..., 0]).mean())
        w2, h2 = (x1 - x0), (y1 - y0)
        X0, Y0 = max(0, x0 - w2 // 2), max(0, y0 - h2 // 2)
        X1, Y1 = min(CHIP_PX, x1 + w2 // 2), min(CHIP_PX, y1 + h2 // 2)
        R = f["rgb"][j, Y0:Y1, X0:X1, 0].astype(np.float32)
        N = f["nir"][j, Y0:Y1, X0:X1].astype(np.float32)
        nd = (N - R) / np.maximum(N + R, 1.0)
        mask = np.ones(nd.shape, bool)
        mask[y0 - Y0:y1 - Y0, x0 - X0:x1 - X0] = False
        out["ndvi_ring"][j] = float(nd[mask].mean()) if mask.any() else np.nan
    return out


def cmd_run(args: argparse.Namespace) -> None:
    import torch

    from .cropcls import CropClassifier
    from .fastdet import FastDetector
    from .naip_index import NaipCatalog

    t0 = time.monotonic()
    t = pq.read_table(args.input)
    d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in t.column_names}
    n = len(d["id"])
    order = np.lexsort((morton(d["lon"].astype(float), d["lat"].astype(float)), d["state"].astype(str)))
    if args.limit:
        order = order[:args.limit]
    cat = NaipCatalog.load(args.naip_dir, min_year=args.min_year)
    parts = args.out / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    chunks = [order[k:k + args.chunk] for k in range(0, len(order), args.chunk)]
    kk, nn = (int(v) for v in args.shard.split("/")) if args.shard else (0, 1)
    todo = [(ci, ch) for ci, ch in enumerate(chunks) if ci % nn == kk and not (parts / f"part_{ci:05d}.done").exists()]
    log.info("%d rows in %d chunks of %d; shard %d/%d: %d chunks to do", n, len(chunks), args.chunk, kk, nn, len(todo))
    if not todo:
        return
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    det = FastDetector(args.weights, imgsz=args.imgsz, device=device, half=device != "cpu")
    ver = CropClassifier.load(args.verifier) if args.verifier else None
    fetcher = Fetcher(cat, args.workers)
    lon, lat = d["lon"].astype(float), d["lat"].astype(float)
    st = d["state"].astype(str)

    def prep(ch: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, np.ndarray], float]:
        tf = time.monotonic()
        rows, years, isref = year_jobs(cat, lon[ch], lat[ch], d["ref_item"][ch], args.max_other, args.min_year)
        g = ch[rows]
        f = fetcher.fetch(lon[g], lat[g], st[g], years)
        return g, years, isref, f, time.monotonic() - tf

    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(prep, todo[0][1])
    chips_total = 0
    for q, (ci, _ch) in enumerate(todo):
        g, years, isref, f, tfetch = fut.result()
        if q + 1 < len(todo):  # fetch the next chunk while the GPU scores this one
            fut = pool.submit(prep, todo[q + 1][1])
        ts = time.monotonic()
        s = score_chunk(det, ver, f, d["box_w_m"][g].astype(float), d["box_h_m"][g].astype(float),
                        d["kind"][g].astype(str), args.batch)
        # year-to-year change: 32 x 32 grey patch of the box vs the scan year's patch of the same row
        corr_ref = np.full(len(g), np.nan, np.float32)
        mad_ref = np.full(len(g), np.nan, np.float32)
        patches: dict[int, np.ndarray] = {}
        for j in np.flatnonzero(f["ok"]).tolist():
            b = box_px(float(d["box_w_m"][g[j]]), float(d["box_h_m"][g[j]]))
            patches[j] = patch32(f["rgb"][j].mean(axis=2), b)
        ref_of: dict[int, int] = {}
        for j in np.flatnonzero(isref).tolist():
            ref_of[int(g[j])] = j
        for j, pj in patches.items():
            r = ref_of.get(int(g[j]))
            if r is None or r == j or r not in patches:
                continue
            corr_ref[j] = corr(pj, patches[r])
            a, b_ = pj, patches[r]
            mad_ref[j] = float(np.abs((a - a.mean()) / (a.std() + 1e-3) - (b_ - b_.mean()) / (b_.std() + 1e-3)).mean())
        it = f["item"]
        cols = {
            "id": pa.array(d["id"][g].tolist(), pa.string()), "kind": pa.array(d["kind"][g].tolist(), pa.string()),
            "year": pa.array(years.astype(np.int32)), "is_ref": pa.array(isref),
            "item_id": pa.array([str(cat.ids[k]) if k >= 0 else "" for k in it], pa.string()),
            "item_state": pa.array([str(cat.states[k]) if k >= 0 else "" for k in it], pa.string()),
            "date": pa.array([cat.date_str(k) if k >= 0 else "" for k in it], pa.string()),
            "gsd": pa.array([float(cat.gsd[k]) if k >= 0 else np.nan for k in it]),
            "ok": pa.array(f["ok"]), "valid": pa.array(f["valid"]),
            "corr_ref": pa.array(corr_ref), "mad_ref": pa.array(mad_ref),
            **{k: pa.array(v) for k, v in s.items()},
        }
        tmp = parts / f".part_{ci:05d}.parquet"
        pq.write_table(pa.table(cols), tmp, compression="zstd")
        tmp.replace(parts / f"part_{ci:05d}.parquet")
        (parts / f"part_{ci:05d}.done").write_text(json.dumps({"rows": len(g), "chips_ok": int(f["ok"].sum()),
                                                               "fetch_s": round(tfetch, 1),
                                                               "score_s": round(time.monotonic() - ts, 1)}))
        chips_total += len(g)
        el = time.monotonic() - t0
        log.info("chunk %d (%d/%d): %d chips (%d ok), fetch %.0f s, score %.0f s; %d chips in %.0f s (%.0f/s)", ci, q + 1,
                 len(todo), len(g), int(f["ok"].sum()), tfetch, time.monotonic() - ts, chips_total, el, chips_total / el)
        del f, s
    pool.shutdown()
    log.info("run done: %d chips in %.1f min", chips_total, (time.monotonic() - t0) / 60)


def cmd_merge(args: argparse.Namespace) -> None:
    files = sorted((args.out / "parts").glob("part_*.parquet"))
    t = pa.concat_tables([pq.read_table(f) for f in files])
    pq.write_table(t, args.out / "years.parquet", compression="zstd")
    log.info("merged %d parts, %d rows -> %s", len(files), t.num_rows, args.out / "years.parquet")


# =========================================================================== features
FEATURE_COLS = ["years_checked", "years_seen", "years_seen50", "other_checked", "other_seen", "other_seen50",
                "other_hires_checked", "other_hires_seen", "conf_ref_fresh", "verify_ref_fresh", "conf_max_other",
                "conf_mean_other", "verify_max_other", "verify_mean_other", "first_year", "first_seen_year",
                "last_seen_year", "ref_year", "ndvi_ref", "ndvi_oldest", "ndvi_max_other", "slab_appeared",
                "slab_gone", "corr_min_other", "corr_mean_other", "persist", "leafoff_checked", "leafoff_seen"]


def year_features(y: dict[str, np.ndarray], seen: float = SEEN) -> dict[str, np.ndarray]:
    """Per-id features from the long id x year table (rows grouped by id)."""
    ids = y["id"]
    order = np.lexsort((-y["year"], ids))
    y = {k: v[order] for k, v in y.items()}
    ids = y["id"]
    uniq, start = np.unique(ids, return_index=True)
    end = np.r_[start[1:], len(ids)]
    conf = np.maximum(np.nan_to_num(y["conf_box"], nan=0.0), np.nan_to_num(y["conf_near"], nan=0.0))
    # the verifier on an arbitrary box (blurry 1 m flight, bare ground) is out of distribution and often scores high
    # (README v3 "snap"), so a year's verifier score counts only on the detection v2 made there (0 when none)
    ver = np.nan_to_num(y["verify_det"], nan=0.0)
    ok = y["ok"].astype(bool)
    month = np.array([int(s[5:7]) if s else 0 for s in y["date"]])
    leafoff = np.isin(month, (11, 12, 1, 2, 3))  # NAIP flights outside the growing season (FL/GA 2021, some Nov)
    f: dict[str, list[Any]] = {k: [] for k in ["id", *FEATURE_COLS]}
    for i, (s, e) in enumerate(zip(start, end)):
        sl = slice(s, e)
        o = ok[sl]
        ref = y["is_ref"][sl] & o
        oth = ~y["is_ref"][sl] & o
        c, v, yr, g = conf[sl], ver[sl], y["year"][sl], y["gsd"][sl]
        nd = y["ndvi_box"][sl]
        f["id"].append(uniq[i])
        f["years_checked"].append(int(o.sum()))
        f["years_seen"].append(int((o & (c >= seen)).sum()))
        f["years_seen50"].append(int((o & (c >= 0.5)).sum()))
        f["other_checked"].append(int(oth.sum()))
        f["other_seen"].append(int((oth & (c >= seen)).sum()))
        f["other_seen50"].append(int((oth & (c >= 0.5)).sum()))
        hr = oth & (g <= 0.65)
        f["other_hires_checked"].append(int(hr.sum()))
        f["other_hires_seen"].append(int((hr & (c >= seen)).sum()))
        f["conf_ref_fresh"].append(float(c[ref].max()) if ref.any() else np.nan)
        f["verify_ref_fresh"].append(float(v[ref].max()) if ref.any() else np.nan)
        f["conf_max_other"].append(float(c[oth].max()) if oth.any() else np.nan)
        f["conf_mean_other"].append(float(c[oth].mean()) if oth.any() else np.nan)
        f["verify_max_other"].append(float(v[oth].max()) if oth.any() else np.nan)
        f["verify_mean_other"].append(float(v[oth].mean()) if oth.any() else np.nan)
        f["first_year"].append(int(yr[o].min()) if o.any() else -1)
        sy = yr[o & (c >= seen)]
        f["first_seen_year"].append(int(sy.min()) if len(sy) else -1)
        f["last_seen_year"].append(int(sy.max()) if len(sy) else -1)
        f["ref_year"].append(int(yr[ref][0]) if ref.any() else -1)
        nref = float(nd[ref][0]) if ref.any() else np.nan
        f["ndvi_ref"].append(nref)
        oo = o & (yr == yr[o].min()) if o.any() else o
        nold = float(nd[oo][0]) if oo.any() else np.nan
        f["ndvi_oldest"].append(nold)
        f["ndvi_max_other"].append(float(np.nanmax(nd[oth])) if oth.any() else np.nan)
        later = o & (yr > (yr[ref][0] if ref.any() else 9999))
        # vegetation before, paved in the scan year: built (or a construction pad) since the oldest flight
        f["slab_appeared"].append(bool(ref.any() and oo.any() and not ref[oo].any() and nold >= 0.2 and nref < 0.08))
        # paved in the scan year, vegetated again in a later flight: gone (rare; scan years are the newest)
        f["slab_gone"].append(bool(ref.any() and later.any() and nref < 0.08 and float(np.nanmax(nd[later])) >= 0.2))
        cr = y["corr_ref"][sl][oth]
        cr = cr[np.isfinite(cr)]
        f["corr_min_other"].append(float(cr.min()) if len(cr) else np.nan)
        f["corr_mean_other"].append(float(cr.mean()) if len(cr) else np.nan)
        f["persist"].append(float((o & (c >= seen)).sum() / max(1, o.sum())))
        lo = oth & leafoff[sl]
        f["leafoff_checked"].append(int(lo.sum()))
        f["leafoff_seen"].append(int((lo & (c >= seen)).sum()))
    return {k: np.asarray(v) for k, v in f.items()}


def load_years(path: Path) -> dict[str, np.ndarray]:
    t = pq.read_table(path)
    return {k: t.column(k).to_numpy(zero_copy_only=False) for k in t.column_names}


def cmd_features(args: argparse.Namespace) -> None:
    t0 = time.monotonic()
    y = load_years(args.years)
    f = year_features(y)
    ft = pa.table({k: pa.array(v.tolist() if v.dtype == object else v) for k, v in f.items()})
    pq.write_table(ft, args.years.with_name("features.parquet"), compression="zstd")
    cand = pq.read_table(args.candidates)
    pos = {k: i for i, k in enumerate(f["id"].tolist())}
    j = np.array([pos.get(k, -1) for k in cand.column("id").to_pylist()])
    hit = j >= 0
    cols = {k: cand.column(k) for k in cand.column_names}
    for k in FEATURE_COLS:
        v = f[k]
        if v.dtype == bool:
            arr = np.zeros(len(j), bool)
            arr[hit] = v[j[hit]]
            cols[k] = pa.array(arr)
        elif np.issubdtype(v.dtype, np.integer):
            arr = np.full(len(j), -1, np.int32)
            arr[hit] = v[j[hit]]
            cols[k] = pa.array(arr)
        else:
            arr = np.full(len(j), np.nan)
            arr[hit] = v[j[hit]]
            cols[k] = pa.array(arr)
    cols["multiyear_checked"] = pa.array(hit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(cols), args.out, compression="zstd")
    log.info("features for %d ids; %d of %d candidates checked -> %s (%.0f s)", len(f["id"]), int(hit.sum()), len(j),
             args.out, time.monotonic() - t0)


# =========================================================================== analysis of labelled / known groups
def cmd_labelled(args: argparse.Namespace) -> None:
    """How the features separate courts from look-alikes: reviewed candidates by C/N/U label, known OSM courts,
    and the scan's truth misses (does another year show them?)."""
    y = load_years(args.years)
    f = year_features(y)
    inp = pq.read_table(args.input, columns=["id", "kind", "labelled", "state", "conf_v2", "verify_scan"]).to_pydict()
    meta = {i: (k, lab, s) for i, k, lab, s in zip(inp["id"], inp["kind"], inp["labelled"], inp["state"])}
    groups: dict[str, list[int]] = {}
    for q, i in enumerate(f["id"].tolist()):
        k, lab, _ = meta.get(i, ("?", "", ""))
        groups.setdefault(k, []).append(q)
        if lab:
            groups.setdefault(f"label_{lab}", []).append(q)
    res: dict[str, Any] = {}
    for g, idx in sorted(groups.items()):
        idx = np.asarray(idx)
        e: dict[str, Any] = {"n": len(idx)}
        for k in ("years_checked", "years_seen", "other_checked", "other_seen", "other_seen50", "conf_max_other",
                  "conf_mean_other", "verify_mean_other", "corr_mean_other", "persist", "ndvi_ref"):
            v = f[k][idx].astype(float)
            v = v[np.isfinite(v)]
            e[k] = {"mean": round(float(v.mean()), 3) if len(v) else None,
                    "p25": round(float(np.quantile(v, 0.25)), 3) if len(v) else None,
                    "p50": round(float(np.median(v)), 3) if len(v) else None}
        oc = f["other_checked"][idx]
        os_ = f["other_seen"][idx]
        has = oc > 0
        e["share_seen_in_all_other"] = round(float((os_[has] == oc[has]).mean()), 3) if has.any() else None
        e["share_seen_in_no_other"] = round(float((os_[has] == 0).mean()), 3) if has.any() else None
        e["share_seen50_in_any_other"] = round(float((f["other_seen50"][idx][has] > 0).mean()), 3) if has.any() else None
        e["slab_appeared"] = int(f["slab_appeared"][idx].sum())
        res[g] = e
    write_json(args.out, res)
    print("| group | n | other years checked (mean) | seen in all other | seen in none | v2 >= 0.5 in any other | "
          "conf mean other | corr mean | slab appeared |")
    print("|---|---|---|---|---|---|---|---|---|")
    for g, e in res.items():
        print(f"| {g} | {e['n']} | {e['other_checked']['mean']} | {e['share_seen_in_all_other']} | "
              f"{e['share_seen_in_no_other']} | {e['share_seen50_in_any_other']} | {e['conf_mean_other']['mean']} | "
              f"{e['corr_mean_other']['mean']} | {e['slab_appeared']} |")


# =========================================================================== v4 score
SCORE_SINGLE = ["conf", "conf_v2", "verify_scan", "verify3", "conf_ref_fresh", "verify_ref_fresh"]
SCORE_MULTI = ["conf_mean_other", "conf_max_other", "verify_mean_other", "verify_max_other", "other_seen", "persist",
               "corr_mean_other", "slab_appeared", "years_seen50"]


def review_dirs(extra: list[Path] | None = None) -> None:
    for p in extra or []:
        REVIEW_SETS.setdefault(p.name, p)


def cmd_fit_score(args: argparse.Namespace) -> None:
    """Logistic model P(court visible | single-year + multi-year features), fitted on every blind review label
    (C = 1, N or U = 0), L2-regularised, features standardised. Cross-validated AUC is logged against the single-year
    features alone. Writes ``score_v4`` (and ``reviewed`` = used for training) into the v4 candidates file."""
    review_dirs(args.review_dir)
    labs = load_review_labels()
    feats = SCORE_SINGLE + SCORE_MULTI
    t = pq.read_table(args.candidates)
    d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in ["id", *feats]}
    pos = {k: i for i, k in enumerate(d["id"].tolist())}
    ii = np.array([pos[k] for k in labs if k in pos])
    lab = np.array([labs[k][0] for k in labs if k in pos])
    xall = np.column_stack([np.nan_to_num(d[f].astype(float), nan=0.0) for f in feats])
    mu, sd = xall[ii].mean(0), xall[ii].std(0) + 1e-9
    y = (lab == "C").astype(float)

    def fit(x: np.ndarray, yy: np.ndarray) -> tuple[np.ndarray, float]:
        w, b = np.zeros(x.shape[1]), 0.0
        for _ in range(5000):
            p = 1 / (1 + np.exp(-(x @ w + b)))
            g = p - yy
            w -= 0.1 * (x.T @ g / len(yy) + args.l2 * w / len(yy))
            b -= 0.1 * float(g.mean())
        return w, b

    def auc(yy: np.ndarray, s: np.ndarray) -> float:
        o = np.argsort(s)
        r = np.empty(len(s))
        r[o] = np.arange(1, len(s) + 1)
        n1 = yy.sum()
        return float((r[yy.astype(bool)].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(yy) - n1)))

    rng = np.random.default_rng(0)
    cv: dict[str, float] = {}
    for name, cols in (("single", list(range(len(SCORE_SINGLE)))), ("single+multi", list(range(len(feats))))):
        x = (xall[ii][:, cols] - mu[cols]) / sd[cols]
        aucs = []
        for _ in range(10):
            fold = rng.integers(0, 5, len(y))
            s = np.zeros(len(y))
            for k in range(5):
                w, b = fit(x[fold != k], y[fold != k])
                s[fold == k] = x[fold == k] @ w + b
            aucs.append(auc(y, s))
        cv[name] = round(float(np.mean(aucs)), 3)
    w, b = fit((xall[ii] - mu) / sd, y)
    score = 1 / (1 + np.exp(-(((xall - mu) / sd) @ w + b)))
    model = {"features": feats, "mean": mu.tolist(), "std": sd.tolist(), "w": w.tolist(), "b": b, "l2": args.l2,
             "target": "C (court markings or hoops visible) vs N or U", "labels": {x: int((lab == x).sum()) for x in "CNU"},
             "review_sets": {k: str(v) for k, v in REVIEW_SETS.items()}, "cv_auc": cv,
             "note": "trained on blind model-assistant labels (not gold); scores are a ranking, not calibrated precision"}
    write_json(args.candidates.with_name("score_model.json"), model)
    reviewed = np.zeros(len(score), bool)
    reviewed[ii] = True
    for name, v in (("score_v4", pa.array(score.astype(np.float32))), ("reviewed", pa.array(reviewed))):
        t = t.set_column(t.column_names.index(name), name, v) if name in t.column_names else t.append_column(name, v)
    pq.write_table(t, args.candidates, compression="zstd")
    log.info("score_v4 fitted on %s labels; CV AUC %s -> %s", model["labels"], cv, args.candidates)


# =========================================================================== rules
def parse_rule(text: str) -> dict[str, Any]:
    """'conf=0.5,v2=0.5,verify=0.98|states,seen=1,seen_frac=0.5,drop_unseen=1,other_conf=0.5' -> rule dict.

    verify=states uses the v3 per-state thresholds; verify=X a flat threshold. ``seen`` = minimum other years with
    v2 >= 0.25 at the box (only for rows with other years checked; rows without other years pass), ``persist`` =
    minimum share of checked years seen, ``drop_unseen=1`` = drop candidates no other checked year shows,
    ``other_conf`` = minimum mean v2 score over the other years, ``or_v3=1`` = also keep everything v3 exported,
    ``score`` = minimum ``score_v4`` (multi-year logistic score, ``fit-score``), ``v3_score=X`` = also keep v3 exports
    whose score_v4 >= X (implies or_v3 for those only)."""
    r: dict[str, Any] = {"conf": 0.5, "v2": 0.5, "verify": "states", "seen": 0, "persist": 0.0, "drop_unseen": False,
                         "other_conf": 0.0, "or_v3": False, "verify_col": "verify_scan", "min_other_checked": 0,
                         "score": -1.0, "v3_score": None}
    for part in text.split(","):
        k, _, v = part.partition("=")
        if k in ("conf", "v2", "persist", "other_conf", "score"):
            r[k] = float(v)
        elif k == "v3_score":
            r[k] = float(v)
        elif k in ("seen", "min_other_checked"):
            r[k] = int(v)
        elif k in ("drop_unseen", "or_v3"):
            r[k] = v not in ("0", "false", "")
        elif k == "verify":
            r[k] = v if v == "states" else float(v)
        elif k == "col":
            r["verify_col"] = v
    return r


def select_rule(d: dict[str, np.ndarray], r: dict[str, Any], thresholds: dict[str, float]) -> np.ndarray:
    ver = np.nan_to_num(d[r["verify_col"]].astype(float), nan=-1.0)
    if r["verify"] == "states":
        vthr = np.array([thresholds.get(s, 0.98) for s in d["state"]])
    else:
        vthr = np.full(len(ver), float(r["verify"]))
    sel = ((d["privacy"] == "public") & np.isfinite(d["p_home"].astype(float)) & (d["conf"].astype(float) >= r["conf"])
           & (d["conf_v2"].astype(float) >= r["v2"]) & (ver >= vthr))
    oc = d["other_checked"].astype(int)
    has = oc > 0
    if r["min_other_checked"]:
        sel &= oc >= r["min_other_checked"]
    if r["seen"]:
        sel &= ~has | (d["other_seen"].astype(int) >= r["seen"])
    if r["persist"] > 0:
        sel &= ~has | (d["persist"].astype(float) >= r["persist"])
    if r["drop_unseen"]:
        sel &= ~has | (d["other_seen"].astype(int) > 0)
    if r["other_conf"] > 0:
        sel &= ~has | (np.nan_to_num(d["conf_mean_other"].astype(float), nan=0.0) >= r["other_conf"])
    if r["score"] > -1.0:
        sel &= np.nan_to_num(d["score_v4"].astype(float), nan=-1.0) >= r["score"]
    if r["or_v3"]:
        sel |= d["export"].astype(bool)
    if r["v3_score"] is not None:
        sel |= d["export"].astype(bool) & (np.nan_to_num(d["score_v4"].astype(float), nan=-1.0) >= r["v3_score"])
    return sel


GRID_COLS = ["id", "state", "lon", "lat", "conf", "conf_v2", "privacy", "p_home", "verify_scan", "verify3", "export",
             "multiyear_checked", "score_v4", *FEATURE_COLS]


def cmd_grid(args: argparse.Namespace) -> None:
    from .v3 import _heldout_runs, _park_sum, planimetric_precision

    t = pq.read_table(args.candidates, columns=GRID_COLS)
    d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in t.column_names}
    thr = {s: float(e["threshold"]) for s, e in read_json(args.thresholds)["states"].items()}
    labels = load_review_labels()
    lab = np.array([labels.get(i, ("", ""))[0] for i in d["id"]], dtype=object)
    tmp = args.out.with_name(".grid_tmp.parquet")
    rows = []
    for text in args.rules:
        r = parse_rule(text)
        sel = select_rule(d, r, thr)
        sub = t.filter(pa.array(sel))
        pq.write_table(sub.select(["lon", "lat", "conf", "state"]).set_column(2, "conf", pa.array(np.ones(sub.num_rows))),
                       tmp)
        res = _heldout_runs(tmp, args.area, list(args.heldout), [0.5])
        park = _park_sum(res["all"]["in_covered_parks"], "0.50")
        plan = planimetric_precision({"lon": d["lon"][sel], "lat": d["lat"][sel], "conf": np.ones(int(sel.sum())),
                                      "verify": np.ones(int(sel.sum()))},
                                     [s for s in args.heldout if "planimetrics" in s], [0.5], (0.0,))
        pp = [(v["c0.50_v0.00"]["matched"], v["c0.50_v0.00"]["detections"]) for v in plan.values()]
        by = Counter(d["state"][sel].tolist())
        lc = Counter(lab[sel & (lab != "")].tolist())
        v3 = d["export"].astype(bool)
        rows.append({"rule": text, "selected": int(sel.sum()), "added_vs_v3": int((sel & ~v3).sum()),
                     "dropped_vs_v3": int((v3 & ~sel).sum()), "top_state_share": round(by.most_common(1)[0][1] /
                                                                                        max(1, sel.sum()), 3) if by else 0,
                     "TX": by.get("TX", 0), "MO": by.get("MO", 0), "OH": by.get("OH", 0), "CA": by.get("CA", 0),
                     "labelled_C": lc.get("C", 0), "labelled_N": lc.get("N", 0), "labelled_U": lc.get("U", 0),
                     "heldout_in_park_matched": park[1], "heldout_in_park": park[0],
                     "planimetric_matched": sum(m for m, _ in pp), "planimetric": sum(n for _, n in pp),
                     "recall_heldout_not_in_osm_ge150": (res["not_in_osm_ge150"]["recall"] or {}).get("0.50")})
    tmp.unlink(missing_ok=True)
    write_json(args.out, {"rules": rows})
    print("| rule | selected | +/- vs v3 | CA share | TX/MO/OH | labelled C/N/U | held-out park | DC+Ramsey | "
          "recall held-out OSM-lacking >=150 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r_ in rows:
        print(f"| {r_['rule']} | {r_['selected']} | +{r_['added_vs_v3']} / -{r_['dropped_vs_v3']} | "
              f"{r_['CA'] / max(1, r_['selected']):.2f} | {r_['TX']}/{r_['MO']}/{r_['OH']} | "
              f"{r_['labelled_C']}/{r_['labelled_N']}/{r_['labelled_U']} | {r_['heldout_in_park_matched']}/"
              f"{r_['heldout_in_park']} | {r_['planimetric_matched']}/{r_['planimetric']} | "
              f"{r_['recall_heldout_not_in_osm_ge150']} |")


def cmd_select(args: argparse.Namespace) -> None:
    t = pq.read_table(args.candidates)
    d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in GRID_COLS}
    thr = {s: float(e["threshold"]) for s, e in read_json(args.thresholds)["states"].items()}
    r = parse_rule(args.rule)
    sel = select_rule(d, r, thr)
    if args.exclude and args.exclude.exists():  # reviewed by eye and rejected (N): never exported
        bad = {ln.split()[0] for ln in args.exclude.read_text(encoding="utf-8").splitlines()
               if ln.strip() and not ln.startswith("#")}
        drop = sel & np.isin(d["id"], list(bad))
        log.info("excluded by review: %d", int(drop.sum()))
        sel &= ~drop
    name = "export_v4"
    t = t.set_column(t.column_names.index(name), name, pa.array(sel)) if name in t.column_names \
        else t.append_column(name, pa.array(sel))
    pq.write_table(t, args.candidates, compression="zstd")
    by = Counter(d["state"][sel].tolist())
    write_json(args.candidates.with_name("selection.json"), {
        "rule": {**r, "text": args.rule, "privacy": "public",
                 "per_state_thresholds": args.thresholds.as_posix() if r["verify"] == "states" else None,
                 "require_phome": True, "exclude": args.exclude.as_posix() if args.exclude else None},
        "selected": int(sel.sum()), "v3_exported": int(d["export"].astype(bool).sum()),
        "added_vs_v3": int((sel & ~d["export"].astype(bool)).sum()),
        "dropped_vs_v3": int((d["export"].astype(bool) & ~sel).sum()), "by_state": dict(by.most_common())})
    log.info("v4 selection: %d (v3 %d; +%d / -%d)", int(sel.sum()), int(d["export"].astype(bool).sum()),
             int((sel & ~d["export"].astype(bool)).sum()), int((d["export"].astype(bool) & ~sel).sum()))


EVIDENCE_KEYS = ("years_checked", "years_seen", "first_seen_year")


def cmd_app_ready(args: argparse.Namespace) -> None:
    """v3's app_ready format (export_app schema, detected features add ``site``) plus optional evidence fields on
    detected features: years_checked (NAIP flights looked at), years_seen (flights where v2 scores >= 0.25 at the
    box) and first_seen_year. OSM features are copied from v3's files unchanged."""
    from .export_app import PROPERTY_KEYS, SITE_VALUES, _feature, validate
    from .national import state_names, state_slug

    t = pq.read_table(args.candidates, columns=["id", "state", "lon", "lat", "conf", "verify", "verify_scan", "conf2",
                                                "p_home", "privacy", "site", "export_v4", *EVIDENCE_KEYS])
    d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in t.column_names}
    sel = d["export_v4"].astype(bool) & (d["privacy"] == "public") & np.isfinite(d["p_home"].astype(float))
    if int((d["export_v4"].astype(bool) & ~sel).sum()):
        raise SystemExit("export_v4 holds non-public or CNN-unscored rows; refusing")
    names = state_names(args.states_file)
    dst = args.out / "app_ready"
    dst.mkdir(parents=True, exist_ok=True)
    rule = read_json(args.candidates.with_name("selection.json"))["rule"] if args.candidates.with_name(
        "selection.json").exists() else {}
    index: dict[str, Any] = {"operating_point": {"flag_col": "export_v4", **rule}, "updated": args.updated,
                             "evidence_fields": {"years_checked": "NAIP flights (2016-2023) looked at for this box",
                                                 "years_seen": "flights where the v2 detector scores >= 0.25 at the box",
                                                 "first_seen_year": "earliest such flight (-1: none)"},
                             "states": {}}
    exported = []
    for code in sorted(names):
        label = state_slug(names[code])
        v3_file = args.v3_app_ready / f"{code}.geojson"
        osm = []
        if v3_file.exists():
            osm = [f for f in read_json(v3_file)["features"] if f["properties"]["source"] != "detected"]
        idx = np.flatnonzero(sel & (d["state"] == code))
        exported.append(idx)
        det = []
        for i in idx[np.argsort(-d["conf"][idx].astype(float))]:
            f = _feature(round(float(d["lon"][i]), 6), round(float(d["lat"][i]), 6), {
                "id": str(d["id"][i]), "name": None, "source": "detected", "status": "candidate",
                "confidence": round(float(d["conf"][i]), 3), "hoops": None, "surface": None, "lit": None,
                "address": None, "region": label, "osm_id": None, "updated": args.updated})
            if d["site"][i] in SITE_VALUES:
                f["properties"]["site"] = str(d["site"][i])
            for k in EVIDENCE_KEYS:
                v = int(d[k][i])
                f["properties"][k] = v if v >= 0 or k == "first_seen_year" else None
            det.append(f)
        fc = {"type": "FeatureCollection", "features": osm + det}
        # validate with the shared schema (evidence fields stripped), then the evidence fields themselves
        core = {"type": "FeatureCollection", "features": [
            {**f, "properties": {k: v for k, v in f["properties"].items() if k not in EVIDENCE_KEYS}}
            for f in fc["features"]]}
        errors = validate(core)
        for f in det:
            p = f["properties"]
            extra = [k for k in p if k not in PROPERTY_KEYS and k != "site"]
            if extra != [k for k in EVIDENCE_KEYS if k in p]:
                errors.append(f"{p['id']}: unexpected keys {extra}")
            for k in EVIDENCE_KEYS:
                if p.get(k) is not None and not isinstance(p[k], int):
                    errors.append(f"{p['id']}: {k} must be int or null")
        if errors:
            raise SystemExit(f"{code}: validation failed: {errors[:5]}")
        write_json(dst / f"{code}.geojson", fc, indent=None)
        index["states"][code] = {"region": label, "osm": len(osm), "detected": len(det),
                                 "detected_by_site": dict(Counter(f["properties"].get("site") or "-" for f in det))}
    index["totals"] = {"osm": sum(v["osm"] for v in index["states"].values()),
                       "detected": sum(v["detected"] for v in index["states"].values())}
    ex = np.concatenate(exported) if exported else np.zeros(0, np.int64)
    pq.write_table(t.take(pa.array(ex, type=pa.int64())), dst / "exported_candidates.parquet", compression="zstd")
    write_json(dst / "index.json", index)
    log.info("app_ready v4: %s -> %s", index["totals"], dst)


# =========================================================================== review
def fetch_rgb(lon: np.ndarray, lat: np.ndarray, st: np.ndarray, year: int | None = None, px: int = CHIP_PX,
              items: np.ndarray | None = None, workers: int = 16) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """RGB chips (scan-year item when ``items`` gives NAIP ids, else the newest own-state flight / ``year``)."""
    from .naip_index import GroupedReader, plan_windows
    from .privacy import naip_catalog

    cat = naip_catalog(config.NAIP_GEOPARQUET_DIR)
    it = cat.select(lon, lat, st, half_m=px * GSD / 2, year=year)
    if items is not None:
        pos = {str(k): i for i, k in enumerate(cat.ids)}
        for j, s in enumerate(items):
            k = pos.get(str(s).split("~")[0]) if s else None
            if k is not None:
                it[j] = k
    wp = plan_windows(cat, it, lon, lat, px, GSD)
    out = np.zeros((len(lon), px, px, 3), np.uint8)
    ok = np.zeros(len(lon), bool)

    def handle(key: Any, img: np.ndarray | None, err: str) -> int:
        i = int(key)
        if img is not None and float((img.max(axis=2) > 0).mean()) >= 0.9:
            out[i] = img
            ok[i] = True
        return i

    list(GroupedReader(cat, workers=workers).run(wp, np.arange(len(lon)), handle))
    return out, ok, it


def cmd_review(args: argparse.Namespace) -> None:
    """Blind sheets (96 m crops at 2x, only a number) of a random sample of rows matching --where (a pandas-free
    expression over the v4 candidates' columns, e.g. "export_v4 & ~export")."""
    from PIL import Image, ImageDraw

    t = pq.read_table(args.candidates)
    d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in t.column_names}
    env = {k: (v.astype(bool) if v.dtype == bool else v) for k, v in d.items()}
    env["np"] = np
    rng = random.Random(args.seed)
    # one or more strata "name=expression" (or a single --where); all strata are shuffled into one blind sheet set
    strata = [s.split("=", 1) for s in args.strata] if args.strata else [["all", args.where]]
    take, stratum_of, pops = [], {}, {}
    for name, expr in strata:
        m = np.asarray(eval(expr, {"__builtins__": {}}, env), bool)  # noqa: S307 (local CLI expression)
        idx = [i for i in np.flatnonzero(m).tolist() if i not in stratum_of]
        pops[name] = {"population": int(m.sum()), "expr": expr}
        pick = rng.sample(idx, min(args.n, len(idx)))
        pops[name]["sampled"] = len(pick)
        for i in pick:
            stratum_of[i] = name
        take += pick
    take = sorted(take)
    idx = np.asarray(take)
    lon, lat = d["lon"][take].astype(float), d["lat"][take].astype(float)
    imgs, ok, _ = fetch_rgb(lon, lat, d["state"][take].astype(str), items=d["naip_item"][take])
    out = args.out_dir / args.name
    (out / "crops").mkdir(parents=True, exist_ok=True)
    order = list(range(len(take)))
    rng.shuffle(order)
    items, thumbs = [], []
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
        w, hh = float(d["box_w_m"][i]) / GSD * s / 2, float(d["box_h_m"][i]) / GSD * s / 2
        mid = args.thumb_px / 2
        dr = ImageDraw.Draw(im)
        dr.rectangle([mid - w - 4, mid - hh - 4, mid + w + 4, mid + hh + 4], outline=(255, 255, 0), width=2)
        dr.rectangle([0, 0, 34, 14], fill=(0, 0, 0))
        dr.text((2, 1), f"{k:03d}", fill=(255, 255, 0))
        im.save(out / "crops" / f"{k:03d}.jpg", quality=90)
        thumbs.append(im)
        rec = {"n": k, "stratum": stratum_of[i], "id": str(d["id"][i]), "state": str(d["state"][i])}
        for kk in ("conf", "conf_v2", "verify_scan", "p_home", "site", "lon", "lat", "export", "export_v4",
                   "years_checked", "years_seen", "other_checked", "other_seen", "persist"):
            if kk in d:
                v = d[kk][i]
                rec[kk] = (v.item() if hasattr(v, "item") else v) if not (isinstance(v, float) and v != v) else None
        items.append(rec)
    cols, rws = 5, 4
    for s0 in range(0, len(thumbs), cols * rws):
        sheet = Image.new("RGB", (cols * args.thumb_px, rws * args.thumb_px), "white")
        for jj, th in enumerate(thumbs[s0:s0 + cols * rws]):
            sheet.paste(th, ((jj % cols) * args.thumb_px, (jj // cols) * args.thumb_px))
        sheet.save(out / f"sheet_{s0 // (cols * rws):02d}.jpg", quality=88)
    write_json(out / "sample.json", {"strata": pops, "items": items,
                                     "note": "blind review (only a number on each crop, strata shuffled together); "
                                             "model-assistant labels, NOT gold"})
    log.info("review %s: %d crops of %d -> %s", args.name, len(items), len(idx), out)


# =========================================================================== CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("input")
    p.add_argument("--candidates", type=Path, default=NAT / "v3" / "candidates_us.parquet")
    p.add_argument("--detections", type=Path, default=NAT / "detections.parquet")
    p.add_argument("--min-conf-v2", type=float, default=0.3)
    p.add_argument("--min-verify", type=float, default=0.9)
    p.add_argument("--known-n", type=int, default=15000)
    p.add_argument("--known-min-conf", type=float, default=0.3)
    p.add_argument("--seed", type=int, default=4)
    p.add_argument("--out", type=Path, default=V4_DATA / "multiyear_input.parquet")
    add_common_args(p)
    p = sub.add_parser("run")
    p.add_argument("--input", type=Path, default=V4_DATA / "multiyear_input.parquet")
    p.add_argument("--naip-dir", type=Path, default=config.NAIP_GEOPARQUET_DIR)
    p.add_argument("--min-year", type=int, default=2016)
    p.add_argument("--max-other", type=int, default=4, help="other NAIP years per row (newest first)")
    p.add_argument("--weights", type=Path, default=config.WEIGHTS_DIR / "best_v2.pt")
    p.add_argument("--imgsz", type=int, default=416)
    p.add_argument("--verifier", type=Path, default=config.WEIGHTS_DIR / "verifier_v2.pt")
    p.add_argument("--chunk", type=int, default=4096, help="rows per chunk (all their years are fetched together)")
    p.add_argument("--batch", type=int, default=512)
    p.add_argument("--workers", type=int, default=128)
    p.add_argument("--shard", help="K/N: only chunks with index %% N == K")
    p.add_argument("--limit", type=int, default=0, help="only the first N rows (smoke test)")
    p.add_argument("--out", type=Path, default=V4_OUT / "multiyear")
    add_common_args(p)
    p = sub.add_parser("merge")
    p.add_argument("--out", type=Path, default=V4_OUT / "multiyear")
    add_common_args(p)
    p = sub.add_parser("features")
    p.add_argument("--years", type=Path, default=V4_OUT / "multiyear" / "years.parquet")
    p.add_argument("--candidates", type=Path, default=NAT / "v3" / "candidates_us.parquet")
    p.add_argument("--out", type=Path, default=NAT4 / "candidates_v4.parquet")
    add_common_args(p)
    p = sub.add_parser("fit-score")
    p.add_argument("--candidates", type=Path, default=NAT4 / "candidates_v4.parquet")
    p.add_argument("--review-dir", type=Path, nargs="*", default=[NAT4 / "review" / "persist_strata"],
                   help="extra blind review folders (labels.json + sample.json) to train on")
    p.add_argument("--l2", type=float, default=1.0)
    add_common_args(p)
    p = sub.add_parser("labelled")
    p.add_argument("--years", type=Path, default=V4_OUT / "multiyear" / "years.parquet")
    p.add_argument("--input", type=Path, default=V4_DATA / "multiyear_input.parquet")
    p.add_argument("--out", type=Path, default=V4_OUT / "multiyear" / "groups.json")
    add_common_args(p)
    for name in ("grid", "select"):
        p = sub.add_parser(name)
        p.add_argument("--candidates", type=Path, default=NAT4 / "candidates_v4.parquet")
        p.add_argument("--thresholds", type=Path, default=NAT / "v3" / "verify_thresholds.json")
        if name == "grid":
            p.add_argument("--rules", nargs="+", required=True)
            p.add_argument("--heldout", nargs="+", default=["dc_planimetrics", "ramsey_county_planimetrics", "boston_bprd",
                                                            "columbus_crpd", "nashville_metro_parks"])
            p.add_argument("--area", type=Path, default=config.DATA_DIR / "v3" / "heldout_scanned_area.geojson")
            p.add_argument("--out", type=Path, default=NAT4 / "grid.json")
        else:
            p.add_argument("--rule", required=True)
            p.add_argument("--exclude", type=Path, help="candidate ids rejected by review (first token per line)")
        add_common_args(p)
    p = sub.add_parser("app-ready")
    p.add_argument("--candidates", type=Path, default=NAT4 / "candidates_v4.parquet")
    p.add_argument("--v3-app-ready", type=Path, default=NAT / "v3" / "app_ready")
    p.add_argument("--states-file", type=Path, default=config.STATES_FILE)
    p.add_argument("--updated", default=time.strftime("%Y-%m-%d"))
    p.add_argument("--out", type=Path, default=NAT4)
    add_common_args(p)
    p = sub.add_parser("review")
    p.add_argument("--candidates", type=Path, default=NAT4 / "candidates_v4.parquet")
    p.add_argument("--where", default="export")
    p.add_argument("--strata", nargs="*", help="NAME=EXPR ...; --n rows from each, shuffled into one blind set")
    p.add_argument("--name", required=True)
    p.add_argument("--n", type=int, default=40, help="rows per stratum")
    p.add_argument("--seed", type=int, default=21)
    p.add_argument("--crop-px", type=int, default=160)
    p.add_argument("--thumb-px", type=int, default=320)
    p.add_argument("--out-dir", type=Path, default=NAT4 / "review")
    add_common_args(p)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    {"input": cmd_input, "run": cmd_run, "merge": cmd_merge, "features": cmd_features, "labelled": cmd_labelled,
     "fit-score": cmd_fit_score,
     "grid": cmd_grid, "select": cmd_select, "app-ready": cmd_app_ready, "review": cmd_review}[args.cmd](args)


if __name__ == "__main__":
    main()
