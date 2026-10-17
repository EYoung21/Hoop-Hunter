"""Indoor basketball courts layer (no imagery): OSM, Overture places, NCES schools, park department pages.

Indoor courts are invisible from above, so the satellite pipeline never finds them. This module merges
four open sources into one layer, dedupes them, and classifies access:

* **OSM** indoor basketball features (``data/osm_us_20261015/indoor_us.parquet``: sports centres, sports
  halls, fitness centres, indoor pitches tagged sport=basketball). ODbL.
* **Overture places** (``data/web/overture_places_us.parquet`` from ``websources overture-extract``):
  category basketball_court, community / civic centres, YMCAs, recreation centres, and gyms or sports clubs
  whose name points to a court (LA Fitness, Life Time, 24 Hour Fitness, JCC, Boys & Girls Club, fieldhouse,
  "basketball", ...). CDLA-Permissive-2.0 (Apache-2.0 for Foursquare rows, CC0 for some). A generic gym
  category alone is not taken as a court.
* **NCES** public and private schools and IPEDS colleges: kept as ``kind=school_gym`` /
  ``college_gym``, "school gym (likely, not open to the public)", ``access=school``. Never in the public
  default export. Public domain.
* **Park department pages** (``data/web/located_pages.parquet`` from ``websources build``): pages that
  state an indoor basketball court, a gym(nasium) with basketball, or open-gym basketball.

Commands (``python -m hoop_pipeline.indoor <cmd> --help``)::

    build    merge + dedupe -> out/indoor/indoor_courts.parquet, indoor_courts.geojson (public / membership /
             unknown, court listed or likely), indoor_courts_listed.geojson (listed only),
             indoor_school_gyms.geojson, summary.json
    sample   stratified sample for reading by eye; fetches the listed website politely (robots.txt, delay)
    score    precision of the labelled sample

Access classes (only when the data says so; otherwise ``unknown``):
``public`` OSM access=yes/permissive or operator:type public/government, a municipal recreation /
community centre name, or a park department page; ``membership`` OSM access=private/customers/members,
fee-based gyms and clubs (gym category, YMCA, JCC, Boys & Girls Club); ``school`` NCES / IPEDS / OSM
operator:type school|university; ``unknown`` otherwise.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from shapely.strtree import STRtree

from . import config
from .common import add_common_args, read_json, setup_logging, write_json
from .geoparquet import read_geoparquet, write_geoparquet

log = logging.getLogger("indoor")

OSM_INDOOR = config.DATA_DIR / "osm_us_20261015" / "indoor_us.parquet"
OSM_COURTS = config.DATA_DIR / "osm_us_20261015" / "courts_us.parquet"
OVERTURE_FILE = config.DATA_DIR / "web" / "overture_places_us.parquet"
LOCATED_PAGES = config.DATA_DIR / "web" / "located_pages.parquet"
NCES_DIR = config.DATA_DIR / "sites" / "nces"
CANDIDATES = config.OUT_DIR / "national" / "v3" / "candidates_us.parquet"
OUT_DIR = config.OUT_DIR / "indoor"
OUT_FILE = OUT_DIR / "indoor_courts.parquet"

APP_KEYS = ["id", "name", "source", "status", "confidence", "hoops", "surface", "lit", "address", "region", "osm_id",
            "updated"]
EXTRA_KEYS = ["site", "indoor", "state", "access", "kind", "likelihood", "provenance"]
ACCESS = ("public", "membership", "school", "unknown")
LIKELIHOOD = ("listed", "likely", "possible")       # listed: the source says basketball; likely: venue type

# --------------------------------------------------------------------------- name rules
# Names that point to a basketball court in a gym / club / centre category. 24 Hour Fitness only counts as a
# court for its Sport / Super-Sport clubs; LA Fitness and Life Time clubs usually have one.
CHAIN_COURT_RE = re.compile(r"\bla fitness\b|\blife ?time\b|\b24 hour fitness (super[- ]?)?sport|\bymca\b|\bywca\b|"
                            r"\bjcc\b|jewish community cent|boys (&|and) girls club|\bfieldhouse\b|\bfield house\b|"
                            r"sportsplex|basketball|\bhoops?\b|athletic club|sports (center|centre|club)|"
                            r"recreation (center|centre)|\brec (center|centre)\b|\bgymnasium\b|"
                            r"\bpolice athletic league\b|\bpal center\b", re.I)
NO_COURT_RE = re.compile(r"planet fitness|anytime fitness|orangetheory|orange theory|crossfit|\byoga\b|pilates|"
                         r"\bbarre\b|f45|snap fitness|curves|cycle|spin|martial|karate|taekwondo|jiu|boxing|"
                         r"gymnastics|dance|climbing|\bswim|aquatic|tennis|pickleball|golf|bowling|skate|"
                         r"ice (rink|arena)|soccer|volleyball|cheer|personal train|physical therapy|chiropract|"
                         r"senior (center|centre)|\bchurch\b|ministr|head ?start|child ?care|day ?care|preschool|"
                         r"early learning|\boffices?\b|headquarters|administrat|\bassociation\b|of america\b|"
                         r"thrift|\bstore\b|\bcamp\b|foundation|\bfund\b|resale|employment|housing|"
                         r"health (center|centre|clinic)|clinic|medical", re.I)
OUTDOOR_NAME_RE = re.compile(r"\bparks?\b|playground|\bcourts\b|\bsquare\b|\bfields?\b|\bplayfield\b|\boutdoor\b", re.I)
INDOOR_NAME_RE = re.compile(r"\bgym|gymnasium|cent(er|re)|arena|academy|fieldhouse|field house|pavilion|dome|"
                            r"\bsports\b|\bindoor\b|\bhall\b|\bclub\b|\bymca\b|\bcoliseum\b|\bcomplex\b", re.I)
MEMBERSHIP_RE = re.compile(r"\bla fitness\b|\blife ?time\b|\b24 hour fitness\b|\bymca\b|\bywca\b|\bjcc\b|"
                           r"jewish community cent|boys (&|and) girls club|athletic club|sports club|\bclub\b", re.I)
# Only names that say a government runs the place count as "public" (the data says so); a bare
# "community center" or "recreation center" stays "unknown".
MUNICIPAL_RE = re.compile(r"\bcity of\b|\btown of\b|\bvillage of\b|\btownship\b|\bcounty (parks?|rec)|\bpark district\b|"
                          r"\bparks (and|&) rec|\bparks department\b|\bmunicipal\b|\bpolice athletic league\b|"
                          r"\bpal\b", re.I)
SCHOOL_NAME_RE = re.compile(r"\b(high school|middle school|junior high|elementary|school|college|university|"
                            r"\bhs\b|isd)\b", re.I)
SCHOOL_LEVEL_RE = re.compile(r"\b(high|middle|junior|jr\.?|secondary|senior high|intermediate|academy|prep|"
                             r"preparatory|k-?12|college|university)\b", re.I)
ELEMENTARY_RE = re.compile(r"\b(elementary|elem\.?|primary|kindergarten|pre-?k|preschool|early (learning|"
                           r"childhood)|montessori|daycare|day care)\b", re.I)

OVERTURE_COURT_CATS = {"basketball_court"}
OVERTURE_CENTER_CATS = {"community_center", "civic_center", "recreation_center", "sports_complex"}
OVERTURE_CLUB_CATS = {"gym", "sport_or_fitness_facility", "health_and_wellness_club", "sport_or_recreation_club",
                      "youth_organization", "sports_and_recreation", "social_or_community_service"}
OVERTURE_ARENA_CATS = {"basketball_stadium"}


# --------------------------------------------------------------------------- helpers
def _tags(v: Any) -> dict[str, str]:
    if isinstance(v, dict):
        return v
    try:
        d = json.loads(v) if v else {}
        return d if isinstance(d, dict) else {}
    except (TypeError, ValueError):
        return {}


def osm_access(t: dict[str, str]) -> str:
    acc = (t.get("access") or "").lower()
    op = (t.get("operator:type") or "").lower()
    if op in ("school", "university", "college") or t.get("amenity") in ("school", "college", "university"):
        return "school"
    if acc in ("private", "customers", "members", "permit", "no"):
        return "membership"
    if acc in ("yes", "permissive", "public") or op in ("public", "government", "municipal"):
        return "public"
    return "unknown"


def overture_class(cat: str | None, name: str | None, brand: str | None) -> tuple[str, str, str] | None:
    """(kind, likelihood, access) of an Overture place, or None when nothing points to a basketball court."""
    nm = f"{name or ''} {brand or ''}"
    if cat in OVERTURE_COURT_CATS:
        if OUTDOOR_NAME_RE.search(nm) and not INDOOR_NAME_RE.search(nm):
            return None                      # "Tompkins Square Park", "Forest Park Courts": an outdoor court
        return "basketball_venue", "listed", "membership" if MEMBERSHIP_RE.search(nm) else "unknown"
    if cat in OVERTURE_ARENA_CATS:
        return "arena", "listed", "unknown"
    if NO_COURT_RE.search(nm) and not re.search(r"basketball|hoops?\b|fieldhouse|field house", nm, re.I):
        return None
    if cat in OVERTURE_CENTER_CATS:
        if re.search(r"basketball|\bgym\b|gymnasium|fieldhouse|field house|recreation|\brec\b|sportsplex|ymca|ywca|"
                     r"boys (&|and) girls", nm, re.I):
            acc = "membership" if MEMBERSHIP_RE.search(nm) else "public" if MUNICIPAL_RE.search(nm) else "unknown"
            return "rec_center", "likely", acc
        return "community_center", "possible", "public" if MUNICIPAL_RE.search(nm) else "unknown"
    if cat in OVERTURE_CLUB_CATS and CHAIN_COURT_RE.search(nm):
        acc = "membership" if (MEMBERSHIP_RE.search(nm) or cat in ("gym", "health_and_wellness_club")) else (
            "public" if MUNICIPAL_RE.search(nm) else "unknown")
        kind = "gym" if cat in ("gym", "sport_or_fitness_facility", "health_and_wellness_club") else "club"
        return kind, "likely", acc
    return None


def _by_prefix(c: Counter[str]) -> Counter[str]:
    out: Counter[str] = Counter()
    for k, v in c.items():
        out[k.split(":")[0]] += v
    return out


def school_likelihood(name: str, kind: str) -> str:
    if kind == "college_gym":
        return "likely"
    if ELEMENTARY_RE.search(name) and not SCHOOL_LEVEL_RE.search(name):
        return "possible"
    return "likely" if SCHOOL_LEVEL_RE.search(name) else "possible"


def name_core(s: str | None) -> set[str]:
    from .websources import name_core as _nc

    return set(_nc(s).split())


def name_sim(a: str | None, b: str | None) -> float:
    ca, cb = name_core(a), name_core(b)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / len(ca | cb)


# --------------------------------------------------------------------------- sources
def rows_osm() -> list[dict[str, Any]]:
    if not OSM_INDOOR.exists():
        log.warning("%s missing", OSM_INDOOR)
        return []
    t = pq.read_table(OSM_INDOOR, columns=["osm_type", "osm_id", "name", "tags", "center_lon", "center_lat",
                                           "indoor_kind", "osm_ref"]).to_pylist()
    out = []
    for r in t:
        tg = _tags(r["tags"])
        addr = " ".join(x for x in (tg.get("addr:housenumber"), tg.get("addr:street")) if x) or None
        if addr and tg.get("addr:city"):
            addr += f", {tg['addr:city']}"
        out.append({"src": "osm", "src_id": f"{r['osm_type']}/{r['osm_id']}", "name": r["name"] or tg.get("name"),
                    "lon": r["center_lon"], "lat": r["center_lat"], "kind": str(r["indoor_kind"] or "sports_centre"),
                    "likelihood": "listed", "access": osm_access(tg), "address": addr,
                    "evidence": f"OSM {r['indoor_kind']} sport={tg.get('sport')}", "url": None})
    return out


def rows_overture(min_conf: float) -> tuple[list[dict[str, Any]], Counter[str]]:
    if not OVERTURE_FILE.exists():
        log.warning("%s missing (run websources overture-extract)", OVERTURE_FILE)
        return [], Counter()
    t = pq.read_table(OVERTURE_FILE, columns=["id", "name", "cat", "confidence", "operating_status", "website",
                                              "address", "locality", "region", "postcode", "brand", "licenses",
                                              "lon", "lat"]).to_pylist()
    out: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    for r in t:
        if (r["operating_status"] or "") in ("permanently_closed", "temporarily_closed"):
            skipped["closed"] += 1
            continue
        if (r["confidence"] or 0) < min_conf:
            skipped["low_confidence"] += 1
            continue
        c = overture_class(r["cat"], r["name"], r["brand"])
        if c is None:
            skipped[f"no_court_signal:{r['cat']}"] += 1
            continue
        kind, lik, acc = c
        addr = ", ".join(x for x in (r["address"], r["locality"], r["region"], r["postcode"]) if x) or None
        out.append({"src": "overture", "src_id": r["id"], "name": r["name"], "lon": r["lon"], "lat": r["lat"],
                    "kind": kind, "likelihood": lik, "access": acc, "address": addr,
                    "evidence": f"Overture category {r['cat']}", "url": r["website"], "licenses": r["licenses"]})
    return out, skipped


def rows_nces() -> list[dict[str, Any]]:
    from .sites import read_nces

    out = []
    for key, kind in (("nces_public", "school_gym"), ("nces_private", "school_gym"), ("ipeds", "college_gym")):
        z = NCES_DIR / f"{key}.zip"
        if not z.exists():
            log.warning("%s missing", z)
            continue
        d = read_nces(z, key)
        for i in range(len(d["id"])):
            lon, lat = float(d["lon"][i]), float(d["lat"][i])
            if not (np.isfinite(lon) and np.isfinite(lat)):
                continue
            nm = str(d["name"][i]).title()
            out.append({"src": key, "src_id": str(d["id"][i]), "name": nm, "lon": lon, "lat": lat, "kind": kind,
                        "likelihood": school_likelihood(nm, kind), "access": "school", "address": None,
                        "evidence": "school gym (likely, not open to the public)", "url": None})
    return out


def rows_web() -> list[dict[str, Any]]:
    if not LOCATED_PAGES.exists():
        log.warning("%s missing (run websources build)", LOCATED_PAGES)
        return []
    t = pq.read_table(LOCATED_PAGES).to_pylist()
    out = []
    seen: set[str] = set()
    for r in t:
        io = r["indoor_outdoor"]
        if r["from_table"] or io not in ("indoor", "both", "indoor_likely"):
            continue
        key = f"{r['park_key']}|{r['url']}"
        if key in seen:
            continue
        seen.add(key)
        # a government or park-district site (".gov", ".us", "...parks.org", "parkdistrict", "recreation")
        gov = bool(re.search(r"\.(gov|us)$|parks|parkdistrict|recreation", r["host"] or "", re.I))
        out.append({"src": "web", "src_id": r["url"], "name": r["park_name"] or r["page_name"], "lon": r["lon"],
                    "lat": r["lat"], "kind": "rec_center", "likelihood": "listed" if io in ("indoor", "both") or
                    r["open_gym"] else "likely", "access": "public" if gov else "unknown", "address": None,
                    "evidence": r["snippet"], "url": r["url"], "fetched_at": r["fetched_at"]})
    return out


# --------------------------------------------------------------------------- dedupe
SRC_RANK = {"osm": 0, "web": 1, "overture": 2, "ipeds": 3, "nces_public": 4, "nces_private": 4}
LIK_RANK = {"listed": 0, "likely": 1, "possible": 2}


def dedupe(rows: list[dict[str, Any]], radius_m: float, near_m: float) -> list[list[int]]:
    """Union-find clusters: within ``near_m`` when either name is missing or names share a token; within
    ``radius_m`` when names agree (similarity >= 0.5). A school only merges with a same-named feature."""
    from .websources import pts_m

    n = len(rows)
    if not n:
        return []
    pts = pts_m([r["lon"] for r in rows], [r["lat"] for r in rows])
    tree = STRtree(pts)
    a, b = tree.query(pts, predicate="dwithin", distance=radius_m)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in zip(a.tolist(), b.tolist()):
        if i >= j:
            continue
        ri, rj = rows[i], rows[j]
        sim = name_sim(ri["name"], rj["name"])
        school = ri["access"] == "school" or rj["access"] == "school"
        d = float(shapely.distance(pts[i], pts[j]))
        if school and (ri["access"] != rj["access"]):
            # a school only absorbs a place with the same name ("Sanibel Recreation Center" is not "The Sanibel
            # School", though both reduce to "sanibel"); names that say recreation / community / YMCA never merge
            ok = sim >= 1.0 and not re.search(r"recreation|\brec\b|community|ymca|ywca|park\b|club",
                                              f"{ri['name']} {rj['name']}", re.I)
        elif ri["src"] == rj["src"] and ri["src"] in ("nces_public", "nces_private", "ipeds"):
            ok = sim >= 0.8 and d <= near_m
        else:
            ok = sim >= 0.5 or (d <= near_m and (sim > 0 or not ri["name"] or not rj["name"]))
        if ok:
            parent[find(i)] = find(j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def merge_group(rows: list[dict[str, Any]], idx: list[int]) -> dict[str, Any]:
    g = sorted((rows[i] for i in idx), key=lambda r: (SRC_RANK.get(r["src"], 9), LIK_RANK[r["likelihood"]]))
    head = g[0]
    accs = {r["access"] for r in g}
    access = "school" if "school" in accs else "membership" if "membership" in accs and "public" not in accs else (
        "public" if "public" in accs and "membership" not in accs else (
            next((r["access"] for r in g if r["src"] in ("osm", "web") and r["access"] != "unknown"), "unknown")))
    lik = min((r["likelihood"] for r in g), key=lambda v: LIK_RANK[v])
    kinds = [r["kind"] for r in g]
    kind = next((k for k in kinds if k not in ("school_gym", "college_gym")), kinds[0]) if access != "school" else (
        "college_gym" if "college_gym" in kinds else "school_gym")
    name = next((r["name"] for r in g if r["name"]), None)
    srcs = sorted({r["src"] for r in g}, key=lambda s: SRC_RANK.get(s, 9))
    return {
        "name": name, "lon": float(np.mean([r["lon"] for r in g if r["src"] == head["src"]])),
        "lat": float(np.mean([r["lat"] for r in g if r["src"] == head["src"]])), "kind": kind, "likelihood": lik,
        "access": access, "address": next((r["address"] for r in g if r["address"]), None),
        "sources": ";".join(srcs), "primary_source": head["src"], "n_sources": len(srcs), "n_records": len(g),
        "source_ids": ";".join(f"{r['src']}:{r['src_id']}" for r in g)[:4000],
        "url": next((r["url"] for r in g if r["src"] == "web" and r["url"]), None) or next(
            (r["url"] for r in g if r["url"]), None),
        "evidence": " | ".join(dict.fromkeys(str(r["evidence"]) for r in g if r["evidence"]))[:300],
        "osm_ref": next((r["src_id"] for r in g if r["src"] == "osm"), None),
    }


def status_of(m: dict[str, Any]) -> str:
    srcs = set(m["sources"].split(";"))
    if "osm" in srcs or ("web" in srcs and m["likelihood"] == "listed"):
        return "verified"
    return "candidate"


# --------------------------------------------------------------------------- build
APP_OSM_DEFAULT = str(config.OUT_DIR / "national" / "v3" / "app_ready" / "*.geojson")


def app_osm_refs(pattern: str) -> set[str]:
    """OSM refs ("way/123") of the OSM features in the app's national layer (its app_ready GeoJSON files)."""
    import glob

    refs: set[str] = set()
    paths = sorted(glob.glob(pattern)) if pattern else []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for f in json.load(fh).get("features", []):
                pr = f.get("properties") or {}
                if pr.get("source") == "osm" and pr.get("osm_id"):
                    refs.add(str(pr["osm_id"]))
    log.info("app OSM layer: %d features from %d files (%s)", len(refs), len(paths), pattern)
    return refs


