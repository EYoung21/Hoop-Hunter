"""Post-processing of the national (CONUS, every NAIP pixel) scan. See README "National run (2026-10-15)".

Runs on a cloud node (the local PC is short on RAM). Inputs: ``scan_engine merge --packed`` output
(``out/national/detections.parquet``, ``merge_stats.json``, ``court_best_conf.json``), the site layers
(``data/sites/sites_us.parquet``, ``residential_us.parquet``, ``buildings_<ST>.parquet``), OSM courts
(``data/osm_us/courts_us.parquet``) and, optionally, ``hoop_pipeline.rescore`` output.

Commands
--------
    split-sites   per-state slices of sites_us / residential_us (state bbox + 0.05 deg) -> data/sites/by_state/
    osm-geojson   courts_<ST>.geojson (export_app's OSM input) for every state from courts_us.parquet
    osm-phome     context-CNN p_home for every OSM basketball feature (160 m NAIP crops) -> data/national/osm_phome.parquet
    privacy       per state, in parallel: privacy class + site for every candidate (rules, plus p_home from rescore
                  where available) and for every OSM feature (rules + p_home) -> out/national/privacy/<ST>.parquet,
                  out/national/osm_privacy/<ST>.json
    summarize     out/national/candidates_us.parquet + state_summary.{json,csv} (tiles, candidates by band and class,
                  known-court recall per state)
    review        blind stratified review sheets of public candidates (rescore chips) -> out/national/review/
    score         precision per stratum and at operating points from the reviewer's labels
    app-ready     out/national/app_ready/<ST>.geojson in the app schema (+ site): OSM courts that pass the privacy
                  policy plus public candidates at the chosen operating point
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import random
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import config
from .common import add_common_args, setup_logging, write_json

log = logging.getLogger("national")
OUT = config.OUT_DIR / "national"
NAT_DATA = config.DATA_DIR / "national"
SITES_DIR = config.DATA_DIR / "sites"
BY_STATE = SITES_DIR / "by_state"
BANDS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)


def state_slug(name: str) -> str:
    return name.lower().replace(" ", "_")


def state_names(states_file: Path = config.STATES_FILE) -> dict[str, str]:
    from .states import States

    s = States.load(states_file)
    return {str(c): str(n) for c, n in zip(s.codes, s.names)}


# =========================================================================== split-sites
def cmd_split_sites(args: argparse.Namespace) -> None:
    import shapely

    from .geoparquet import read_geoparquet, write_geoparquet
    from .states import States

    t0 = time.monotonic()
    states = States.load(args.states_file)
    args.out.mkdir(parents=True, exist_ok=True)
    for name in ("sites", "residential"):
        a, g = read_geoparquet(args.sites_dir / f"{name}_us.parquet")
        b = shapely.bounds(g)
        log.info("%s_us: %d features (%.0f s)", name, len(g), time.monotonic() - t0)
        for code, geom in zip(states.codes, states.geoms):
            x0, y0, x1, y1 = (float(v) for v in shapely.bounds(geom))
            p = args.pad_deg
            keep = (b[:, 2] >= x0 - p) & (b[:, 0] <= x1 + p) & (b[:, 3] >= y0 - p) & (b[:, 1] <= y1 + p)
            write_geoparquet(args.out / f"{name}_{code}.parquet", {k: v[keep] for k, v in a.items()}, g[keep])
        del a, g, b
    log.info("split-sites done in %.0f s -> %s", time.monotonic() - t0, args.out)


# =========================================================================== osm-geojson
def cmd_osm_geojson(args: argparse.Namespace) -> None:
    import shapely

    from .osm_extract import write_state_geojson

    t = pq.read_table(args.courts)
    geoms = shapely.from_wkb(t.column("geometry").to_numpy(zero_copy_only=False))
    names = state_names(args.states_file)
    counts = {}
    for code in sorted(set(t.column("state").to_pylist()) - {None, ""}):
        counts[code] = write_state_geojson(t, geoms, code, args.out / f"courts_{code}.geojson",
                                           state_slug(names.get(code, code)))
    write_json(args.out / "courts_by_state.json", counts)
    log.info("OSM state GeoJSON: %d states, %d features", len(counts), sum(counts.values()))


# =========================================================================== osm-phome
def cmd_osm_phome(args: argparse.Namespace) -> None:
    from .cropcls import CropClassifier
    from .privacy import fetch_context_crops

    t0 = time.monotonic()
    t = pq.read_table(args.courts, columns=["osm_ref", "state", "center_lon", "center_lat"]).to_pydict()
    refs = np.asarray(t["osm_ref"], dtype=object)
    lon = np.asarray(t["center_lon"], float)
    lat = np.asarray(t["center_lat"], float)
    st = np.asarray(t["state"], dtype=object)
    clf = CropClassifier.load(args.context_model)
    px = int(round(float(clf.meta.get("point_m", 160.0)) / config.TARGET_GSD_M))
    p_home = np.full(len(refs), np.nan)
    for k in range(0, len(refs), args.chunk):
        sl = slice(k, k + args.chunk)
        imgs, ok = fetch_context_crops(lon[sl], lat[sl], st[sl], px, args.naip_dir, workers=args.workers)
        idx = np.flatnonzero(ok) + k
        p_home[idx] = clf.score_images(imgs[ok])
        log.info("osm p_home: %d/%d (%.0f s)", min(k + args.chunk, len(refs)), len(refs), time.monotonic() - t0)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"osm_ref": refs.tolist(), "p_home": p_home}), args.out, compression="zstd")
    log.info("osm p_home for %d features (%d scored) -> %s", len(refs), int(np.isfinite(p_home).sum()), args.out)


# =========================================================================== privacy (per state, parallel)
FEATURE_KEYS = ("house_35m", "house_60m", "mid_100m", "nearest_bldg_m", "nearest_bldg_area", "nearest_bldg_class",
                "in_residential", "in_apartments", "padus_inset_m")


def classify_state(st: str, detections: str, rescore: str | None, osm_phome: str | None, courts: str,
                   sites_dir: str, buildings_dir: str, out_dir: str, padus_inset_m: float) -> dict[str, Any]:
    """Privacy class of every candidate (not near a known OSM court) and every OSM feature of one state."""
    from pyproj import Transformer

    from .privacy import ALBERS, Context, classify_point, point_features

    t0 = time.monotonic()
    setup_logging(0)
    det = pq.read_table(detections, filters=[("state", "=", st)])
    det = det.filter(pc.equal(det.column("near_known_osm"), ""))
    d = det.to_pydict()
    n = len(d["id"])
    p_home = np.full(n, np.nan)
    conf2 = np.full(n, np.nan)
    verify2 = np.full(n, np.nan)
    if rescore and Path(rescore).exists() and n:
        r = pq.read_table(rescore, columns=["id", "chip_ok", "conf2", "verify", "p_home"])
        r = r.filter(pc.is_in(r.column("id"), value_set=pa.array(d["id"])))
        pos = {cid: i for i, cid in enumerate(d["id"])}
        rd = r.to_pydict()
        for cid, ok_, c2, v2, ph in zip(rd["id"], rd["chip_ok"], rd["conf2"], rd["verify"], rd["p_home"]):
            if ok_ and cid in pos:
                i = pos[cid]
                p_home[i], conf2[i], verify2[i] = ph, c2, v2
    bpath = Path(buildings_dir) / f"buildings_{st}.parquet"
    ctx = Context.load(st, [bpath] if bpath.exists() else [], Path(sites_dir))
    tr = Transformer.from_crs("EPSG:4326", ALBERS, always_xy=True)
    x, y = tr.transform(np.asarray(d["lon"], float), np.asarray(d["lat"], float))
    cls_, site_, why_ = [], [], []
    feats: dict[str, list[Any]] = {k: [] for k in FEATURE_KEYS}
    for i in range(n):
        f = point_features(ctx, float(x[i]), float(y[i]))
        c, s, w = classify_point(f, float(p_home[i]), padus_inset_m)
        cls_.append(c)
        site_.append(s)
        why_.append(w)
        for k in FEATURE_KEYS:
            v = f[k]
            feats[k].append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
    cols = {k: det.column(k) for k in det.column_names}
    cols.update({"privacy": pa.array(cls_, pa.string()), "site": pa.array(site_, pa.string()),
                 "privacy_reason": pa.array(why_, pa.string()), "p_home": pa.array(p_home), "conf2": pa.array(conf2),
                 "verify2": pa.array(verify2)})
    for k, v in feats.items():
        cols[f"f_{k}"] = pa.array(v)
    out = Path(out_dir)
    (out / "privacy").mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(cols), out / "privacy" / f"{st}.parquet", compression="zstd")
    # OSM features of the state (export_app --osm-privacy input)
    ct = pq.read_table(courts, columns=["osm_ref", "state", "center_lon", "center_lat", "tags"], filters=[("state", "=", st)])
    cd = ct.to_pydict()
    ph_osm: dict[str, float] = {}
    if osm_phome and Path(osm_phome).exists():
        o = pq.read_table(osm_phome).to_pydict()
        ph_osm = {r_: p_ for r_, p_ in zip(o["osm_ref"], o["p_home"]) if p_ is not None}
    ox, oy = tr.transform(np.asarray(cd["center_lon"], float), np.asarray(cd["center_lat"], float))
    osm_cls: dict[str, str] = {}
    detail: dict[str, Any] = {}
    for i, ref in enumerate(cd["osm_ref"]):
        ph = float(ph_osm.get(ref, float("nan")))
        c, s, w = classify_point(point_features(ctx, float(ox[i]), float(oy[i])), ph, padus_inset_m)
        osm_cls[ref] = c
        acc = json.loads(cd["tags"][i] or "{}").get("access")
        detail[ref] = {"privacy": c, "site": s, "reason": w, "access": acc, "p_home": None if ph != ph else round(ph, 3)}
    (out / "osm_privacy").mkdir(parents=True, exist_ok=True)
    write_json(out / "osm_privacy" / f"{st}.json", osm_cls, indent=None)
    write_json(out / "osm_privacy" / f"{st}_detail.json", detail, indent=None)
    res = {"state": st, "candidates": n, "classes": dict(Counter(cls_)), "rescored": int(np.isfinite(p_home).sum()),
           "osm": len(osm_cls), "osm_classes": dict(Counter(osm_cls.values())), "buildings": len(ctx.b_xy),
           "seconds": round(time.monotonic() - t0, 1)}
    log.info("privacy %s: %s", st, json.dumps(res))
    return res


def cmd_privacy(args: argparse.Namespace) -> None:
    t0 = time.monotonic()
    det = pq.read_table(args.detections, columns=["state", "near_known_osm"])
    counts = Counter(det.column("state").to_pylist())
    del det
    sts = [s for s, _ in counts.most_common()] if not args.states else [s.upper() for s in args.states]
    osm_states = set(pq.read_table(args.courts, columns=["state"]).column("state").to_pylist()) - {None, ""}
    sts += sorted(s for s in osm_states if s not in sts and not args.states)
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(classify_state, st, str(args.detections), str(args.rescore) if args.rescore else None,
                            str(args.osm_phome) if args.osm_phome else None, str(args.courts), str(args.sites_dir),
                            str(args.buildings_dir), str(args.out), args.padus_inset_m): st for st in sts}
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as exc:  # one bad state must not lose the others
                log.error("privacy %s failed: %s", futs[f], exc)
                results.append({"state": futs[f], "error": str(exc)})
    write_json(args.out / "privacy_states.json", sorted(results, key=lambda r: r["state"]))
    log.info("privacy: %d states in %.1f min", len(results), (time.monotonic() - t0) / 60)


# =========================================================================== summarize
def band_counts(conf: np.ndarray) -> dict[str, int]:
    return {f"{t:.1f}": int((conf >= t).sum()) for t in BANDS}


def cmd_summarize(args: argparse.Namespace) -> None:
    files = sorted((args.out / "privacy").glob("*.parquet"))
    t = pa.concat_tables([pq.read_table(p) for p in files], promote_options="default")
    item = pc.replace_substring_regex(t.column("item_id"), r"~[A-Z]{2}$", "")
    cand = pa.table({
        "id": t.column("id"), "state": t.column("state"), "lon": t.column("lon"), "lat": t.column("lat"),
        "conf": t.column("conf"), "verify_scan": t.column("verify"), "verify": t.column("verify2"),
        "conf2": t.column("conf2"), "p_home": t.column("p_home"), "privacy": t.column("privacy"),
        "site": t.column("site"), "privacy_reason": t.column("privacy_reason"), "box_w_m": t.column("box_w_m"),
        "box_h_m": t.column("box_h_m"), "edge": t.column("edge"), "naip_item": item, "naip_date": t.column("date"),
        **{c: t.column(c) for c in t.column_names if c.startswith("f_")},
    })
    cand = cand.sort_by([("conf", "descending")])
    pq.write_table(cand, args.out / "candidates_us.parquet", compression="zstd")
    log.info("candidates_us.parquet: %d rows", cand.num_rows)
    d = {k: cand.column(k).to_numpy(zero_copy_only=False) for k in ("state", "conf", "privacy", "verify", "site")}
    merge = json.loads((args.out / "merge_stats.json").read_text(encoding="utf-8"))
    rec = merge.get("known_court_recall", {}).get("by_state", {})
    plan = pq.read_table(args.plan / "plan.parquet", columns=["item_id", "state", "kept_tiles", "zone_km2"]).to_pydict()
    scanned: dict[str, Counter[str]] = {}
    for p in sorted(args.packed.glob("*.items.parquet")):
        it = pq.read_table(p, columns=["state", "tiles", "raw_detections", "item_seconds"]).to_pydict()
        for s_, tl, rd in zip(it["state"], it["tiles"], it["raw_detections"]):
            c = scanned.setdefault(s_, Counter())
            c["items"] += 1
            c["tiles"] += int(tl or 0)
            c["raw_detections"] += int(rd or 0)
    plan_st: dict[str, Counter[str]] = {}
    for s_, kt, zk in zip(plan["state"], plan["kept_tiles"], plan["zone_km2"]):
        c = plan_st.setdefault(s_, Counter())
        c["items"] += 1
        c["tiles"] += int(kt)
        c["km2"] += float(zk)
    names = state_names(args.states_file)
    osm_priv: dict[str, Counter[str]] = {}
    for p in sorted((args.out / "osm_privacy").glob("[A-Z][A-Z].json")):
        osm_priv[p.stem] = Counter(json.loads(p.read_text(encoding="utf-8")).values())
    rows = []
    for s_ in sorted(set(plan_st) | set(d["state"].tolist())):
        m = d["state"] == s_
        conf = d["conf"][m].astype(float)
        cls = d["privacy"][m]
        r = rec.get(s_, {})
        row: dict[str, Any] = {
            "state": s_, "name": names.get(s_, s_), "items_planned": plan_st.get(s_, {}).get("items", 0),
            "items_scanned": scanned.get(s_, {}).get("items", 0), "km2": round(plan_st.get(s_, {}).get("km2", 0.0)),
            "tiles_planned": plan_st.get(s_, {}).get("tiles", 0), "tiles_scanned": scanned.get(s_, {}).get("tiles", 0),
            "candidates": int(m.sum()), **{f"cand_ge_{k}": v for k, v in band_counts(conf).items()},
            **{f"{c}_ge_0.5": int(((cls == c) & (conf >= 0.5)).sum())
               for c in ("public", "residents_only", "private_home", "unknown", "restricted")},
            "osm_courts_labelable_in_scan": r.get("courts", 0),
            **{f"recall_{k}": r.get(k) for k in ("0.10", "0.25", "0.50", "0.60", "0.70")},
            "osm_features": sum(osm_priv.get(s_, {}).values()),
            **{f"osm_{c}": osm_priv.get(s_, {}).get(c, 0) for c in ("public", "unknown", "residents_only", "private_home",
                                                                    "restricted")},
        }
        rows.append(row)
    conf = d["conf"].astype(float)
    tot = {"candidates": int(len(conf)), "by_band": band_counts(conf),
           "by_class": {f"{t:.1f}": dict(Counter(d["privacy"][conf >= t].tolist())) for t in BANDS},
           "recall_all": merge.get("known_court_recall", {}).get("all"),
           "tiles_scanned": int(sum(c["tiles"] for c in scanned.values())),
           "items_scanned": int(sum(c["items"] for c in scanned.values())),
           "items_planned": len(plan["item_id"]), "tiles_planned": int(sum(plan["kept_tiles"]))}
    write_json(args.out / "state_summary.json", {"totals": tot, "states": rows})
    with open(args.out / "state_summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log.info("summary: %s", json.dumps(tot))


# =========================================================================== review
def cmd_review(args: argparse.Namespace) -> None:
    """Blind review sheets of public candidates, stratified by (conf band x fresh-chip verifier band)."""
    from PIL import Image, ImageDraw

    t = pq.read_table(args.out / "candidates_us.parquet",
                      columns=["id", "state", "conf", "verify", "verify_scan", "conf2", "p_home", "privacy", "site",
                               "box_w_m", "box_h_m", "lon", "lat"])
    t = t.filter(pc.is_in(t.column("privacy"), value_set=pa.array(args.classes)))
    t = t.filter(pc.is_valid(t.column("verify")))
    d = t.to_pydict()
    rng = random.Random(args.seed)
    n = len(d["id"])
    conf = np.asarray(d["conf"], float)
    ver = np.asarray(d["verify"], float)
    have = np.array([(args.chips / f"{i}.jpg").exists() for i in d["id"]])
    strata: dict[str, dict[str, int]] = {}
    picked: list[dict[str, Any]] = []
    for c0, c1 in zip(args.conf_bands[:-1], args.conf_bands[1:]):
        for v0, v1 in zip(args.verify_bands[:-1], args.verify_bands[1:]):
            key = f"c{c0:.2f}-{c1:.2f}|v{v0:.3f}-{v1:.3f}"
            m = (conf >= c0) & (conf < c1) & (ver >= v0) & (ver < v1)
            pool = np.flatnonzero(m & have)
            take = rng.sample(pool.tolist(), min(args.per_stratum, len(pool)))
            strata[key] = {"population": int(m.sum()), "sampled": len(take)}
            for i in take:
                picked.append({k: d[k][i] for k in d} | {"stratum": key})
    rng.shuffle(picked)
    out = args.out / "review" / args.name
    (out / "crops").mkdir(parents=True, exist_ok=True)
    thumbs = []
    gsd = config.TARGET_GSD_M
    for k, r in enumerate(picked, start=1):
        r["n"] = k
        im = Image.open(args.chips / f"{r['id']}.jpg").convert("RGB")
        c = im.width / 2
        h = args.crop_px / 2
        im = im.crop((int(c - h), int(c - h), int(c + h), int(c + h))).resize((args.thumb_px, args.thumb_px),
                                                                            Image.Resampling.BICUBIC)
        s = args.thumb_px / args.crop_px
        w, hh = r["box_w_m"] / gsd * s / 2, r["box_h_m"] / gsd * s / 2
        mid = args.thumb_px / 2
        dr = ImageDraw.Draw(im)
        dr.rectangle([mid - w - 4, mid - hh - 4, mid + w + 4, mid + hh + 4], outline=(255, 255, 0), width=2)
        dr.rectangle([0, 0, 34, 14], fill=(0, 0, 0))
        dr.text((2, 1), f"{k:03d}", fill=(255, 255, 0))
        im.save(out / "crops" / f"{k:03d}.jpg", quality=90)
        thumbs.append(im)
    cols, rws = 5, 4
    for k in range(0, len(thumbs), cols * rws):
        sheet = Image.new("RGB", (cols * args.thumb_px, rws * args.thumb_px), "white")
        for j, th in enumerate(thumbs[k:k + cols * rws]):
            sheet.paste(th, ((j % cols) * args.thumb_px, (j // cols) * args.thumb_px))
        sheet.save(out / f"sheet_{k // (cols * rws):02d}.jpg", quality=88)
    meta = {"classes": args.classes, "conf_bands": args.conf_bands, "verify_bands": args.verify_bands,
            "strata": strata, "crop_m": args.crop_px * gsd, "population": n, "items": picked,
            "note": "blind review sample (only a number on each crop); model-assistant labels, NOT gold"}
    write_json(out / "sample.json", meta)
    log.info("review %s: %d crops in %d sheets -> %s; strata %s", args.name, len(picked), len(range(0, len(thumbs), 20)),
             out, json.dumps(strata))


def _estimate(items: list[dict[str, Any]], strata: dict[str, dict[str, int]], keep: Any, unsure_as_not: bool,
              rng: random.Random | None = None) -> tuple[float, float, float]:
    """(precision, expected courts, candidates) over the population passing ``keep``, each stratum weighted by
    population x share of its sample passing."""
    num = den = 0.0
    by: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by.setdefault(it["stratum"], []).append(it)
    for s, its in by.items():
        if rng is not None:
            its = [rng.choice(its) for _ in its]
        passing = [it for it in its if keep(it)]
        if not passing:
            continue
        share = len(passing) / len(its)
        dec = [it for it in passing if it["code"] in ("C", "N") or (unsure_as_not and it["code"] == "U")]
        if not dec:
            continue
        p = sum(it["code"] == "C" for it in dec) / len(dec)
        num += strata[s]["population"] * share * p
        den += strata[s]["population"] * share
    return (num / den if den else float("nan"), num, den)


def cmd_score(args: argparse.Namespace) -> None:
    out = args.out / "review" / args.name
    meta = json.loads((out / "sample.json").read_text(encoding="utf-8"))
    lab = json.loads(args.labels.read_text(encoding="utf-8"))["labels"]
    items = []
    for it in meta["items"]:
        code = lab.get(str(it["n"]))
        if code is None:
            continue
        it["code"], _, it["access"] = code.partition(":")
        items.append(it)
    strata = meta["strata"]
    res: dict[str, Any] = {"note": "model-assistant labels (blind), NOT gold", "labeled": len(items),
                           "codes": dict(Counter(it["code"] for it in items)),
                           "home_courts": sum(it["code"] == "C" and it["access"] == "H" for it in items),
                           "by_stratum": {}, "grid": []}
    for s in strata:
        its = [it for it in items if it["stratum"] == s]
        res["by_stratum"][s] = {"population": strata[s]["population"], **Counter(i["code"] for i in its)}
    rng = random.Random(7)
    for c_min in args.conf_min:
        for v_min in args.verify_min:
            def keep(it: dict[str, Any], c_min: float = c_min, v_min: float = v_min) -> bool:
                return it["conf"] >= c_min and it["verify"] >= v_min
            p, exp_c, pop = _estimate(items, strata, keep, False)
            p2, _, _ = _estimate(items, strata, keep, True)
            boots = sorted(v for v in (_estimate(items, strata, keep, False, rng)[0] for _ in range(1000)) if v == v)
            ci = [round(boots[int(0.025 * len(boots))], 3), round(boots[int(0.975 * len(boots)) - 1], 3)] if boots else None
            res["grid"].append({"conf_min": c_min, "verify_min": v_min, "candidates": round(pop), "precision": round(p, 3),
                                "precision_unsure_as_not": round(p2, 3), "ci95": ci, "expected_courts": round(exp_c),
                                "reviewed": sum(1 for it in items if keep(it)),
                                "reviewed_C_N_U": [sum(1 for it in items if keep(it) and it["code"] == c) for c in "CNU"]})
    write_json(out / "scores.json", res)
    print(json.dumps({k: v for k, v in res.items() if k != "grid"}, indent=1))
    print("| conf >= | verify >= | candidates | precision (95% CI) | unsure as not | reviewed C/N/U |")
    print("|---|---|---|---|---|---|")
    for g in res["grid"]:
        ci_s = f"{g['ci95'][0]:.2f}-{g['ci95'][1]:.2f}" if g["ci95"] else "-"
        print(f"| {g['conf_min']:.2f} | {g['verify_min']:.3f} | {g['candidates']} | {g['precision']:.2f} ({ci_s}) | "
              f"{g['precision_unsure_as_not']:.2f} | {'/'.join(map(str, g['reviewed_C_N_U']))} |")


# =========================================================================== app-ready
def cmd_app_ready(args: argparse.Namespace) -> None:
    from .export_app import SITE_VALUES, _feature, osm_features, validate

    t = pq.read_table(args.out / "candidates_us.parquet",
                      columns=["id", "state", "lon", "lat", "conf", "verify", "conf2", "privacy", "site"])
    d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in t.column_names}
    ver = np.nan_to_num(d["verify"].astype(float), nan=-1.0)
    c2 = np.nan_to_num(d["conf2"].astype(float), nan=-1.0)
    sel = (d["privacy"] == "public") & (d["conf"] >= args.min_conf) & (ver >= args.min_verify) & (c2 >= args.min_conf2)
    names = state_names(args.states_file)
    updated = args.updated
    dst = args.out / "app_ready"
    dst.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {"operating_point": {"privacy": "public", "min_conf": args.min_conf,
                                                 "min_verify": args.min_verify, "min_conf2": args.min_conf2},
                             "updated": updated, "states": {}}
    for code in sorted(names):
        label = state_slug(names[code])
        osm_file = args.osm_dir / f"courts_{code}.geojson"
        priv_file = args.out / "osm_privacy" / f"{code}.json"
        osm = []
        if osm_file.exists():
            priv = json.loads(priv_file.read_text(encoding="utf-8")) if priv_file.exists() else None
            if priv is None:
                log.warning("%s: no OSM privacy classes; OSM courts left out", code)
            else:
                osm = osm_features(osm_file, label, updated, None, priv, False)
        idx = np.flatnonzero(sel & (d["state"] == code))
        det = []
        for i in idx[np.argsort(-d["conf"][idx])]:
            f = _feature(round(float(d["lon"][i]), 6), round(float(d["lat"][i]), 6), {
                "id": str(d["id"][i]), "name": None, "source": "detected", "status": "candidate",
                "confidence": round(float(d["conf"][i]), 3), "hoops": None, "surface": None, "lit": None,
                "address": None, "region": label, "osm_id": None, "updated": updated})
            if d["site"][i] in SITE_VALUES:
                f["properties"]["site"] = str(d["site"][i])
            det.append(f)
        fc = {"type": "FeatureCollection", "features": osm + det}
        errors = validate(fc)
        if errors:
            raise SystemExit(f"{code}: validation failed: {errors[:5]}")
        write_json(dst / f"{code}.geojson", fc, indent=None)
        index["states"][code] = {"region": label, "osm": len(osm), "detected": len(det),
                                 "detected_by_site": dict(Counter(f["properties"].get("site") or "-" for f in det))}
    tot = {"osm": sum(v["osm"] for v in index["states"].values()),
           "detected": sum(v["detected"] for v in index["states"].values())}
    index["totals"] = tot
    write_json(dst / "index.json", index)
    log.info("app_ready: %d states, %s -> %s", len(index["states"]), tot, dst)


# =========================================================================== CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--out", type=Path, default=OUT)
        p.add_argument("--states-file", type=Path, default=config.STATES_FILE)
        add_common_args(p)

    p = sub.add_parser("split-sites")
    p.add_argument("--sites-dir", type=Path, default=SITES_DIR)
    p.add_argument("--pad-deg", type=float, default=0.05)
    common(p)
    p.set_defaults(out=BY_STATE)
    p = sub.add_parser("osm-geojson")
    p.add_argument("--courts", type=Path, default=config.OSM_US_DIR / "courts_us.parquet")
    common(p)
    p.set_defaults(out=config.OSM_US_DIR)
    p = sub.add_parser("osm-phome")
    p.add_argument("--courts", type=Path, default=config.OSM_US_DIR / "courts_us.parquet")
    p.add_argument("--context-model", type=Path, default=config.WEIGHTS_DIR / "context_v2.pt")
    p.add_argument("--naip-dir", type=Path, default=config.NAIP_GEOPARQUET_DIR)
    p.add_argument("--chunk", type=int, default=8192)
    p.add_argument("--workers", type=int, default=128)
    common(p)
    p.set_defaults(out=NAT_DATA / "osm_phome.parquet")
    p = sub.add_parser("privacy")
    p.add_argument("--detections", type=Path, default=OUT / "detections.parquet")
    p.add_argument("--rescore", type=Path)
    p.add_argument("--osm-phome", type=Path)
    p.add_argument("--courts", type=Path, default=config.OSM_US_DIR / "courts_us.parquet")
    p.add_argument("--sites-dir", type=Path, default=BY_STATE)
    p.add_argument("--buildings-dir", type=Path, default=SITES_DIR)
    p.add_argument("--padus-inset-m", type=float, default=20.0)
    p.add_argument("--states", nargs="*")
    p.add_argument("--workers", type=int, default=16)
    common(p)
    p = sub.add_parser("summarize")
    p.add_argument("--plan", type=Path, default=config.DATA_DIR / "scan2" / "US")
    p.add_argument("--packed", type=Path, default=NAT_DATA / "packed")
    common(p)
    p = sub.add_parser("review")
    p.add_argument("--chips", type=Path, default=NAT_DATA / "rescore_chips")
    p.add_argument("--name", default="public")
    p.add_argument("--classes", nargs="+", default=["public"])
    p.add_argument("--conf-bands", type=float, nargs="+", default=[0.4, 0.5, 0.6, 0.7, 1.01])
    p.add_argument("--verify-bands", type=float, nargs="+", default=[0.0, 0.9, 0.98, 1.001])
    p.add_argument("--per-stratum", type=int, default=15)
    p.add_argument("--crop-px", type=int, default=200)
    p.add_argument("--thumb-px", type=int, default=300)
    p.add_argument("--seed", type=int, default=2026)
    common(p)
    p = sub.add_parser("score")
    p.add_argument("--name", default="public")
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--conf-min", type=float, nargs="+", default=[0.4, 0.5, 0.6, 0.7])
    p.add_argument("--verify-min", type=float, nargs="+", default=[0.0, 0.5, 0.9, 0.95, 0.98, 0.99])
    common(p)
    p = sub.add_parser("app-ready")
    p.add_argument("--osm-dir", type=Path, default=config.OSM_US_DIR)
    p.add_argument("--min-conf", type=float, required=True)
    p.add_argument("--min-verify", type=float, required=True)
    p.add_argument("--min-conf2", type=float, default=-1.0)
    p.add_argument("--updated", default=dt.date.today().isoformat())
    common(p)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    {"split-sites": cmd_split_sites, "osm-geojson": cmd_osm_geojson, "osm-phome": cmd_osm_phome,
     "privacy": cmd_privacy, "summarize": cmd_summarize, "review": cmd_review, "score": cmd_score,
     "app-ready": cmd_app_ready}[args.cmd](args)


if __name__ == "__main__":
    main()
