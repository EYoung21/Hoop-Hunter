"""Privacy classification of detected courts: only courts the public can use reach the app.

Every candidate gets ``privacy`` = public | residents_only | private_home | restricted | unknown,
plus ``site`` (park, school, college, church, rec, community) when public, and the reasons.

Evidence (sites.py builds the layers):

* public-site polygons (OSM parks, playgrounds, recreation grounds, sports centres, schools,
  landuse=education, colleges, places of worship, landuse=religious, community centres) and
  PAD-US public land; point-only sites (NCES schools, IPEDS colleges, Overture places, OSM
  nodes) with a per-type radius;
* restricted polygons (prisons, military land) and private sites (access=private parks/clubs);
* residential context: OSM landuse=residential / residential=apartments polygons,
  building=apartments|dormitory, and Overture building footprints around the court
  (house-sized 40-450 m^2, mid 450-1,500, large >= 1,500; nearest building and its class);
* optionally ``p_home`` from the context CNN (cropcls, 160 m NAIP crop around the court).

Rules, first match wins (``classify_point``):

1. inside a restricted polygon                                   -> restricted
2. inside an OSM public-site polygon (not access-private), or at
   least ``padus_inset_m`` (20 m) inside PAD-US public land        -> public,
   unless the CNN is sure it is a home lot (p_home >= 0.85)       -> private_home
3. inside an access-private site polygon (HOA park, club)          -> residents_only
4. apartments: residential=apartments landuse, or an apartment
   building within 40 m                                           -> residents_only
5. within the radius of a point site                              -> public, unless the lot
   looks residential (a house-sized building within 35 m and no larger or civic building within
   100 m, or p_home >= 0.5)                                        -> private_home
6. house-sized building within 35 m, or landuse=residential, or
   p_home >= 0.7                                                  -> private_home
7. otherwise                                                      -> unknown

The export (export_app) keeps only ``public`` by default; residents_only and unknown are
opt-in flags; private_home and restricted are never exported.

Commands
--------
    python -m hoop_pipeline.privacy classify --candidates out/scan2_tn/detections.parquet --scope TN \\
        --buildings data/sites/buildings_TN.parquet --context-model weights/context_v2.pt
    python -m hoop_pipeline.privacy context-data --out data/crops/context.npz     # OSM-labelled crops for the CNN
    python -m hoop_pipeline.privacy eval-osm --scope TN                            # rules on OSM-labelled courts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from pyproj import Transformer

from . import config
from .common import add_common_args, setup_logging, write_json
from .geoparquet import read_geoparquet
from .osm_tags import PRIVATE_ACCESS

log = logging.getLogger("privacy")
ALBERS = "EPSG:5070"
SITES_DIR = config.DATA_DIR / "sites"
SITE_PRIORITY = {"school": 0, "college": 1, "church": 2, "community": 3, "rec": 4, "park": 5}
# Footprint classes. A detached house is 40-600 m^2 (large suburban homes reach 450-600; v1 review, 2026-10-09);
# a building Overture classes as residential/house counts as a house up to 1,200 m^2; a building whose subtype is
# civic/education/religious/commercial/... never does, whatever its size.
HOUSE_MIN, HOUSE_MAX, MID_MAX, RES_HOUSE_MAX = 40.0, 600.0, 1500.0, 1200.0
RESIDENTIAL_CLASSES = {"house", "detached", "semidetached_house", "residential", "bungalow", "terrace", "farm",
                       "cabin", "static_caravan"}
APARTMENT_CLASSES = {"apartments", "dormitory"}
CIVIC_SUBTYPES = {"civic", "education", "religious", "commercial", "industrial", "medical", "service", "entertainment",
                  "transportation", "military"}
CIVIC_CLASSES = {"church", "chapel", "cathedral", "mosque", "synagogue", "temple", "religious", "school", "college",
                 "university", "kindergarten", "civic", "public", "government", "sports_hall", "sports_centre",
                 "grandstand", "pavilion", "commercial", "retail", "office", "industrial", "warehouse", "hospital"}


def _tf(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


def _to_alb(geoms: np.ndarray) -> np.ndarray:
    tr = _tf("EPSG:4326", ALBERS)
    return shapely.transform(geoms, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))


# =========================================================================== context layers
@dataclass
class Context:
    site_geom: np.ndarray       # Albers
    site_kind: np.ndarray       # park, school, ..., restricted
    site_source: np.ndarray
    site_name: np.ndarray
    site_private: np.ndarray
    site_point: np.ndarray      # bool
    site_buf: np.ndarray
    site_area: np.ndarray
    site_tree: Any
    res_geom: np.ndarray
    res_kind: np.ndarray
    res_tree: Any
    b_xy: np.ndarray            # building centroids, Albers (n, 2)
    b_area: np.ndarray
    b_class: np.ndarray
    b_subtype: np.ndarray
    b_tree: Any

    @classmethod
    def load(cls, scope: str, buildings: list[Path], sites_dir: Path = SITES_DIR,
             bbox: tuple[float, float, float, float] | None = None) -> Context:
        t0 = time.monotonic()
        filt = None
        s, sg = read_geoparquet(sites_dir / f"sites_{scope}.parquet",
                                columns=["site", "source", "name", "private", "geom_kind", "buffer_m"], filters=filt)
        if bbox is not None:
            b = shapely.bounds(sg)
            keep = (b[:, 2] >= bbox[0]) & (b[:, 0] <= bbox[2]) & (b[:, 3] >= bbox[1]) & (b[:, 1] <= bbox[3])
            s = {k: v[keep] for k, v in s.items()}
            sg = sg[keep]
        sg_alb = _to_alb(sg)
        is_pt = s["geom_kind"] == "point"
        r, rg = read_geoparquet(sites_dir / f"residential_{scope}.parquet", columns=["kind", "access"])
        if bbox is not None:
            b = shapely.bounds(rg)
            keep = (b[:, 2] >= bbox[0]) & (b[:, 0] <= bbox[2]) & (b[:, 3] >= bbox[1]) & (b[:, 1] <= bbox[3])
            r = {k: v[keep] for k, v in r.items()}
            rg = rg[keep]
        rg_alb = _to_alb(rg)
        bx: list[np.ndarray] = []
        ba: list[np.ndarray] = []
        bc: list[np.ndarray] = []
        bs: list[np.ndarray] = []
        tr = _tf("EPSG:4326", ALBERS)
        for p in buildings:
            t = pq.read_table(p, columns=["lon", "lat", "area_m2", "class", "subtype"])
            lon = t.column("lon").to_numpy()
            lat = t.column("lat").to_numpy()
            if bbox is not None:
                m = (lon >= bbox[0]) & (lon <= bbox[2]) & (lat >= bbox[1]) & (lat <= bbox[3])
                t = t.filter(pa.array(m))
                lon, lat = lon[m], lat[m]
            x, y = tr.transform(lon, lat)
            bx.append(np.column_stack([x, y]))
            ba.append(t.column("area_m2").to_numpy())
            bc.append(np.array([str(v or "") for v in t.column("class").to_pylist()], dtype=object))
            bs.append(np.array([str(v or "") for v in t.column("subtype").to_pylist()], dtype=object))
        b_xy = np.concatenate(bx) if bx else np.zeros((0, 2))
        b_tree = shapely.STRtree(shapely.points(b_xy[:, 0], b_xy[:, 1])) if len(b_xy) else None
        ctx = cls(sg_alb, s["site"].astype(str), s["source"].astype(str), s["name"], s["private"].astype(bool), is_pt,
                  s["buffer_m"].astype(float), shapely.area(sg_alb), shapely.STRtree(sg_alb), rg_alb,
                  r["kind"].astype(str), shapely.STRtree(rg_alb) if len(rg_alb) else None, b_xy,
                  np.concatenate(ba) if ba else np.zeros(0), np.concatenate(bc) if bc else np.zeros(0, object),
                  np.concatenate(bs) if bs else np.zeros(0, object), b_tree)
        log.info("context %s: %d sites (%d points), %d residential polygons, %d buildings (%.0f s)", scope, len(sg_alb),
                 int(is_pt.sum()), len(rg_alb), len(b_xy), time.monotonic() - t0)
        return ctx


# =========================================================================== features + rules
def point_features(ctx: Context, x: float, y: float) -> dict[str, Any]:
    p = shapely.Point(x, y)
    f: dict[str, Any] = {"poly_sites": [], "pt_sites": [], "restricted": False, "private_site": False,
                         "padus_inset_m": -1.0}
    near = ctx.site_tree.query(p, predicate="dwithin", distance=300.0)
    for j in near:
        kind = ctx.site_kind[j]
        g = ctx.site_geom[j]
        if ctx.site_point[j]:
            d = float(shapely.distance(p, g))
            if d <= ctx.site_buf[j] and kind != "restricted":
                f["pt_sites"].append((kind, ctx.site_source[j], d, ctx.site_name[j], float(g.x), float(g.y)))
            continue
        if not shapely.intersects(g, p):
            continue
        if kind == "restricted":
            f["restricted"] = True
            continue
        if ctx.site_private[j]:
            f["private_site"] = True
            continue
        if ctx.site_source[j] == "padus":
            inset = float(shapely.distance(p, shapely.boundary(g)))
            f["padus_inset_m"] = max(f["padus_inset_m"], inset)
            f["poly_sites"].append((kind, "padus", float(ctx.site_area[j]), ctx.site_name[j], inset))
        else:
            f["poly_sites"].append((kind, ctx.site_source[j], float(ctx.site_area[j]), ctx.site_name[j], 1e9))
    f["in_residential"] = f["in_apartments"] = False
    f["apt_bldg_40m"] = False
    if ctx.res_tree is not None:
        for j in ctx.res_tree.query(p, predicate="dwithin", distance=40.0):
            k = ctx.res_kind[j]
            if k == "apartments_bldg":
                f["apt_bldg_40m"] = True
            elif shapely.intersects(ctx.res_geom[j], p):
                if k == "apartments":
                    f["in_apartments"] = True
                else:
                    f["in_residential"] = True
    f.update({"house_35m": 0, "house_60m": 0, "mid_100m": 0, "large_150m": 0, "nearest_bldg_m": float("inf"),
              "nearest_bldg_area": float("nan"), "nearest_bldg_class": "", "apt_class_40m": False,
              "civic_150m": False, "house_xy_35m": []})
    if ctx.b_tree is not None:
        idx = ctx.b_tree.query(p, predicate="dwithin", distance=150.0)
        if len(idx):
            d = np.hypot(ctx.b_xy[idx, 0] - x, ctx.b_xy[idx, 1] - y)
            a = ctx.b_area[idx]
            cl = ctx.b_class[idx]
            sub = ctx.b_subtype[idx]
            civic = np.isin(sub, list(CIVIC_SUBTYPES)) | np.isin(cl, list(CIVIC_CLASSES))
            resid = (sub == "residential") | np.isin(cl, list(RESIDENTIAL_CLASSES))
            house = ~civic & (a >= HOUSE_MIN) & ((a < HOUSE_MAX) | (resid & (a < RES_HOUSE_MAX)))
            f["house_35m"] = int((house & (d <= 35)).sum())
            f["house_xy_35m"] = [(float(bx), float(by)) for bx, by in ctx.b_xy[idx[house & (d <= 35)]]]
            f["house_60m"] = int((house & (d <= 60)).sum())
            f["mid_100m"] = int((~house & (a >= HOUSE_MAX) & (d <= 100)).sum() + (civic & (a >= 150) & (d <= 100)).sum())
            f["large_150m"] = int((a >= MID_MAX).sum())
            k = int(np.argmin(d))
            f["nearest_bldg_m"] = float(d[k])
            f["nearest_bldg_area"] = float(a[k])
            f["nearest_bldg_class"] = str(cl[k] or sub[k])
            f["apt_class_40m"] = bool(np.isin(cl[d <= 40], list(APARTMENT_CLASSES)).any())
            f["civic_150m"] = bool(civic.any())
    return f


# Context-CNN thresholds (held-out TN OSM courts: AUC 0.99; val cells: AUC 0.97, P/R 0.88/0.89 at 0.7, 0.83/0.90 at 0.5)
VETO_POLYGON, VETO_POINT, HOME_CNN, HOUSE_LOT_CNN = 0.85, 0.5, 0.7, 0.25
SITE_BUILDING_M = 25.0  # a house-sized building this close to a point site's location is the site's own building


def classify_point(f: dict[str, Any], p_home: float = float("nan"), padus_inset_m: float = 20.0) -> tuple[str, str, str]:
    """(privacy class, site type, reason) from point_features (+ optional CNN home probability)."""
    ph = p_home if p_home == p_home else 0.0  # NaN -> 0
    if f["restricted"]:
        return "restricted", "", "inside prison/military land"
    strong = [s for s in f["poly_sites"] if s[1] != "padus" or s[4] >= padus_inset_m]
    if strong:
        site = sorted(strong, key=lambda s: (SITE_PRIORITY.get(s[0], 9), s[2]))[0]
        if ph >= VETO_POLYGON:
            return "private_home", "", f"inside {site[1]} {site[0]} polygon but CNN home p={ph:.2f}"
        return "public", site[0], f"inside {site[1]} {site[0]} polygon"
    if f["private_site"]:
        return "residents_only", "", "inside an access=private site (HOA park, club)"
    if f["in_apartments"] or f["apt_bldg_40m"] or f["apt_class_40m"]:
        return "residents_only", "", "apartment complex"
    if f["pt_sites"]:
        site = sorted(f["pt_sites"], key=lambda s: (SITE_PRIORITY.get(s[0], 9), s[2]))[0]
        # the building at the site's own point (a small church, a school annex) is the site, not a house
        sx, sy = site[4], site[5]
        houses = [h for h in f["house_xy_35m"] if (h[0] - sx) ** 2 + (h[1] - sy) ** 2 > SITE_BUILDING_M ** 2]
        house_lot = bool(houses) and f["mid_100m"] == 0
        # with the context CNN, the house rule also needs a weak home signal; without it, the house rule alone decides
        cnn = p_home == p_home
        if ph >= VETO_POINT or (house_lot and (not cnn or ph >= HOUSE_LOT_CNN)):
            why = f"CNN home p={ph:.2f}" if ph >= VETO_POINT else "houses within 35 m, no larger or civic building nearby"
            return "private_home", "", f"near {site[1]} {site[0]} point but residential lot ({why})"
        return "public", site[0], f"within {site[2]:.0f} m of {site[1]} {site[0]} point"
    if f["house_35m"] > 0 or f["in_residential"] or ph >= HOME_CNN:
        why = "house-sized building within 35 m" if f["house_35m"] else ("landuse=residential" if f["in_residential"]
                                                                          else f"CNN home p={ph:.2f}")
        return "private_home", "", why
    return "unknown", "", "no public site, no residential evidence"


# =========================================================================== classify
_CATALOGS: dict[str, Any] = {}


def naip_catalog(naip_dir: Path) -> Any:
    """NaipCatalog loaded once per process (rescore calls fetch_context_crops once per chunk)."""
    from .naip_index import NaipCatalog

    key = str(naip_dir)
    if key not in _CATALOGS:
        _CATALOGS[key] = NaipCatalog.load(naip_dir)
    return _CATALOGS[key]


def fetch_context_crops(lons: np.ndarray, lats: np.ndarray, states: np.ndarray, px: int, naip_dir: Path,
                        workers: int = 64) -> tuple[np.ndarray, np.ndarray]:
    from .naip_index import GroupedReader, plan_windows

    cat = naip_catalog(naip_dir)
    item = cat.select(lons, lats, states, half_m=px * config.TARGET_GSD_M / 2)
    wp = plan_windows(cat, item, lons, lats, px, config.TARGET_GSD_M)
    out = np.zeros((len(lons), px, px, 3), np.uint8)
    ok = np.zeros(len(lons), bool)

    def handle(key: Any, img: np.ndarray | None, err: str) -> int:
        i = int(key)
        if img is not None:
            out[i] = img
            ok[i] = True
        return i

    list(GroupedReader(cat, workers=workers).run(wp, np.arange(len(lons)), handle))
    return out, ok


def cmd_classify(args: argparse.Namespace) -> None:
    t0 = time.monotonic()
    t = pq.read_table(args.candidates)
    d = t.to_pydict()
    lon = np.asarray(d["lon"], float)
    lat = np.asarray(d["lat"], float)
    conf = np.asarray(d["conf"], float)
    pad = 0.05
    bbox = (float(lon.min()) - pad, float(lat.min()) - pad, float(lon.max()) + pad, float(lat.max()) + pad)
    ctx = Context.load(args.scope, args.buildings, args.sites_dir, bbox)
    x, y = _tf("EPSG:4326", ALBERS).transform(lon, lat)
    p_home = np.full(len(lon), np.nan)
    conf2 = np.full(len(lon), np.nan)
    verify2 = np.full(len(lon), np.nan)
    if args.rescore:  # p_home / second-detector score / verifier precomputed by hoop_pipeline.rescore
        r = pq.read_table(args.rescore, columns=["id", "chip_ok", "conf2", "verify", "p_home"]).to_pydict()
        pos = {cid: i for i, cid in enumerate(d["id"])}
        n_r = 0
        for cid, ok_, c2, v2, ph in zip(r["id"], r["chip_ok"], r["conf2"], r["verify"], r["p_home"]):
            i = pos.get(cid)
            if i is None or not ok_:
                continue
            p_home[i], conf2[i], verify2[i] = ph, c2, v2
            n_r += 1
        log.info("rescore: p_home / conf2 / verify for %d candidates from %s", n_r, args.rescore)
    if args.context_model:
        from .cropcls import CropClassifier

        clf = CropClassifier.load(args.context_model)
        sel = np.flatnonzero(conf >= args.context_min_conf)
        px = int(round(float(clf.meta.get("point_m", 160.0)) / config.TARGET_GSD_M))
        st = np.asarray(d.get("state") or [args.scope] * len(lon), dtype=object)
        imgs, ok = fetch_context_crops(lon[sel], lat[sel], st[sel], px, args.naip_dir)
        prob = clf.score_images(imgs[ok])
        p_home[sel[ok]] = prob
        log.info("context CNN scored %d candidates (conf >= %.2f)", int(ok.sum()), args.context_min_conf)
    cls_, site_, why_ = [], [], []
    feats: dict[str, list[Any]] = {k: [] for k in ("house_35m", "house_60m", "mid_100m", "large_150m", "nearest_bldg_m",
                                                   "nearest_bldg_area", "nearest_bldg_class", "in_residential",
                                                   "in_apartments", "padus_inset_m")}
    for i in range(len(lon)):
        f = point_features(ctx, float(x[i]), float(y[i]))
        c, s, w = classify_point(f, float(p_home[i]), args.padus_inset_m)
        cls_.append(c)
        site_.append(s)
        why_.append(w)
        for k in feats:
            feats[k].append(f[k])
    cols = {k: t.column(k) for k in t.column_names}
    cols.update({"privacy": pa.array(cls_), "site": pa.array(site_), "privacy_reason": pa.array(why_),
                 "p_home": pa.array(p_home)})
    if args.rescore:
        cols.update({"conf2": pa.array(conf2), "verify2": pa.array(verify2)})
    for k, v in feats.items():
        cols[f"f_{k}"] = pa.array(v)
    out = args.out or args.candidates.with_name(args.candidates.stem + "_privacy.parquet")
    pq.write_table(pa.table(cols), out, compression="zstd")
    # candidates GeoJSON for export_app (not near a known OSM court)
    near = np.asarray(d.get("near_known_osm") or [""] * len(lon), dtype=object)
    today = time.strftime("%Y-%m-%d")
    out_feats: list[dict[str, Any]] = []
    for i in np.argsort(-conf):
        if near[i]:
            continue
        v = d.get("verify", [None] * len(lon))[i]
        if verify2[i] == verify2[i]:
            v = verify2[i]
        out_feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(float(lon[i]), 6),
                                                                                       round(float(lat[i]), 6)]},
                      "properties": {"id": d["id"][i], "confidence": round(float(conf[i]), 4), "privacy": cls_[i],
                                     "site": site_[i] or None, "privacy_reason": why_[i],
                                     "p_home": None if p_home[i] != p_home[i] else round(float(p_home[i]), 3),
                                     "verify": None if v is None or v != v else round(float(v), 3),
                                     "conf2": None if conf2[i] != conf2[i] else round(float(conf2[i]), 4),
                                     "box_m": [round(float(d["box_w_m"][i]), 1), round(float(d["box_h_m"][i]), 1)],
                                     "state": d.get("state", [args.scope] * len(lon))[i], "region": args.region_label,
                                     "naip_item": d.get("item_id", [None] * len(lon))[i],
                                     "naip_date": d.get("date", [None] * len(lon))[i], "model": args.model_name,
                                     "detected": today}})
    gj = out.with_suffix(".geojson")
    write_json(gj, {"type": "FeatureCollection", "metadata": {"attribution": "Imagery: USDA NAIP (public domain). "
                    "Known courts and sites: (c) OpenStreetMap contributors (ODbL); USGS PAD-US; NCES EDGE; Overture Maps "
                    "(CDLA-Permissive-2.0 / Apache-2.0); Overture buildings (ODbL)."}, "features": out_feats}, indent=None)
    log.info("wrote %d candidate features (not near known OSM courts) to %s", len(out_feats), gj)
    fresh = np.asarray(d.get("near_known_osm") or [""] * len(lon), dtype=object) == ""
    summ: dict[str, Any] = {"candidates": len(lon), "by_class": dict(Counter(cls_))}
    for thr in (0.3, 0.5, 0.6, 0.7):
        m = (conf >= thr) & fresh
        summ[f"not_known_conf_ge_{thr}"] = dict(Counter(np.asarray(cls_, dtype=object)[m].tolist()))
        summ[f"public_sites_conf_ge_{thr}"] = dict(Counter(np.asarray(site_, dtype=object)[m & (np.asarray(cls_) == "public")]
                                                           .tolist()))
    summ["seconds"] = round(time.monotonic() - t0, 1)
    write_json(out.with_suffix(".json"), summ)
    log.info("privacy: %s -> %s", json.dumps(summ), out)


def cmd_classify_osm(args: argparse.Namespace) -> None:
    """Privacy class of every OSM basketball feature of a state file (courts_<ST>.geojson), for
    export_app --osm-privacy. Uses the same rules (no CNN unless --context-model is given)."""
    from .common import read_json

    feats = read_json(args.osm)["features"]
    refs = [f["properties"]["osm_ref"] for f in feats]
    lon = np.array([f["properties"]["center_lon"] for f in feats], float)
    lat = np.array([f["properties"]["center_lat"] for f in feats], float)
    bbox = (float(lon.min()) - 0.05, float(lat.min()) - 0.05, float(lon.max()) + 0.05, float(lat.max()) + 0.05)
    ctx = Context.load(args.scope, args.buildings, args.sites_dir, bbox)
    p_home = np.full(len(lon), np.nan)
    if args.context_model:
        from .cropcls import CropClassifier

        clf = CropClassifier.load(args.context_model)
        px = int(round(float(clf.meta.get("point_m", 160.0)) / config.TARGET_GSD_M))
        imgs, ok = fetch_context_crops(lon, lat, np.array([args.scope] * len(lon), dtype=object), px, args.naip_dir)
        p_home[ok] = clf.score_images(imgs[ok])
    x, y = _tf("EPSG:4326", ALBERS).transform(lon, lat)
    out: dict[str, str] = {}
    detail: dict[str, Any] = {}
    for i, ref in enumerate(refs):
        c, site, why = classify_point(point_features(ctx, float(x[i]), float(y[i])), float(p_home[i]), args.padus_inset_m)
        out[ref] = c
        detail[ref] = {"privacy": c, "site": site, "reason": why, "access": feats[i]["properties"]["tags"].get("access"),
                       "p_home": None if p_home[i] != p_home[i] else round(float(p_home[i]), 3)}
    write_json(args.out, out)
    write_json(args.out.with_name(args.out.stem + "_detail.json"), detail)
    acc = Counter((detail[r]["access"] or "-", detail[r]["privacy"]) for r in refs)
    log.info("OSM courts %s: %s; (access, class) %s", args.osm.name, dict(Counter(out.values())), dict(acc.most_common(20)))


# =========================================================================== OSM-labelled courts
def osm_labelled_courts(context_path: Path, courts_path: Path, scope_states: list[str] | None,
                        sites_scope: str) -> dict[str, np.ndarray]:
    """Courts and pitches with a public / home label from OSM tags alone (no buildings):

    home    access=private|no pitch (basketball, tennis, pickleball, multi; 60-1,500 m^2) inside
            landuse=residential (not apartments), not in any public-site polygon, no apartment
            building within 40 m;
    public  pitch inside an OSM public-site polygon, neither the pitch nor the site access-private.
    """
    a, g = read_geoparquet(courts_path, columns=["osm_ref", "state", "area_m2", "tags", "label_ok", "center_lon",
                                                 "center_lat"])
    acc = np.array([str(json.loads(t).get("access", "")).lower() for t in a["tags"]], dtype=object)
    sel = a["label_ok"].astype(bool)
    refs = list(a["osm_ref"][sel])
    lons = list(a["center_lon"][sel])
    lats = list(a["center_lat"][sel])
    sts = list(a["state"][sel])
    accs = list(acc[sel])
    kinds = ["basketball"] * len(refs)
    c, _ = read_geoparquet(context_path, columns=["kind", "osm_type", "osm_id", "access", "center_lon", "center_lat",
                                                  "area_m2", "state"],
                           filters=[("kind", "in", ["pitch_tennis", "pitch_pickleball", "pitch_other"])],
                           with_geometry=False)
    ok = (c["area_m2"] >= 60) & (c["area_m2"] <= 1500)
    refs += [f"{t}/{i}" for t, i in zip(c["osm_type"][ok], c["osm_id"][ok])]
    lons += list(c["center_lon"][ok])
    lats += list(c["center_lat"][ok])
    sts += list(c["state"][ok])
    accs += [str(v or "").lower() for v in c["access"][ok]]
    kinds += list(c["kind"][ok])
    lon_a, lat_a = np.array(lons, float), np.array(lats, float)
    st_a = np.array(sts, dtype=object)
    keep = np.ones(len(lon_a), bool) if not scope_states else np.isin(st_a, scope_states)
    lon_a, lat_a, st_a = lon_a[keep], lat_a[keep], st_a[keep]
    acc_a = np.array(accs, dtype=object)[keep]
    ref_a = np.array(refs, dtype=object)[keep]
    kind_a = np.array(kinds, dtype=object)[keep]
    ctx_sites, sg = read_geoparquet(SITES_DIR / f"sites_{sites_scope}.parquet", columns=["site", "source", "private",
                                                                                         "geom_kind"])
    polys = (ctx_sites["geom_kind"] == "polygon") & (ctx_sites["source"] == "osm")
    pts = shapely.points(lon_a, lat_a)
    stree = shapely.STRtree(sg[polys])
    pi, si = stree.query(pts, predicate="within")
    site_kind = np.full(len(pts), "", dtype=object)
    site_priv = np.zeros(len(pts), bool)
    restricted = np.zeros(len(pts), bool)
    pidx = np.flatnonzero(polys)
    for p_, s_ in zip(pi, si):
        k = ctx_sites["site"][pidx[s_]]
        if k == "restricted":
            restricted[p_] = True
        elif ctx_sites["private"][pidx[s_]]:
            site_priv[p_] = True
        elif not site_kind[p_] or SITE_PRIORITY.get(k, 9) < SITE_PRIORITY.get(site_kind[p_], 9):
            site_kind[p_] = k
    r, rg = read_geoparquet(SITES_DIR / f"residential_{sites_scope}.parquet", columns=["kind"])
    rtree = shapely.STRtree(rg)
    ri, rj = rtree.query(pts, predicate="within")
    in_res = np.zeros(len(pts), bool)
    in_apt = np.zeros(len(pts), bool)
    for p_, r_ in zip(ri, rj):
        if r["kind"][r_] == "residential":
            in_res[p_] = True
        else:
            in_apt[p_] = True
    priv = np.isin(acc_a, list(PRIVATE_ACCESS - {"students", "permit"}))
    home = priv & in_res & ~in_apt & (site_kind == "") & ~site_priv & ~restricted
    public = (site_kind != "") & ~priv & ~site_priv & ~restricted
    label = np.where(home, "home", np.where(public, "public", ""))
    log.info("OSM-labelled courts: %d home, %d public (of %d pitches)", int(home.sum()), int(public.sum()), len(pts))
    return {"ref": ref_a, "lon": lon_a, "lat": lat_a, "state": st_a, "kind": kind_a, "label": label.astype(object),
            "site": site_kind}


def cell_val(lat: float, lon: float, frac: float, cell_deg: float = 0.1) -> bool:
    key = f"ctx:{math.floor(lat / cell_deg)}:{math.floor(lon / cell_deg)}"
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < frac


def cmd_context_data(args: argparse.Namespace) -> None:
    lab = osm_labelled_courts(args.context, args.courts, None, args.sites_scope)
    rng = np.random.default_rng(args.seed)
    idx_home = np.flatnonzero(lab["label"] == "home")
    idx_pub = np.flatnonzero(lab["label"] == "public")
    if len(idx_home) > args.max_per_class:
        idx_home = rng.choice(idx_home, args.max_per_class, replace=False)
    n_pub = min(len(idx_pub), max(args.max_per_class, len(idx_home)))
    idx_pub = rng.choice(idx_pub, n_pub, replace=False)
    idx = np.sort(np.r_[idx_home, idx_pub])
    px = int(round(args.point_m / config.TARGET_GSD_M))
    imgs, ok = fetch_context_crops(lab["lon"][idx], lab["lat"][idx], lab["state"][idx], px, args.naip_dir, args.workers)
    idx = idx[ok]
    imgs = imgs[ok]
    # resize to the model input once, here, to keep the file small
    import torch

    x = torch.from_numpy(imgs).permute(0, 3, 1, 2).float()
    out = []
    for k in range(0, len(x), 512):
        out.append(torch.nn.functional.interpolate(x[k:k + 512], size=(args.input, args.input), mode="bilinear",
                                                   align_corners=False, antialias=True).round().clamp(0, 255)
                   .to(torch.uint8).permute(0, 2, 3, 1).numpy())
    X = np.concatenate(out) if out else np.zeros((0, args.input, args.input, 3), np.uint8)
    split = np.array(["test" if lab["state"][i] == args.test_state else
                      ("val" if cell_val(lab["lat"][i], lab["lon"][i], args.val_frac) else "train") for i in idx])
    y = (lab["label"][idx] == "home").astype(np.int8)
    meta = {"arch": "resnet18", "mode": "point", "input": args.input, "point_m": args.point_m, "classes": ["public", "home"]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez(args.out, x=X, y=y, split=split, ref=lab["ref"][idx].astype(str), kind=lab["kind"][idx].astype(str),
             state=lab["state"][idx].astype(str), meta=json.dumps(meta))
    stats = {s: {"n": int((split == s).sum()), "home": int(y[split == s].sum())} for s in ("train", "val", "test")}
    log.info("context crops -> %s: %s", args.out, stats)


def cmd_eval_osm(args: argparse.Namespace) -> None:
    """Rules (and the CNN if given) against OSM-labelled courts of the scope state(s)."""
    states = [s.upper() for s in args.states]
    lab = osm_labelled_courts(args.context, args.courts, states, args.sites_scope)
    m = lab["label"] != ""
    lon, lat = lab["lon"][m], lab["lat"][m]
    truth = lab["label"][m]
    bbox = (float(lon.min()) - 0.05, float(lat.min()) - 0.05, float(lon.max()) + 0.05, float(lat.max()) + 0.05)
    ctx = Context.load(args.sites_scope, args.buildings, SITES_DIR, bbox)
    x, y = _tf("EPSG:4326", ALBERS).transform(lon, lat)
    p_home = np.full(len(lon), np.nan)
    if args.context_model:
        from .cropcls import CropClassifier

        clf = CropClassifier.load(args.context_model)
        px = int(round(float(clf.meta.get("point_m", 160.0)) / config.TARGET_GSD_M))
        imgs, ok = fetch_context_crops(lon, lat, lab["state"][m], px, args.naip_dir)
        p_home[ok] = clf.score_images(imgs[ok])
    pred = np.array([classify_point(point_features(ctx, float(x[i]), float(y[i])), float(p_home[i]))[0]
                     for i in range(len(lon))])
    flag = pred == "private_home"
    home = truth == "home"
    res = {"courts": int(m.sum()), "home": int(home.sum()), "public": int((~home).sum()),
           "context_model": str(args.context_model or ""),
           "pred_counts_home": dict(Counter(pred[home].tolist())), "pred_counts_public": dict(Counter(pred[~home].tolist())),
           "flag_home_precision": round(float((flag & home).sum() / max(1, flag.sum())), 4),
           "flag_home_recall": round(float((flag & home).sum() / max(1, home.sum())), 4),
           "public_on_home_courts": int(((pred == "public") & home).sum()),
           "note": "labels use OSM landuse/site polygons, which the rules also use: optimistic for rules 2-4"}
    write_json(args.out, res)
    log.info("eval-osm: %s", json.dumps(res))


# =========================================================================== CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("classify")
    p.add_argument("--candidates", type=Path, required=True, help="scan_engine merge detections.parquet")
    p.add_argument("--scope", default="TN", help="sites_<scope>.parquet to use (TN, us, ...)")
    p.add_argument("--sites-dir", type=Path, default=SITES_DIR)
    p.add_argument("--buildings", type=Path, nargs="*", default=[])
    p.add_argument("--context-model", type=Path)
    p.add_argument("--context-min-conf", type=float, default=0.25)
    p.add_argument("--rescore", type=Path, help="hoop_pipeline.rescore output: use its p_home (no NAIP fetch) and carry "
                                                 "conf2 / verify into the outputs")
    p.add_argument("--padus-inset-m", type=float, default=20.0)
    p.add_argument("--naip-dir", type=Path, default=config.NAIP_GEOPARQUET_DIR)
    p.add_argument("--region-label", default="tennessee")
    p.add_argument("--model-name", default="best_v2.pt")
    p.add_argument("--out", type=Path)
    add_common_args(p)
    p = sub.add_parser("classify-osm", help="privacy class of a state's OSM courts (for export_app --osm-privacy)")
    p.add_argument("--osm", type=Path, default=config.OSM_US_DIR / "courts_TN.geojson")
    p.add_argument("--scope", default="TN")
    p.add_argument("--sites-dir", type=Path, default=SITES_DIR)
    p.add_argument("--buildings", type=Path, nargs="*", default=[])
    p.add_argument("--context-model", type=Path)
    p.add_argument("--padus-inset-m", type=float, default=20.0)
    p.add_argument("--naip-dir", type=Path, default=config.NAIP_GEOPARQUET_DIR)
    p.add_argument("--out", type=Path, default=config.OUT_DIR / "osm_privacy_TN.json")
    add_common_args(p)
    p = sub.add_parser("context-data")
    p.add_argument("--context", type=Path, default=config.OSM_US_DIR / "context_us.parquet")
    p.add_argument("--courts", type=Path, default=config.OSM_US_DIR / "courts_us.parquet")
    p.add_argument("--sites-scope", default="us")
    p.add_argument("--max-per-class", type=int, default=20000)
    p.add_argument("--point-m", type=float, default=160.0)
    p.add_argument("--input", type=int, default=224)
    p.add_argument("--val-frac", type=float, default=0.1)
    p.add_argument("--test-state", default="TN")
    p.add_argument("--workers", type=int, default=96)
    p.add_argument("--naip-dir", type=Path, default=config.NAIP_GEOPARQUET_DIR)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path, default=config.DATA_DIR / "crops" / "context.npz")
    add_common_args(p)
    p = sub.add_parser("eval-osm")
    p.add_argument("--states", nargs="+", default=["TN"])
    p.add_argument("--sites-scope", default="TN")
    p.add_argument("--buildings", type=Path, nargs="*", default=[])
    p.add_argument("--context", type=Path, default=config.OSM_US_DIR / "context_us.parquet")
    p.add_argument("--courts", type=Path, default=config.OSM_US_DIR / "courts_us.parquet")
    p.add_argument("--context-model", type=Path)
    p.add_argument("--naip-dir", type=Path, default=config.NAIP_GEOPARQUET_DIR)
    p.add_argument("--out", type=Path, default=config.OUT_DIR / "privacy_eval_osm.json")
    add_common_args(p)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    {"classify": cmd_classify, "classify-osm": cmd_classify_osm, "context-data": cmd_context_data,
     "eval-osm": cmd_eval_osm}[args.cmd](args)


if __name__ == "__main__":
    main()