def cmd_build(args: argparse.Namespace) -> None:
    from .states import load_states

    osm = rows_osm()
    ovt, skipped = rows_overture(args.min_confidence)
    nces = rows_nces() if not args.no_schools else []
    web = rows_web()
    rows = [r for r in osm + web + ovt + nces if r["lon"] is not None and r["lat"] is not None]
    log.info("inputs: osm %d, web %d, overture %d (skipped %d: %s), nces/ipeds %d", len(osm), len(web), len(ovt),
             sum(skipped.values()), dict(_by_prefix(skipped)), len(nces))
    states = load_states()
    st = states.assign(np.array([r["lon"] for r in rows]), np.array([r["lat"] for r in rows]))
    rows = [r for r, s in zip(rows, st) if s]
    # Overture basketball_court places at an outdoor court (OSM outdoor court or satellite candidate within 40 m)
    rows = flag_outdoor(rows, args.outdoor_radius)
    groups = dedupe(rows, args.radius, args.near)
    merged = [merge_group(rows, g) for g in groups]
    for m in merged:                      # "High school gym", "Pauley Pavilion (UCLA)": the name says school
        if m["access"] == "unknown" and SCHOOL_NAME_RE.search(m["name"] or ""):
            m["access"] = "school"
    st = states.assign(np.array([m["lon"] for m in merged]), np.array([m["lat"] for m in merged]))
    region = {c: n.lower().replace(" ", "_") for c, n in zip(states.codes, states.names)}
    today = dt.date.today().isoformat()
    for m, s in zip(merged, st):
        m["state"] = s
        m["region"] = region.get(s, s.lower())
        m["status"] = status_of(m)
        ids_by_src = dict(reversed([x.split(":", 1) for x in m["source_ids"].split(";") if ":" in x]))
        key = next((f"{s_}:{ids_by_src[s_]}" for s_ in ("osm", "overture", "ipeds", "nces_public", "nces_private",
                                                          "web") if s_ in ids_by_src), m["source_ids"])  # stable id
        m["id"] = "indoor-" + hashlib.sha1(key.encode()).hexdigest()[:12]
        m["site"] = {"school_gym": "school", "college_gym": "college"}.get(m["kind"], "rec")
        m["updated"] = today
    merged.sort(key=lambda m: (m["id"], m["lon"], m["lat"]))
    seen_ids: Counter[str] = Counter()
    for m in merged:                      # one page placed at two parks: same key, different place
        seen_ids[m["id"]] += 1
        if seen_ids[m["id"]] > 1:
            m["id"] += f"-{seen_ids[m['id']]}"
    # OSM features the app already shows: the national OSM layer exports every basketball feature, indoor ones
    # included, without an indoor flag. They stay out of the GeoJSON (no duplicate pin) and are listed in
    # osm_indoor_flag_fixes.json so the existing rows can get indoor = true instead.
    app_osm = app_osm_refs(args.app_osm)
    for m in merged:
        m["in_app_osm"] = bool(m["osm_ref"]) and m["osm_ref"] in app_osm
    cols = ["id", "name", "kind", "likelihood", "access", "status", "state", "region", "site", "address",
            "primary_source", "sources", "n_sources", "n_records", "source_ids", "osm_ref", "in_app_osm", "url",
            "evidence", "lon", "lat", "updated"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    table = {c: pa.array([m.get(c) for m in merged]) for c in cols}
    table["indoor"] = pa.array([True] * len(merged))
    write_geoparquet(OUT_FILE, table, shapely.points([m["lon"] for m in merged], [m["lat"] for m in merged]))
    # GeoJSON in the app schema (+ site, indoor, access, kind, likelihood, provenance)
    default = [m for m in merged if m["access"] != "school" and m["likelihood"] in ("listed", "likely")
               and m["kind"] != "arena" and not m["in_app_osm"]]
    schools = [m for m in merged if m["access"] == "school" and not m["in_app_osm"]]
    fixes = [{"app_id": "osm-" + m["osm_ref"].replace("/", "-"), "osm_id": m["osm_ref"], "name": m["name"],
              "state": m["state"], "indoor_place": m["id"], "kind": m["kind"], "access": m["access"],
              "likelihood": m["likelihood"]} for m in merged if m["in_app_osm"]]
    write_json(OUT_DIR / "osm_indoor_flag_fixes.json",
               {"about": "OSM basketball features that the national app layer already shows with indoor = false, but "
                         "that OSM tags as indoor (sports centre / hall, indoor pitch, ...). Set indoor = true on these "
                         "app ids rather than loading them again from the indoor layer.",
                "app_layer": args.app_osm, "count": len(fixes), "features": fixes})
    listed = [m for m in default if m["likelihood"] == "listed"]     # a source says there is a court
    for name, feats in (("indoor_courts.geojson", default), ("indoor_courts_listed.geojson", listed),
                        ("indoor_school_gyms.geojson", schools)):
        fc = {"type": "FeatureCollection", "features": [feature(m) for m in feats]}
        errs = validate(fc)
        if errs:
            raise SystemExit(f"{name}: {len(errs)} schema errors, e.g. {errs[:3]}")
        write_json(OUT_DIR / name, fc, indent=None)
    summary = summarise(merged, rows, skipped, default, schools, args)
    write_json(OUT_DIR / "summary.json", summary)
    log.info("indoor: %d records -> %d places (%d default GeoJSON, %d school gyms) -> %s", len(rows), len(merged),
             len(default), len(schools), OUT_DIR)
    print(json.dumps(summary["by_source_access"], indent=1))


def flag_outdoor(rows: list[dict[str, Any]], radius: float) -> list[dict[str, Any]]:
    """Drop Overture basketball_court rows (category only) that sit on an outdoor court."""
    from .websources import pts_m

    sel = [i for i, r in enumerate(rows) if r["src"] == "overture" and r["kind"] == "basketball_venue"]
    if not sel:
        return rows
    targets = []
    if OSM_COURTS.exists():
        o = pq.read_table(OSM_COURTS, columns=["center_lon", "center_lat", "outdoor"]).to_pydict()
        keep = [bool(x) for x in o["outdoor"]]
        targets.append(pts_m(np.asarray(o["center_lon"])[keep], np.asarray(o["center_lat"])[keep]))
    if CANDIDATES.exists():
        c = pq.read_table(CANDIDATES, columns=["lon", "lat", "conf"]).to_pydict()
        sel_c = np.asarray(c["conf"], float) >= 0.3
        targets.append(pts_m(np.asarray(c["lon"])[sel_c], np.asarray(c["lat"])[sel_c]))
    if not targets:
        return rows
    tree = STRtree(np.concatenate(targets))
    p = pts_m([rows[i]["lon"] for i in sel], [rows[i]["lat"] for i in sel])
    hit = set(np.asarray(sel)[np.unique(tree.query(p, predicate="dwithin", distance=radius)[0])].tolist())
    log.info("Overture basketball_court places on an outdoor court (OSM / candidate within %.0f m): %d of %d dropped",
             radius, len(hit), len(sel))
    return [r for i, r in enumerate(rows) if i not in hit]


def feature(m: dict[str, Any]) -> dict[str, Any]:
    src = {"nces_public": "nces", "nces_private": "nces", "ipeds": "nces"}.get(m["primary_source"], m["primary_source"])
    props: dict[str, Any] = {
        "id": m["id"], "name": m["name"], "source": src, "status": m["status"], "confidence": None, "hoops": None,
        "surface": None, "lit": None, "address": m["address"], "region": m["region"], "osm_id": m["osm_ref"],
        "updated": m["updated"], "site": m["site"], "indoor": True, "state": m["state"], "access": m["access"],
        "kind": m["kind"], "likelihood": m["likelihood"], "provenance": m["source_ids"][:500],
    }
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(m["lon"], 6), round(m["lat"], 6)]},
            "properties": props}


