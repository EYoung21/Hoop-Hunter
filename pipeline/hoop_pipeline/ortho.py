"""Leaf-off and sharper public orthoimagery: a polite generic crop fetcher, re-scoring and review. See docs/imagery.md.

NAIP is 0.6 m and flown in the growing season, so unmarked blacktops are ambiguous and courts under canopy are hidden.
Many states publish leaf-off 3-12 inch orthoimagery under open terms. ``ortho_sources.json`` lists the sources that
were checked (license quote and URL, coverage, resolution, season); this module fetches crops from them.

Commands
--------
    test      one crop per source at a fixed test point in its state -> out/v4/ortho/test/<key>.jpg (endpoint check)
    select    rows to look at in each covered area -> data/v4/ortho_input.parquet: the scan's truth misses (courts no
              detection matched: canopy, shadow, stale records), every reviewed candidate (C / N / U labels), a sample of
              exported candidates, a sample of public "slab" candidates (v2 >= 0.5, verifier >= 0.8, not exported), and
              a sample of detections on known OSM courts (how the detector behaves on this imagery)
    fetch     a 192 m crop at 0.3 m (640 px) per row from the row's source (EPSG:3857 bbox, ground-size corrected),
              rate-limited per host (max_concurrent, min_interval_s, retries with back-off, the pipeline User-Agent)
              -> data/v4/ortho_chips/<key>/<id>.jpg + data/v4/ortho_fetch.parquet; blank / no-data crops are flagged
    score     v2 detector + verifier on each crop resampled to 0.6 m (the detector's training GSD), next to the same
              rows' NAIP scores from the multi-year run -> out/v4/ortho/scores.parquet + summary.json
    review    blind sheets of sharp crops for any subset (e.g. the candidates earlier reviews called unsure)

Every request carries config.USER_AGENT. Only sources with verdict USE in the survey are in the registry.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests

from . import config
from .common import add_common_args, read_json, setup_logging, write_json

log = logging.getLogger("ortho")
REGISTRY = config.PIPELINE_ROOT / "ortho_sources.json"
V4_DATA = config.DATA_DIR / "v4"
OUT = config.OUT_DIR / "v4" / "ortho"
R_MERC = 6378137.0
CROP_M = 192.0          # same ground size as the detector's 320 px NAIP chips
FETCH_GSD = 0.3         # fetched crops: 640 px for 192 m (2x NAIP); the detector sees them at 0.6 m
DET_PX = config.CHIP_SIZE_PX


def load_registry(path: Path = REGISTRY) -> list[dict[str, Any]]:
    return read_json(path)["sources"]


def merc(lon: float, lat: float) -> tuple[float, float]:
    x = R_MERC * math.radians(lon)
    y = R_MERC * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def source_for(sources: list[dict[str, Any]], state: str, lon: float, lat: float) -> dict[str, Any] | None:
    """The covering source for a point: same state, inside the source's bbox when it has one; regional sources
    with ``priority`` (NYC tiles) win over statewide ones."""
    best = None
    for s in sources:
        if state not in s["states"]:
            continue
        bb = s.get("bbox")
        if bb and not (bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]):
            continue
        if best is None or s.get("priority", 0) > best.get("priority", 0):
            best = s
    return best


# =========================================================================== polite HTTP
class Host:
    """Per-source politeness: at most ``max_concurrent`` requests in flight, at least ``min_interval_s`` between
    request starts, retries with exponential back-off on errors / 429 / 5xx (honouring Retry-After)."""

    def __init__(self, src: dict[str, Any]) -> None:
        self.src = src
        self.sem = threading.Semaphore(int(src.get("max_concurrent", 2)))
        self.interval = float(src.get("min_interval_s", 0.5))
        self.lock = threading.Lock()
        self.next_t = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self.requests = 0
        self.errors = 0
        self.max_requests = int(src.get("max_requests", 10 ** 9))

    def get(self, url: str, params: dict[str, Any] | None = None, retries: int = 4) -> bytes | None:
        for attempt in range(retries):
            with self.sem:
                with self.lock:
                    if self.requests >= self.max_requests:
                        return None
                    wait = self.next_t - time.monotonic()
                    self.next_t = max(time.monotonic(), self.next_t) + self.interval
                    self.requests += 1
                if wait > 0:
                    time.sleep(wait)
                try:
                    r = self.session.get(url, params=params, timeout=90)
                except requests.RequestException as exc:
                    self.errors += 1
                    log.debug("%s: %s", self.src["key"], exc)
                    time.sleep(2.0 * 2 ** attempt)
                    continue
            if r.status_code == 200 and r.headers.get("content-type", "").split(";")[0] not in ("text/html",
                                                                                                 "application/json"):
                return r.content
            self.errors += 1
            if r.status_code == 404:  # e.g. a tile outside the cache
                return None
            ra = r.headers.get("Retry-After")
            time.sleep(float(ra) if ra and ra.isdigit() else 2.0 * 2 ** attempt)
        return None


def decode(content: bytes) -> np.ndarray | None:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(content)) as im:
            return np.asarray(im.convert("RGB"))
    except OSError:
        return None


def blank(img: np.ndarray) -> bool:
    """No-data crops: (nearly) uniform, or mostly pure white / black fill."""
    g = img.mean(axis=2)
    return bool(g.std() < 4 or (g > 250).mean() > 0.4 or (g < 3).mean() > 0.4)


def fetch_crop(host: Host, lon: float, lat: float, half_m: float = CROP_M / 2, gsd: float = FETCH_GSD
               ) -> np.ndarray | None:
    """RGB crop of 2 * half_m ground metres around (lon, lat) at ``gsd`` m/px (north-up, Web Mercator)."""
    s = host.src
    x, y = merc(lon, lat)
    hm = half_m / math.cos(math.radians(lat))       # mercator units for half_m on the ground
    px = int(round(2 * half_m / gsd))
    bbox = f"{x - hm:.2f},{y - hm:.2f},{x + hm:.2f},{y + hm:.2f}"
    t = s["type"]
    p = dict(s.get("params", {}))
    if t in ("arcgis_imageserver", "arcgis_mapserver"):
        q = {"bbox": bbox, "bboxSR": 3857, "imageSR": 3857, "size": f"{px},{px}", "format": "jpg", "f": "image", **p}
        if t == "arcgis_mapserver":
            q["transparent"] = "false"
        c = host.get(s["endpoint"] + ("/exportImage" if t == "arcgis_imageserver" else "/export"), q)
    elif t in ("wms13", "wms111"):
        q = {"SERVICE": "WMS", "REQUEST": "GetMap", "STYLES": "", "BBOX": bbox, "WIDTH": px, "HEIGHT": px,
             "FORMAT": "image/jpeg", **p}
        q.update({"VERSION": "1.3.0", "CRS": "EPSG:3857"} if t == "wms13" else {"VERSION": "1.1.1", "SRS": "EPSG:3857"})
        c = host.get(s["endpoint"], q)
    elif t == "xyz_tiles":
        return fetch_tiles(host, x - hm, y - hm, x + hm, y + hm, px, int(p.get("zoom", 19)))
    else:
        raise ValueError(f"unknown source type {t}")
    if c is None:
        return None
    img = decode(c)
    if img is not None and img.shape[:2] != (px, px):
        from PIL import Image

        img = np.asarray(Image.fromarray(img).resize((px, px), Image.Resampling.BICUBIC))
    return img


def fetch_tiles(host: Host, x0: float, y0: float, x1: float, y1: float, px: int, z: int) -> np.ndarray | None:
    from PIL import Image

    world = 2 * math.pi * R_MERC
    res = world / (256 * 2 ** z)
    ox = -world / 2
    oy = world / 2
    tx0, tx1 = int((x0 - ox) // (256 * res)), int((x1 - ox) // (256 * res))
    ty0, ty1 = int((oy - y1) // (256 * res)), int((oy - y0) // (256 * res))
    mosaic = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
    got = 0
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            c = host.get(host.src["endpoint"].format(z=z, y=ty, x=tx))
            im = decode(c) if c else None
            if im is not None:
                mosaic.paste(Image.fromarray(im), ((tx - tx0) * 256, (ty - ty0) * 256))
                got += 1
    if not got:
        return None
    left = (x0 - (ox + tx0 * 256 * res)) / res
    top = ((oy - ty0 * 256 * res) - y1) / res
    crop = mosaic.crop((round(left), round(top), round(left + (x1 - x0) / res), round(top + (y1 - y0) / res)))
    return np.asarray(crop.resize((px, px), Image.Resampling.BICUBIC))


# =========================================================================== test
TEST_POINTS = {  # a public park / school court in each covered area (lon, lat)
    "NC": (-78.6596, 35.7898), "VA": (-77.4605, 37.5539), "OH": (-82.9988, 39.9612), "IN": (-86.1581, 39.7684),
    "KY": (-84.5037, 38.0406), "MO": (-90.2650, 38.6270), "AR": (-92.2896, 34.7465), "MN": (-93.2650, 44.9778),
    "TX": (-94.7400, 32.5007), "CO": (-105.0814, 39.7047), "OR": (-122.6765, 45.5231), "NY": (-73.9969, 40.7306),
    "PA": (-75.1652, 39.9526), "NJ": (-74.1724, 40.7357), "MA": (-71.0589, 42.3601), "CT": (-72.6851, 41.7637),
    "MD": (-76.5662, 39.2904), "DE": (-75.5466, 39.7391), "VT": (-73.2121, 44.4759), "RI": (-71.4128, 41.8240),
    "DC": (-77.0090, 38.8899),
}


def cmd_test(args: argparse.Namespace) -> None:
    from PIL import Image

    out = args.out / "test"
    out.mkdir(parents=True, exist_ok=True)
    res = {}
    for s in load_registry():
        lon, lat = TEST_POINTS[s["states"][0]]
        if s.get("bbox") and s["key"] == "nyc_oti_2024":
            lon, lat = -73.9969, 40.7306
        t0 = time.monotonic()
        img = fetch_crop(Host(s), lon, lat, half_m=48.0, gsd=0.15)
        dt = time.monotonic() - t0
        ok = img is not None and not blank(img)
        if img is not None:
            Image.fromarray(img).save(out / f"{s['key']}.jpg", quality=90)
        res[s["key"]] = {"ok": ok, "seconds": round(dt, 2), "shape": None if img is None else list(img.shape)}
        log.info("%s: %s in %.1f s", s["key"], "ok" if ok else "FAILED/blank", dt)
    write_json(out / "test.json", res)


# =========================================================================== select
def cmd_select(args: argparse.Namespace) -> None:
    from .multiyear import load_review_labels

    rng = random.Random(args.seed)
    sources = load_registry()
    inp = pq.read_table(args.input).to_pydict()
    cand_cols = ["id", "privacy", "conf_v2", "verify_scan", "export"] + (["export_v4"] if "export_v4" in pq.read_schema(
        args.candidates).names else [])
    cd = pq.read_table(args.candidates, columns=cand_cols).to_pydict()
    cpos = {k: i for i, k in enumerate(cd["id"])}
    labels = load_review_labels()
    ex_col = "export_v4" if "export_v4" in cd else "export"
    groups: dict[tuple[str, str], list[int]] = {}
    for i, (rid, kind, st, lon, lat) in enumerate(zip(inp["id"], inp["kind"], inp["state"], inp["lon"], inp["lat"])):
        s = source_for(sources, st, lon, lat)
        if s is None:
            continue
        if kind == "truth_miss":
            g = "truth_miss"
        elif kind == "known":
            g = "known"
        else:
            j = cpos.get(rid)
            if j is None:
                continue
            if rid in labels:
                g = "reviewed"
            elif cd[ex_col][j]:
                g = "export"
            elif (cd["privacy"][j] == "public" and (cd["conf_v2"][j] or 0) >= 0.5
                  and (cd["verify_scan"][j] or 0) >= 0.8):
                g = "slab_pool"
            else:
                continue
        groups.setdefault((s["key"], g), []).append(i)
    caps = {"truth_miss": args.cap_truth, "reviewed": 10 ** 9, "export": args.cap_export, "slab_pool": args.cap_slab,
            "known": args.cap_known}
    take: list[tuple[int, str, str]] = []
    for (key, g), idx in sorted(groups.items()):
        rng.shuffle(idx)
        take += [(i, key, g) for i in idx[:caps[g]]]
    rows = {"id": [], "kind": [], "group": [], "source": [], "state": [], "lon": [], "lat": [], "box_w_m": [],
            "box_h_m": [], "label": []}
    for i, key, g in take:
        for k in ("id", "kind", "state", "lon", "lat", "box_w_m", "box_h_m"):
            rows[k].append(inp[k][i])
        rows["group"].append(g)
        rows["source"].append(key)
        rows["label"].append(labels.get(inp["id"][i], ("", ""))[0])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(rows), args.out)
    summ = Counter(f"{k}|{g}" for _, k, g in take)
    write_json(args.out.with_suffix(".json"), {"rows": len(take), "by_source_group": dict(sorted(summ.items())),
                                               "export_col": ex_col})
    log.info("ortho select: %d rows (%s) -> %s", len(take), dict(Counter(g for *_, g in take)), args.out)


# =========================================================================== fetch
def cmd_fetch(args: argparse.Namespace) -> None:
    from PIL import Image

    t0 = time.monotonic()
    sources = {s["key"]: s for s in load_registry()}
    d = pq.read_table(args.input).to_pydict()
    n = len(d["id"])
    hosts = {k: Host(s) for k, s in sources.items()}
    out = {"id": [None] * n, "source": [None] * n, "ok": [False] * n, "blank": [False] * n, "seconds": [0.0] * n,
           "path": [""] * n}
    chips = args.chips_dir

    def one(i: int) -> None:
        key = d["source"][i]
        path = chips / key / f"{d['id'][i].replace(':', '_')}.jpg"
        out["id"][i], out["source"][i] = d["id"][i], key
        if path.exists():
            out["ok"][i], out["path"][i] = True, str(path)
            return
        if key in args.skip_uncached:  # a slow / stalled server: keep only the crops it already returned
            return
        t = time.monotonic()
        img = fetch_crop(hosts[key], float(d["lon"][i]), float(d["lat"][i]))
        out["seconds"][i] = round(time.monotonic() - t, 2)
        if img is None:
            return
        if blank(img):
            out["blank"][i] = True
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(img).save(path, quality=90)
        out["ok"][i], out["path"][i] = True, str(path)

    # one worker pool per source, all sources in parallel; each Host enforces its own politeness limits
    by_src: dict[str, list[int]] = {}
    for i, k in enumerate(d["source"]):
        by_src.setdefault(k, []).append(i)
    pools = [ThreadPoolExecutor(max_workers=int(sources[k].get("max_concurrent", 2))) for k in by_src]
    futs = []
    for pool, (_k, idx) in zip(pools, by_src.items()):
        futs += [pool.submit(one, i) for i in idx]
    done = 0
    for f in futs:
        f.result()
        done += 1
        if done % 200 == 0:
            log.info("%d/%d crops (%.0f s)", done, n, time.monotonic() - t0)
    for p in pools:
        p.shutdown()
    pq.write_table(pa.table(out), args.out)
    stats = {k: {"rows": len(idx), "ok": int(sum(out["ok"][i] for i in idx)), "blank": int(sum(out["blank"][i] for i in idx)),
                 "requests": hosts[k].requests, "errors": hosts[k].errors,
                 "median_s": float(np.median([out["seconds"][i] for i in idx]))} for k, idx in by_src.items()}
    write_json(args.out.with_suffix(".json"), stats)
    log.info("fetched %d crops in %.1f min: %s", sum(out["ok"]), (time.monotonic() - t0) / 60, json.dumps(stats))


# =========================================================================== score
def cmd_score(args: argparse.Namespace) -> None:
    import torch
    from PIL import Image

    from .cropcls import CropClassifier
    from .fastdet import FastDetector
    from .multiyear import best_overlap, box_px, near_best

    d = pq.read_table(args.input).to_pydict()
    f = pq.read_table(args.fetch).to_pydict()
    ok = np.asarray(f["ok"], bool)
    det = FastDetector(args.weights, imgsz=args.imgsz, device="cpu", half=False)
    ver = CropClassifier.load(args.verifier, device=torch.device("cpu"))
    n = len(d["id"])
    res = {k: np.full(n, np.nan, np.float32) for k in ("o_conf_box", "o_conf_near", "o_verify_box", "o_verify_det")}
    idx = np.flatnonzero(ok)
    c = DET_PX / 2
    t0 = time.monotonic()
    for b0 in range(0, len(idx), args.batch):
        bi = idx[b0:b0 + args.batch]
        chips = np.stack([np.asarray(Image.open(f["path"][i]).convert("RGB").resize((DET_PX, DET_PX), Image.Resampling.BOX))
                          for i in bi])
        x = torch.from_numpy(chips)
        x_pre = det.preprocess(x)
        out = det.detect(x, conf=0.05, x_pre=x_pre).cpu().numpy()
        cand, dets_v, rows_v = [], [], []
        for q, i in enumerate(bi.tolist()):
            dq = out[out[:, 0] == q, 1:6]
            box = box_px(float(d["box_w_m"][i]), float(d["box_h_m"][i]))
            cb, kb = best_overlap(dq, box)
            r_m = 25.0 if d["kind"][i] == "truth_miss" else max(10.0, 0.5 * math.hypot(d["box_w_m"][i], d["box_h_m"][i]))
            cn, kn = near_best(dq, c, c, r_m / config.TARGET_GSD_M)
            res["o_conf_box"][i], res["o_conf_near"][i] = cb, cn
            k = kb if cb >= cn else kn
            if k >= 0:
                dets_v.append([q, *dq[k, :4], dq[k, 4]])
                rows_v.append(i)
            cand.append([q, *box, 1.0])
        res["o_verify_box"][bi] = ver.score_boxes(x_pre, torch.tensor(cand, dtype=torch.float32), DET_PX).float().numpy()
        if dets_v:
            res["o_verify_det"][np.asarray(rows_v)] = ver.score_boxes(
                x_pre, torch.tensor(dets_v, dtype=torch.float32), DET_PX).float().numpy()
        if (b0 // args.batch) % 20 == 0:
            log.info("scored %d/%d (%.0f s)", b0 + len(bi), len(idx), time.monotonic() - t0)
    # the same rows on NAIP (multi-year run): scan-year chip, best over all years
    y = pq.read_table(args.years, columns=["id", "year", "is_ref", "ok", "conf_box", "conf_near", "verify_det", "date"]
                      ).to_pydict()
    naip: dict[str, dict[str, Any]] = {}
    for rid, _yr, ref, okk, cb, cn, vd, dt in zip(y["id"], y["year"], y["is_ref"], y["ok"], y["conf_box"], y["conf_near"],
                                                 y["verify_det"], y["date"]):
        if not okk:
            continue
        cc = max(cb if cb == cb else 0.0, cn if cn == cn else 0.0)
        e = naip.setdefault(rid, {"ref_conf": np.nan, "ref_verify": np.nan, "ref_date": "", "max_conf": 0.0})
        e["max_conf"] = max(e["max_conf"], cc)
        if ref:
            e["ref_conf"], e["ref_verify"], e["ref_date"] = cc, (vd if vd == vd else 0.0), dt
    cols = {k: pa.array(v) for k, v in d.items()}
    cols.update({"ok": pa.array(ok), "blank": pa.array(f["blank"]), **{k: pa.array(v) for k, v in res.items()}})
    for k in ("ref_conf", "ref_verify", "max_conf"):
        cols[f"naip_{k}"] = pa.array([naip.get(r, {}).get(k, np.nan) for r in d["id"]])
    cols["naip_ref_date"] = pa.array([naip.get(r, {}).get("ref_date", "") for r in d["id"]])
    t = pa.table(cols)
    args.out.mkdir(parents=True, exist_ok=True)
    pq.write_table(t, args.out / "scores.parquet")
    summarise(t.to_pydict(), args.out / "summary.json")


def summarise(d: dict[str, list[Any]], path: Path) -> None:
    """Per source x group: crops ok, detector agreement on the sharp crop vs NAIP."""
    oc = np.maximum(np.nan_to_num(np.asarray(d["o_conf_box"], float)), np.nan_to_num(np.asarray(d["o_conf_near"], float)))
    ov = np.nan_to_num(np.asarray(d["o_verify_det"], float))
    nc = np.nan_to_num(np.asarray(d["naip_ref_conf"], float))
    nv = np.nan_to_num(np.asarray(d["naip_ref_verify"], float))
    ok = np.asarray(d["ok"], bool)
    grp = np.asarray(d["group"], dtype=object)
    lab = np.asarray(d["label"], dtype=object)
    src = np.asarray(d["source"], dtype=object)
    out: dict[str, Any] = {"by_group": {}, "by_source": {}, "by_label": {}}

    def stats(m: np.ndarray) -> dict[str, Any]:
        m = m & ok
        k = int(m.sum())
        if not k:
            return {"n": 0}
        return {"n": k, "ortho_conf_ge_0.25": round(float((oc[m] >= 0.25).mean()), 3),
                "ortho_conf_ge_0.5": round(float((oc[m] >= 0.5).mean()), 3),
                "naip_conf_ge_0.25": round(float((nc[m] >= 0.25).mean()), 3),
                "naip_conf_ge_0.5": round(float((nc[m] >= 0.5).mean()), 3),
                "ortho_conf_ge_0.5_and_verify_ge_0.9": round(float(((oc[m] >= 0.5) & (ov[m] >= 0.9)).mean()), 3),
                "naip_conf_ge_0.5_and_verify_ge_0.9": round(float(((nc[m] >= 0.5) & (nv[m] >= 0.9)).mean()), 3),
                "ortho_conf_mean": round(float(oc[m].mean()), 3), "naip_conf_mean": round(float(nc[m].mean()), 3)}

    for g in sorted(set(grp.tolist())):
        out["by_group"][g] = {"rows": int((grp == g).sum()), **stats(grp == g)}
    for s in sorted(set(src.tolist())):
        out["by_source"][s] = {g: stats((src == s) & (grp == g)) for g in sorted(set(grp[src == s].tolist()))}
        out["by_source"][s]["fetched"] = int((ok & (src == s)).sum())
        out["by_source"][s]["rows"] = int((src == s).sum())
    for lb in ("C", "N", "U"):
        out["by_label"][lb] = stats(lab == lb)
    write_json(path, out)
    log.info("ortho summary: %s", json.dumps(out["by_group"]))


# =========================================================================== review
def cmd_review(args: argparse.Namespace) -> None:
    """Blind sheets: the central 96 m of each sharp crop at 2x (only a number), for a subset (--where over scores)."""
    from PIL import Image, ImageDraw

    t = pq.read_table(args.scores)
    d = {k: np.asarray(t.column(k).to_pylist(), dtype=object) for k in t.column_names}
    f = pq.read_table(args.fetch).to_pydict()
    paths = dict(zip(f["id"], f["path"]))
    env = {k: v for k, v in d.items()}
    oc = np.maximum(np.nan_to_num(d["o_conf_box"].astype(float)), np.nan_to_num(d["o_conf_near"].astype(float)))
    env.update({"np": np, "ortho_conf": oc, "naip_conf": np.nan_to_num(d["naip_ref_conf"].astype(float))})
    m = np.asarray(eval(args.where, {"__builtins__": {}}, env), bool) & d["ok"].astype(bool)  # noqa: S307
    idx = np.flatnonzero(m).tolist()
    rng = random.Random(args.seed)
    rng.shuffle(idx)
    idx = idx[:args.n]
    out = args.out / "review" / args.name
    (out / "crops").mkdir(parents=True, exist_ok=True)
    naip: dict[int, Any] = {}
    if args.pair_naip and idx:  # the newest NAIP flight of the same spot, side by side (left) at the same scale
        from .multiyear import fetch_rgb

        ii = np.asarray(idx)
        imgs, ok, _ = fetch_rgb(d["lon"][ii].astype(float), d["lat"][ii].astype(float), d["state"][ii].astype(str))
        for q, i in enumerate(idx):
            if ok[q]:
                naip[i] = Image.fromarray(imgs[q])
    items, thumbs = [], []
    for k, i in enumerate(idx, 1):
        im = Image.open(paths[d["id"][i]]).convert("RGB")
        W = im.width
        half = args.crop_m / 2 / FETCH_GSD
        c = W / 2
        im = im.crop((int(c - half), int(c - half), int(c + half), int(c + half))).resize((args.thumb_px, args.thumb_px),
                                                                                           Image.Resampling.BICUBIC)
        s = args.thumb_px / (2 * half)
        w, h = float(d["box_w_m"][i]) / FETCH_GSD * s / 2, float(d["box_h_m"][i]) / FETCH_GSD * s / 2
        mid = args.thumb_px / 2
        dr = ImageDraw.Draw(im)
        dr.rectangle([mid - w - 4, mid - h - 4, mid + w + 4, mid + h + 4], outline=(255, 255, 0), width=2)
        dr.rectangle([0, 0, 34, 14], fill=(0, 0, 0))
        dr.text((2, 1), f"{k:03d}", fill=(255, 255, 0))
        if args.pair_naip:
            pair = Image.new("RGB", (2 * args.thumb_px + 4, args.thumb_px), "white")
            if i in naip:
                hn = args.crop_m / 2 / config.TARGET_GSD_M
                cn = naip[i].width / 2
                nim = naip[i].crop((int(cn - hn), int(cn - hn), int(cn + hn), int(cn + hn))).resize(
                    (args.thumb_px, args.thumb_px), Image.Resampling.BICUBIC)
                pair.paste(nim, (0, 0))
            pair.paste(im, (args.thumb_px + 4, 0))
            im = pair
        im.save(out / "crops" / f"{k:03d}.jpg", quality=90)
        thumbs.append(im)
        items.append({"n": k, **{kk: (d[kk][i].item() if hasattr(d[kk][i], "item") else d[kk][i])
                                 for kk in ("id", "group", "source", "state", "label", "lon", "lat")}})
    cols, rws = (2, 3) if args.pair_naip else (4, 3)
    tw = thumbs[0].width if thumbs else args.thumb_px
    for s0 in range(0, len(thumbs), cols * rws):
        sheet = Image.new("RGB", (cols * tw, rws * args.thumb_px), "white")
        for jj, th in enumerate(thumbs[s0:s0 + cols * rws]):
            sheet.paste(th, ((jj % cols) * tw, (jj // cols) * args.thumb_px))
        sheet.save(out / f"sheet_{s0 // (cols * rws):02d}.jpg", quality=88)
    write_json(out / "sample.json", {"where": args.where, "population": int(m.sum()), "items": items,
                                     "note": "blind review of sharp / leaf-off crops; model-assistant labels, NOT gold"})
    log.info("ortho review %s: %d crops -> %s", args.name, len(items), out)


# =========================================================================== CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("test")
    p.add_argument("--out", type=Path, default=OUT)
    add_common_args(p)
    p = sub.add_parser("select")
    p.add_argument("--input", type=Path, default=V4_DATA / "multiyear_input.parquet")
    p.add_argument("--candidates", type=Path, default=config.OUT_DIR / "national" / "v4" / "candidates_v4.parquet")
    p.add_argument("--cap-truth", type=int, default=150)
    p.add_argument("--cap-export", type=int, default=100)
    p.add_argument("--cap-slab", type=int, default=80)
    p.add_argument("--cap-known", type=int, default=40)
    p.add_argument("--seed", type=int, default=5)
    p.add_argument("--out", type=Path, default=V4_DATA / "ortho_input.parquet")
    add_common_args(p)
    p = sub.add_parser("fetch")
    p.add_argument("--input", type=Path, default=V4_DATA / "ortho_input.parquet")
    p.add_argument("--chips-dir", type=Path, default=V4_DATA / "ortho_chips")
    p.add_argument("--skip-uncached", nargs="*", default=[], help="source keys: do not request crops not cached yet")
    p.add_argument("--out", type=Path, default=V4_DATA / "ortho_fetch.parquet")
    add_common_args(p)
    p = sub.add_parser("score")
    p.add_argument("--input", type=Path, default=V4_DATA / "ortho_input.parquet")
    p.add_argument("--fetch", type=Path, default=V4_DATA / "ortho_fetch.parquet")
    p.add_argument("--years", type=Path, default=config.OUT_DIR / "v4" / "multiyear" / "years.parquet")
    p.add_argument("--weights", type=Path, default=config.WEIGHTS_DIR / "best_v2.pt")
    p.add_argument("--imgsz", type=int, default=416)
    p.add_argument("--verifier", type=Path, default=config.WEIGHTS_DIR / "verifier_v2.pt")
    p.add_argument("--batch", type=int, default=16)
    p.add_argument("--out", type=Path, default=OUT)
    add_common_args(p)
    p = sub.add_parser("review")
    p.add_argument("--scores", type=Path, default=OUT / "scores.parquet")
    p.add_argument("--fetch", type=Path, default=V4_DATA / "ortho_fetch.parquet")
    p.add_argument("--where", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--n", type=int, default=48)
    p.add_argument("--seed", type=int, default=3)
    p.add_argument("--crop-m", type=float, default=96.0)
    p.add_argument("--pair-naip", action="store_true", help="show the newest NAIP crop (left) next to the sharp one")
    p.add_argument("--thumb-px", type=int, default=320)
    p.add_argument("--out", type=Path, default=OUT)
    add_common_args(p)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    {"test": cmd_test, "select": cmd_select, "fetch": cmd_fetch, "score": cmd_score, "review": cmd_review}[args.cmd](args)


if __name__ == "__main__":
    main()
