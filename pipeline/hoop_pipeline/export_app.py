"""Merge OSM courts, NYC Parks courts and detected candidates into the app's GeoJSON.

Writes ``app/assets/data/courts.geojson`` (a FeatureCollection of Points) in the
shared schema the Expo app reads:

    id, name, source, status, confidence, hoops, surface, lit, address,
    region, osm_id, updated

* OSM (target region): id ``osm-<type>-<id>``, status verified, point = element
  bbox center (what Overpass ``out center`` returns), sorted by element id. Courts in an
  indoor venue (sports centre / hall, gym, community centre, indoor pitch) gain the optional
  ``indoor: true``; a roofed outdoor court (only ``covered=yes``) stays outdoor.
* NYC Parks (``assets/NYCcourts.json``): id ``nyc-<Prop_ID>``; repeated Prop_IDs
  get ``-2``, ``-3`` ... in file order; rows without coordinates are skipped.
* Detected: ``source=detected, status=candidate`` with confidence, only at or
  above ``--min-confidence`` (default 0.5) and not within ``--known-radius`` of
  an OSM court. When the candidates carry a ``privacy`` class (privacy.py), only
  ``public`` ones are exported by default (``--include-residents-only`` and
  ``--include-unknown`` opt in); ``private_home`` and ``restricted`` never are.
  Exported candidates gain the optional ``site`` property (park, school, college,
  church, rec, community).
* ``--osm-privacy`` (privacy.py classify-osm) drops OSM courts that sit on a home lot,
  on restricted land (prisons, military) or in an apartment / HOA complex (kept with
  ``--include-residents-only``), and every court tagged access=private/no.
* Existing ``source=user`` features in the current file are carried over.
* ``updated`` keeps the previous value for features whose content is unchanged.

Everything is validated (schema, types, unique ids) before the atomic write.

Examples
--------
    python -m hoop_pipeline.export_app                       # osm + nyc + candidates >= 0.5
    python -m hoop_pipeline.export_app --no-candidates       # osm + nyc only
    python -m hoop_pipeline.export_app --dry-run             # validate and report, don't write
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import re
from pathlib import Path
from typing import Any

import numpy as np

from . import config
from .common import add_common_args, read_json, setup_logging, write_json
from .osm_tags import indoor_kind

log = logging.getLogger("export_app")

PROPERTY_KEYS = [
    "id", "name", "source", "status", "confidence", "hoops", "surface",
    "lit", "address", "region", "osm_id", "updated",
]
# Optional trailing keys: "site" on detected candidates (privacy.py), "notes" on user courts,
# "indoor" (always true when present) on OSM courts in an indoor venue.
SITE_VALUES = {"park", "school", "college", "church", "rec", "community"}
EXPORT_CLASSES_DEFAULT = {"public"}
NEVER_EXPORT = {"private_home", "restricted"}
SOURCES = {"osm", "nyc_parks", "detected", "user"}
STATUSES = {"verified", "candidate"}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------- builders
def _feature(lon: float, lat: float, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {k: props.get(k) for k in PROPERTY_KEYS},
    }


def _int_or_none(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _bool_or_none(v: Any) -> bool | None:
    return {"yes": True, "no": False}.get(str(v).strip().lower()) if v is not None else None


def osm_indoor(tags: dict[str, Any]) -> bool:
    """True for a court in an indoor venue. A pitch whose only indoor hint is ``covered=yes`` is a roofed
    outdoor court, so it stays outdoor."""
    if indoor_kind(tags) is None:
        return False
    roofed_only = (tags.get("leisure") == "pitch" and tags.get("covered") == "yes" and "building" not in tags
                   and tags.get("indoor") not in ("yes", "room", "area")
                   and tags.get("location") not in ("indoor", "underground"))
    return not roofed_only


def _label_for(lat: float, lon: float, default: str, sub_regions: list[config.Region]) -> str:
    for r in sub_regions:
        if r.south <= lat <= r.north and r.west <= lon <= r.east:
            return r.app_label
    return default


def osm_features(path: Path, region: str, updated: str,
                 sub_regions: list[config.Region] | None = None,
                 privacy: dict[str, str] | None = None, keep_residents: bool = False) -> list[dict[str, Any]]:
    """OSM courts from a fetch_osm region file or an osm_extract state file (same schema).
    Features inside a ``sub_regions`` bbox get that region's label instead of ``region``.
    With ``privacy`` (osm_ref -> class), home-lot / restricted courts and access=private|no
    courts off public sites are left out."""
    feats = read_json(path)["features"]
    if privacy is not None:
        kept = []
        dropped: dict[str, int] = {}
        for f in feats:
            p = f["properties"]
            cls = privacy.get(p["osm_ref"], "unknown")
            acc = str(p.get("tags", {}).get("access", "")).lower()
            residents = cls == "residents_only" and not keep_residents
            if cls in NEVER_EXPORT or residents or acc in ("private", "no"):
                dropped[cls] = dropped.get(cls, 0) + 1
                continue
            kept.append(f)
        log.info("OSM privacy: dropped %d of %d courts %s", len(feats) - len(kept), len(feats), dropped)
        feats = kept
    order = {"node": 0, "way": 1, "relation": 2}
    feats.sort(key=lambda f: (order[f["properties"]["osm_type"]], f["properties"]["osm_id"]))
    out = []
    for f in feats:
        p = f["properties"]
        t = p.get("tags", {})
        label = _label_for(p["center_lat"], p["center_lon"], region, sub_regions or [])
        feat = _feature(
            round(p["center_lon"], 6), round(p["center_lat"], 6),
            {
                "id": f"osm-{p['osm_type']}-{p['osm_id']}",
                "name": t.get("name"),
                "source": "osm",
                "status": "verified",
                "confidence": None,
                "hoops": _int_or_none(t.get("hoops")),
                "surface": t.get("surface"),
                "lit": _bool_or_none(t.get("lit")),
                "address": None,
                "region": label,
                "osm_id": f"{p['osm_type']}/{p['osm_id']}",
                "updated": updated,
            },
        )
        if osm_indoor(t):
            feat["properties"]["indoor"] = True
        out.append(feat)
    return out


def nyc_features(path: Path, updated: str) -> list[dict[str, Any]]:
    rows = read_json(path)
    seen: dict[str, int] = {}
    out = []
    skipped = 0
    for r in rows:
        if not r.get("lat") or not r.get("lon"):
            skipped += 1
            continue
        pid = str(r["Prop_ID"])
        seen[pid] = seen.get(pid, 0) + 1
        fid = f"nyc-{pid}" if seen[pid] == 1 else f"nyc-{pid}-{seen[pid]}"
        out.append(_feature(
            float(r["lon"]), float(r["lat"]),
            {
                "id": fid,
                "name": r.get("Name"),
                "source": "nyc_parks",
                "status": "verified",
                "confidence": None,
                "hoops": None,
                "surface": None,
                "lit": None,
                "address": r.get("Location"),
                "region": "nyc",
                "osm_id": None,
                "updated": updated,
            },
        ))
    log.info("NYC: %d rows, %d without coordinates skipped, %d features", len(rows), skipped, len(out))
    return out


def detected_features(
    path: Path, min_conf: float, default_region: str, updated: str,
    known: list[dict[str, Any]], known_radius_m: float, require_on_site: bool = False,
    offsite_min_box_m: float | None = None, export_classes: set[str] | None = None,
    sub_regions: list[config.Region] | None = None, min_verify: float | None = None,
) -> list[dict[str, Any]]:
    """Candidates from a scan GeoJSON. v2 files carry ``privacy``/``site`` (privacy.py): only
    ``export_classes`` (default {"public"}) are kept and ``site`` is exported; v1 files (no privacy
    class) fall back to the on_site / off-site box-size rules."""
    data = read_json(path)
    out = []
    classes = EXPORT_CLASSES_DEFAULT if export_classes is None else set(export_classes) - NEVER_EXPORT
    below = near = off_site = 0
    by_privacy: dict[str, int] = {}
    known_pts = np.array([(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0]) for f in known]) \
        if known else np.zeros((0, 2))
    for f in sorted(data["features"], key=lambda f: -float(f["properties"].get("confidence") or 0)):
        p = f["properties"]
        conf = float(p.get("confidence") or 0.0)
        if conf < min_conf:
            below += 1
            continue
        if min_verify is not None and p.get("verify") is not None and float(p["verify"]) < min_verify:
            below += 1
            continue
        cls = p.get("privacy")
        if cls is not None:
            if cls not in classes:
                by_privacy[cls] = by_privacy.get(cls, 0) + 1
                continue
        elif p.get("on_site") is False and (
            require_on_site or (offsite_min_box_m is not None and max(p.get("box_m") or [0]) < offsite_min_box_m)
        ):
            off_site += 1  # likely a neighbouring (residential) lot: backyard courts are private, never shown
            continue
        lon, lat = f["geometry"]["coordinates"]
        if len(known_pts):
            dlat = np.radians(known_pts[:, 0] - lat)
            dlon = np.radians(known_pts[:, 1] - lon)
            a = np.sin(dlat / 2) ** 2 + np.cos(np.radians(lat)) * np.cos(np.radians(known_pts[:, 0])) * np.sin(dlon / 2) ** 2
            if float(2 * 6_371_008.8 * np.arcsin(np.sqrt(np.minimum(1.0, a))).min()) <= known_radius_m:
                near += 1
                continue
        props = {
            "id": p["id"],
            "name": None,
            "source": "detected",
            "status": "candidate",
            "confidence": round(conf, 3),
            "hoops": None,
            "surface": None,
            "lit": None,
            "address": None,
            "region": _label_for(lat, lon, p.get("region") or default_region, sub_regions or []),
            "osm_id": None,
            "updated": p.get("detected") or updated,
        }
        feat = _feature(round(lon, 6), round(lat, 6), props)
        if p.get("site") in SITE_VALUES:
            feat["properties"]["site"] = p["site"]
        out.append(feat)
    log.info("candidates: %d in file, %d below %.2f, %d off-site (v1 rule), %d by privacy class %s, %d near known "
             "courts, %d exported", len(data["features"]), below, min_conf, off_site, sum(by_privacy.values()),
             by_privacy, near, len(out))
    return out


# --------------------------------------------------------------------------- validation
def validate(fc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if fc.get("type") != "FeatureCollection" or not isinstance(fc.get("features"), list):
        return ["not a FeatureCollection"]
    ids: set[str] = set()
    for i, f in enumerate(fc["features"]):
        where = f"feature[{i}]"
        if f.get("type") != "Feature":
            errors.append(f"{where}: type != Feature")
        g = f.get("geometry") or {}
        c = g.get("coordinates")
        if g.get("type") != "Point" or not isinstance(c, list) or len(c) != 2:
            errors.append(f"{where}: geometry must be a 2D Point")
        else:
            lon, lat = c
            finite = all(isinstance(v, (int, float)) and math.isfinite(v) for v in c)
            if not finite or not (-180 <= lon <= 180 and -90 <= lat <= 90):
                errors.append(f"{where}: bad coordinates {c}")
        p = f.get("properties") or {}
        keys = list(p.keys())
        # user courts may carry the app-only optional "notes" field; detected candidates an optional "site";
        # OSM courts in an indoor venue "indoor": true
        if keys != PROPERTY_KEYS and not (p.get("source") == "user" and keys == [*PROPERTY_KEYS, "notes"]) \
                and not (p.get("source") == "detected" and keys == [*PROPERTY_KEYS, "site"]) \
                and not (p.get("source") == "osm" and keys == [*PROPERTY_KEYS, "indoor"] and p["indoor"] is True):
            errors.append(f"{where}: property keys {keys} != {PROPERTY_KEYS}")
            continue
        if "site" in p and p["site"] not in SITE_VALUES:
            errors.append(f"{where}: site {p['site']!r} not in {sorted(SITE_VALUES)}")
        if "notes" in p and p["notes"] is not None and not isinstance(p["notes"], str):
            errors.append(f"{where}: notes must be string or null")
        where = f"{where} ({p['id']})"
        if not isinstance(p["id"], str) or not p["id"]:
            errors.append(f"{where}: id must be a non-empty string")
        elif p["id"] in ids:
            errors.append(f"{where}: duplicate id")
        else:
            ids.add(p["id"])
        if p["source"] not in SOURCES:
            errors.append(f"{where}: source {p['source']!r}")
        if p["status"] not in STATUSES:
            errors.append(f"{where}: status {p['status']!r}")
        if p["source"] == "detected":
            if p["status"] != "candidate" or not isinstance(p["confidence"], (int, float)) or not 0 <= p["confidence"] <= 1:
                errors.append(f"{where}: detected features need status=candidate and confidence in [0,1]")
        elif p["confidence"] is not None and not (isinstance(p["confidence"], (int, float)) and 0 <= p["confidence"] <= 1):
            errors.append(f"{where}: confidence must be null or in [0,1]")
        for key in ("name", "surface", "address", "osm_id"):
            if p[key] is not None and not isinstance(p[key], str):
                errors.append(f"{where}: {key} must be string or null")
        if p["hoops"] is not None and (not isinstance(p["hoops"], int) or isinstance(p["hoops"], bool) or p["hoops"] < 0):
            errors.append(f"{where}: hoops must be a non-negative int or null")
        if p["lit"] is not None and not isinstance(p["lit"], bool):
            errors.append(f"{where}: lit must be bool or null")
        if not isinstance(p["region"], str) or not p["region"]:
            errors.append(f"{where}: region must be a non-empty string")
        if not isinstance(p["updated"], str) or not ISO_DATE.match(p["updated"]):
            errors.append(f"{where}: updated must be YYYY-MM-DD")
        if p["source"] == "osm" and not (isinstance(p["osm_id"], str) and "/" in p["osm_id"]):
            errors.append(f"{where}: osm features need osm_id like 'way/123'")
    return errors


def reuse_candidate_ids(new: list[dict[str, Any]], old_path: Path, radius_m: float = 15.0) -> int:
    """Give a new detected candidate the id of a previous candidate within ``radius_m``, so review decisions
    stored on phones (keyed by id) survive a re-scan. Each old id is reused at most once."""
    if not old_path.exists():
        return 0
    try:
        old = [f for f in read_json(old_path)["features"] if f.get("properties", {}).get("source") == "detected"]
    except (OSError, ValueError, KeyError):
        return 0
    if not old:
        return 0
    olat = np.array([f["geometry"]["coordinates"][1] for f in old])
    olon = np.array([f["geometry"]["coordinates"][0] for f in old])
    oid = [f["properties"]["id"] for f in old]
    used: set[str] = set()
    taken = {f["properties"]["id"] for f in new}
    n = 0
    for f in sorted(new, key=lambda f: -(f["properties"].get("confidence") or 0)):
        if f["properties"].get("source") != "detected":
            continue
        lon, lat = f["geometry"]["coordinates"]
        d = np.hypot((olat - lat) * 111_320.0, (olon - lon) * 111_320.0 * np.cos(np.radians(lat)))
        for k in np.argsort(d)[:3]:
            if d[k] > radius_m:
                break
            if oid[k] not in used and (oid[k] == f["properties"]["id"] or oid[k] not in taken):
                used.add(oid[k])
                if oid[k] != f["properties"]["id"]:
                    taken.discard(f["properties"]["id"])
                    f["properties"]["id"] = oid[k]
                    taken.add(oid[k])
                n += 1
                break
    return n


def carry_forward_dates(new: list[dict[str, Any]], old_path: Path) -> int:
    """Keep the old 'updated' for features whose geometry and other properties are unchanged."""
    if not old_path.exists():
        return 0
    try:
        old = {f["properties"]["id"]: f for f in read_json(old_path)["features"]}
    except (OSError, ValueError, KeyError):
        return 0
    kept = 0
    for f in new:
        o = old.get(f["properties"]["id"])
        if not o:
            continue
        a = {k: v for k, v in f["properties"].items() if k != "updated"}
        b = {k: v for k, v in o.get("properties", {}).items() if k != "updated"}
        if a == b and f["geometry"] == o.get("geometry") and o["properties"].get("updated"):
            f["properties"]["updated"] = o["properties"]["updated"]
            kept += 1
    return kept


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="chattanooga", help="target region: picks data/osm/<region>.geojson")
    ap.add_argument("--region-label", help="'region' value written to the app (default: the region's app_region or name)")
    ap.add_argument("--osm", type=Path,
                    help="fetch_osm GeoJSON or osm_extract courts_<ST>.geojson (default data/osm/<region>.geojson)")
    ap.add_argument("--sub-region", nargs="*", default=[],
                    help="regions.json names whose bbox keeps its own label inside a bigger export "
                         "(e.g. --osm data/osm_us/courts_TN.geojson --region-label tennessee --sub-region chattanooga)")
    ap.add_argument("--max-mb", type=float, default=3.0, help="warn if the written file is bigger than this")
    ap.add_argument("--nyc", type=Path, default=config.NYC_COURTS_FILE)
    ap.add_argument("--candidates", type=Path, default=config.CANDIDATES_FILE)
    ap.add_argument("--no-candidates", action="store_true", help="export OSM + NYC (+ user) only")
    ap.add_argument("--min-confidence", type=float, default=0.5)
    ap.add_argument("--require-on-site", action="store_true",
                    help="drop candidates scan_state marked on_site=false (outside the park/school/... itself)")
    ap.add_argument("--offsite-min-box-m", type=float,
                    help="drop off-site candidates whose box is shorter than this (small off-site slabs are mostly "
                         "backyard courts); 22 was used for Tennessee")
    ap.add_argument("--include-residents-only", action="store_true",
                    help="also export candidates classified residents_only (apartment/HOA courts)")
    ap.add_argument("--include-unknown", action="store_true",
                    help="also export candidates with no public-site or residential evidence")
    ap.add_argument("--min-verify", type=float, help="drop candidates whose verifier score is below this")
    ap.add_argument("--osm-privacy", type=Path,
                    help="JSON {osm_ref: privacy class} from privacy.py; drops home-lot/restricted OSM courts")
    ap.add_argument("--known-radius", type=float, default=config.KNOWN_COURT_RADIUS_M)
    ap.add_argument("--out", type=Path, default=config.APP_COURTS_FILE)
    ap.add_argument("--updated", default=dt.date.today().isoformat(), help="ISO date for new/changed features")
    ap.add_argument("--drop-user", action="store_true", help="don't carry over source=user features")
    ap.add_argument("--no-reuse-ids", action="store_true",
                    help="don't give new candidates the id of a previous candidate within 15 m")
    ap.add_argument("--dry-run", action="store_true")
    add_common_args(ap)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)

    osm_file = args.osm or config.osm_path(args.region)
    if not osm_file.exists():
        raise SystemExit(f"{osm_file} missing; run: python -m hoop_pipeline.fetch_osm --regions {args.region}")
    known_regions = config.all_regions()
    # e.g. --region hamilton_county still labels its courts "chattanooga" (regions.json app_region)
    label = args.region_label or (known_regions[args.region].app_label if args.region in known_regions else args.region)
    subs = config.resolve_regions(args.sub_region) if args.sub_region else []
    osm_priv = read_json(args.osm_privacy) if args.osm_privacy else None
    osm = osm_features(osm_file, label, args.updated, subs, osm_priv, args.include_residents_only)
    log.info("OSM %s: %d features (region label %r%s)", osm_file.name, len(osm), label,
             f", sub-regions {[r.name for r in subs]}" if subs else "")
    nyc = nyc_features(args.nyc, args.updated)

    detected: list[dict[str, Any]] = []
    if not args.no_candidates:
        if args.candidates.exists():
            classes = set(EXPORT_CLASSES_DEFAULT)
            if args.include_residents_only:
                classes.add("residents_only")
            if args.include_unknown:
                classes.add("unknown")
            detected = detected_features(args.candidates, args.min_confidence, label, args.updated,
                                         osm, args.known_radius, args.require_on_site, args.offsite_min_box_m,
                                         classes, subs, args.min_verify)
        else:
            log.info("no candidates file at %s; exporting without detections", args.candidates)

    user: list[dict[str, Any]] = []
    if not args.drop_user and args.out.exists():
        try:
            user = [f for f in read_json(args.out)["features"] if f.get("properties", {}).get("source") == "user"]
        except (OSError, ValueError, KeyError):
            user = []
        if user:
            log.info("carrying over %d user-submitted features", len(user))

    reused = reuse_candidate_ids(detected, args.out) if not args.no_reuse_ids else 0
    if reused:
        log.info("reused %d previous candidate ids (within 15 m) so app review decisions carry over", reused)
    features = osm + nyc + detected + user
    kept_dates = carry_forward_dates(features, args.out)
    fc = {"type": "FeatureCollection", "features": features}
    errors = validate(fc)
    if errors:
        for e in errors[:50]:
            log.error(e)
        raise SystemExit(f"validation failed with {len(errors)} error(s); nothing written")

    counts: dict[str, int] = {}
    sites: dict[str, int] = {}
    for f in features:
        key = f"{f['properties']['source']}/{f['properties']['region']}"
        counts[key] = counts.get(key, 0) + 1
        if f["properties"]["source"] == "detected":
            st = f["properties"].get("site") or "(none)"
            sites[st] = sites.get(st, 0) + 1
    log.info("valid: %d features, %d unique ids, %s (%d kept previous 'updated'); detected by site %s",
             len(features), len({f['properties']['id'] for f in features}), counts, kept_dates, sites)
    if args.dry_run:
        log.info("dry run: not writing %s", args.out)
        return
    write_json(args.out, fc, indent=1)
    size_mb = args.out.stat().st_size / 1e6
    log.info("wrote %s (%.2f MB)", args.out, size_mb)
    if size_mb > args.max_mb:
        log.warning("%s is %.2f MB (> %.1f MB); consider a higher --min-confidence", args.out, size_mb, args.max_mb)


if __name__ == "__main__":
    main()