def validate(fc: dict[str, Any]) -> list[str]:
    """App schema (export_app.PROPERTY_KEYS) plus the indoor keys, in order; types of the shared keys."""
    errs: list[str] = []
    ids: set[str] = set()
    for i, f in enumerate(fc.get("features", [])):
        p = f.get("properties") or {}
        if list(p) != APP_KEYS + EXTRA_KEYS:
            errs.append(f"[{i}] keys {list(p)}")
            continue
        c = (f.get("geometry") or {}).get("coordinates") or []
        if len(c) != 2 or not (-180 <= c[0] <= 180 and -90 <= c[1] <= 90):
            errs.append(f"[{i}] coordinates {c}")
        if not p["id"] or p["id"] in ids:
            errs.append(f"[{i}] id {p['id']!r} empty or duplicate")
        ids.add(p["id"])
        if p["status"] not in ("verified", "candidate") or p["indoor"] is not True or p["access"] not in ACCESS:
            errs.append(f"[{i}] status/indoor/access {p['status']} {p['indoor']} {p['access']}")
        if p["likelihood"] not in LIKELIHOOD or not isinstance(p["region"], str) or not p["region"]:
            errs.append(f"[{i}] likelihood/region")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(p["updated"])):
            errs.append(f"[{i}] updated")
        if p["state"] is not None and not re.fullmatch(r"[A-Z]{2}", str(p["state"])):
            errs.append(f"[{i}] state {p['state']!r}")
        for k in ("name", "address", "osm_id", "provenance"):
            if p[k] is not None and not isinstance(p[k], str):
                errs.append(f"[{i}] {k} type")
    return errs


def summarise(merged: list[dict[str, Any]], rows: list[dict[str, Any]], skipped: Counter[str],
              default: list[dict[str, Any]], schools: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    by_src_acc: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for m in merged:
        for s in m["sources"].split(";"):
            by_src_acc[s][m["access"]] += 1
    return {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "params": {"radius_m": args.radius, "near_m": args.near, "min_confidence": args.min_confidence,
                   "outdoor_radius_m": args.outdoor_radius},
        "input_records": dict(Counter(r["src"] for r in rows)),
        "overture_skipped": dict(_by_prefix(skipped)),
        "overture_skipped_by_cat": dict(Counter(skipped).most_common(25)),
        "places": len(merged),
        "by_access": dict(Counter(m["access"] for m in merged)),
        "by_kind": dict(Counter(m["kind"] for m in merged)),
        "by_likelihood": dict(Counter(m["likelihood"] for m in merged)),
        "by_status": dict(Counter(m["status"] for m in merged)),
        "by_source_access": {k: dict(v) for k, v in by_src_acc.items()},
        "multi_source_places": sum(m["n_sources"] > 1 for m in merged),
        "already_in_app_osm_layer": sum(bool(m.get("in_app_osm")) for m in merged),
        "default_geojson": {"features": len(default), "by_access": dict(Counter(m["access"] for m in default)),
                            "by_primary_source": dict(Counter(m["primary_source"] for m in default)),
                            "by_state": dict(Counter(m["state"] for m in default).most_common())},
        "listed_geojson": {"features": sum(m["likelihood"] == "listed" for m in default),
                           "by_access": dict(Counter(m["access"] for m in default if m["likelihood"] == "listed")),
                           "by_primary_source": dict(Counter(m["primary_source"] for m in default
                                                             if m["likelihood"] == "listed"))},
        "school_geojson": {"features": len(schools), "by_likelihood": dict(Counter(m["likelihood"] for m in schools))},
    }


# --------------------------------------------------------------------------- verification sample
def cmd_sample(args: argparse.Namespace) -> None:
    """Stratified sample (primary source x kind) of non-school places; fetches each place's own website (if any)
    through the polite fetcher and records the basketball mentions found, for reading by eye."""
    from .websources import PoliteFetcher, judge_page, load_cached, normalise_url, parse_html

    t, _ = read_geoparquet(OUT_FILE, with_geometry=False)
    rng = random.Random(args.seed)
    strata: dict[str, list[int]] = defaultdict(list)
    for i in range(len(t["id"])):
        if t["access"][i] == "school" and not args.include_schools:
            continue
        strata[f"{t['primary_source'][i]}/{t['kind'][i]}/{t['likelihood'][i]}"].append(i)
    picks: list[tuple[str, int]] = []
    for k, idx in sorted(strata.items()):
        rng.shuffle(idx)
        picks.extend((k, i) for i in idx[:args.per_stratum])
    fetcher = PoliteFetcher(delay=args.delay)
    out = []
    for k, i in picks:
        url = normalise_url(t["url"][i]) if t["url"][i] else None
        res: dict[str, Any] = {}
        if url and args.fetch:
            rec = fetcher.fetch(url)
            html = load_cached(rec) if rec.get("outcome") == "ok" else None
            res["fetch"] = rec.get("outcome")
            if html:
                pp = parse_html(html, url)
                j = judge_page(pp.lines, pp.title)
                res.update(title=pp.title[:120], has_court=j.has_court, io=j.indoor_outdoor, open_gym=j.open_gym,
                           snippet=j.snippet, mentions=j.kinds,
                           gym_words=sorted({w.lower() for w in re.findall(r"\b(gym(?:nasium)?|basketball|court)\b",
                                                                           " ".join(pp.lines), re.I)}))
        out.append({"id": t["id"][i], "stratum": k, "name": t["name"][i], "state": t["state"][i],
                    "access": t["access"][i], "sources": t["sources"][i], "evidence": t["evidence"][i],
                    "url": url, **res})
    OUT_DIR.joinpath("review").mkdir(parents=True, exist_ok=True)
    write_json(OUT_DIR / "review" / "sample.json", out)
    with open(OUT_DIR / "review" / "sample.txt", "w", encoding="utf-8") as fh:
        for o in out:
            fh.write(f"{o['id']} [{o['stratum']}] {o['name']} ({o['state']}) access={o['access']} src={o['sources']}\n"
                     f"    evidence: {o['evidence']}\n    url: {o['url']} fetch={o.get('fetch')} title={o.get('title')}\n"
                     f"    page: court={o.get('has_court')} io={o.get('io')} open_gym={o.get('open_gym')} "
                     f"words={o.get('gym_words')} snippet={o.get('snippet')}\n")
    log.info("sample: %d places from %d strata -> %s", len(out), len(strata), OUT_DIR / "review" / "sample.txt")


def cmd_score(args: argparse.Namespace) -> None:
    from .websources import _wilson

    sample = {o["id"]: o for o in read_json(OUT_DIR / "review" / "sample.json")}
    labels = read_json(Path(args.labels))
    by: dict[str, Counter[str]] = defaultdict(Counter)
    for pid, lab in labels.items():
        if pid not in sample:
            continue
        by["all"][lab] += 1
        st = sample[pid]["stratum"]
        by[st][lab] += 1
        by["likelihood:" + st.rsplit("/", 1)[1]][lab] += 1
        by["source:" + st.split("/", 1)[0]][lab] += 1
    res = {}
    for k, c in sorted(by.items()):
        n = c["C"] + c["N"]
        res[k] = {"C": c["C"], "N": c["N"], "U": c["U"], "precision": round(c["C"] / n, 3) if n else None,
                  "wilson95": _wilson(c["C"], n)}
        print(f"{k:45} C={c['C']:3d} N={c['N']:3d} U={c['U']:3d} precision={res[k]['precision']} CI={res[k]['wilson95']}")
    write_json(OUT_DIR / "review" / "scores.json", res)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m hoop_pipeline.indoor", description=__doc__.split("\n\n")[0])
    add_common_args(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build", help="merge + dedupe the indoor sources")
    p.add_argument("--radius", type=float, default=150.0, help="merge radius for same-named places (m)")
    p.add_argument("--near", type=float, default=40.0, help="merge radius for unnamed / loosely named places (m)")
    p.add_argument("--min-confidence", type=float, default=0.5, help="Overture place confidence")
    p.add_argument("--outdoor-radius", type=float, default=40.0)
    p.add_argument("--no-schools", action="store_true")
    p.add_argument("--app-osm", default=APP_OSM_DEFAULT,
                   help="glob of the app's national GeoJSON; its OSM features are not exported again ('' = none)")
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("sample", help="stratified sample to read by eye (fetches websites politely)")
    p.add_argument("--per-stratum", type=int, default=4)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--no-fetch", dest="fetch", action="store_false")
    p.add_argument("--include-schools", action="store_true")
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("score", help="precision of the labelled sample")
    p.add_argument("--labels", default=str(OUT_DIR / "review" / "labels.json"))
    p.set_defaults(func=cmd_score)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
