"""Fetch basketball courts (and optional negative-sample locations) from OpenStreetMap.

Uses the Overpass API with polite defaults: big bboxes are split into small
tiles, every request carries a User-Agent, failures back off exponentially and
rotate between endpoints, and results are cached on disk (``data/osm``).

Examples
--------
    python -m hoop_pipeline.fetch_osm --regions chattanooga atlanta
    python -m hoop_pipeline.fetch_osm --regions training --negatives
    python -m hoop_pipeline.fetch_osm --bbox 34.98 -85.45 35.26 -85.05 --name chatt_custom

Output: ``data/osm/<region>.geojson``, one feature per OSM element. Geometry is
the court polygon when OSM has one (from ``out geom``), otherwise a point.
Properties carry the OSM tags, the element center and the polygon area.
OSM data is (c) OpenStreetMap contributors, ODbL 1.0.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import random
import time
from pathlib import Path
from typing import Any

import requests
from shapely.geometry import Polygon

from . import config
from .common import (
    LocalMeters,
    add_common_args,
    bbox_area_km2,
    feature_collection,
    read_json,
    setup_logging,
    split_bbox,
    write_json,
)

log = logging.getLogger("fetch_osm")

ATTRIBUTION = "(c) OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"


class OverpassError(RuntimeError):
    pass


# Endpoints that just failed are tried last for a while (e.g. a mirror that is
# down and answers HTTP 500 instantly), instead of first on every request.
_COOLDOWN_S = 300.0
_failed_at: dict[str, float] = {}


def _ordered(endpoints: list[str]) -> list[str]:
    now = time.monotonic()
    return sorted(endpoints, key=lambda ep: now - _failed_at.get(ep, -1e9) < _COOLDOWN_S)


# --------------------------------------------------------------------------- HTTP
def overpass_query(
    query: str,
    endpoints: list[str] | None = None,
    timeout_s: int = config.OVERPASS_TIMEOUT_S,
    max_retries: int = config.OVERPASS_MAX_RETRIES,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """POST an Overpass QL query, retrying with exponential backoff across endpoints."""
    endpoints = endpoints or config.OVERPASS_ENDPOINTS
    sess = session or requests.Session()
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    last_err: str = "no attempt made"
    for attempt in range(max_retries):
        for ep in _ordered(endpoints):
            t0 = time.monotonic()
            try:
                resp = sess.post(ep, data={"data": query}, headers=headers, timeout=(20, timeout_s + 30))
            except requests.RequestException as exc:
                last_err = f"{ep}: {type(exc).__name__}: {exc}"
                log.warning("Overpass request failed (%s)", last_err)
                _failed_at[ep] = time.monotonic()
                time.sleep(2)
                continue
            elapsed = time.monotonic() - t0
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                except ValueError:
                    last_err = f"{ep}: non-JSON 200 response ({resp.text[:120]!r})"
                    log.warning("Overpass returned non-JSON; retrying (%s)", last_err)
                    continue
                remark = str(payload.get("remark", ""))
                if "runtime error" in remark or "Query timed out" in remark:
                    last_err = f"{ep}: {remark}"
                    log.warning("Overpass runtime error, retrying: %s", remark)
                    continue
                log.debug("Overpass %s -> %d elements in %.1fs", ep, len(payload.get("elements", [])), elapsed)
                _failed_at.pop(ep, None)
                return payload
            if resp.status_code == 400:
                raise OverpassError(f"Bad Overpass query (400) from {ep}: {resp.text[:500]}")
            last_err = f"{ep}: HTTP {resp.status_code}"
            _failed_at[ep] = time.monotonic()
            retry_after = resp.headers.get("Retry-After")
            log.warning("Overpass HTTP %d from %s after %.1fs", resp.status_code, ep, elapsed)
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 120))
            else:
                time.sleep(2)
        if attempt + 1 == max_retries:
            break
        backoff = min(120.0, 5.0 * 2**attempt) + random.uniform(0, 3)
        log.info("All Overpass endpoints failed (attempt %d/%d); backing off %.0fs", attempt + 1, max_retries, backoff)
        time.sleep(backoff)
    raise OverpassError(f"Overpass failed after {max_retries} rounds: {last_err}")


# --------------------------------------------------------------------------- queries
def courts_query(bbox: tuple[float, float, float, float], timeout_s: int) -> str:
    s, w, n, e = bbox
    b = f"{s:.6f},{w:.6f},{n:.6f},{e:.6f}"
    return f"""[out:json][timeout:{timeout_s}];
(
  node["sport"~"basketball"]({b});
  way["sport"~"basketball"]({b});
  relation["sport"~"basketball"]({b});
);
out body geom;"""


def negatives_query(bbox: tuple[float, float, float, float], timeout_s: int, per_kind: int) -> str:
    """Centers of places that look court-ish but are not basketball courts."""
    s, w, n, e = bbox
    b = f"{s:.6f},{w:.6f},{n:.6f},{e:.6f}"
    return f"""[out:json][timeout:{timeout_s}];
way["leisure"="park"]({b})->.parks;
.parks out tags center {per_kind};
way["amenity"="parking"]({b})->.parking;
.parking out tags center {per_kind};
way["leisure"="pitch"]["sport"]["sport"!~"basketball"]({b})->.pitches;
.pitches out tags center {per_kind};
way["leisure"="playground"]({b})->.playgrounds;
.playgrounds out tags center {per_kind};"""


# --------------------------------------------------------------------------- geometry
def _assemble_rings(lines: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Join open member ways of a multipolygon relation into closed rings."""
    rings: list[list[tuple[float, float]]] = []
    pending = [ln[:] for ln in lines if len(ln) >= 2]
    while pending:
        ring = pending.pop(0)
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for i, seg in enumerate(pending):
                if seg[0] == ring[-1]:
                    ring += seg[1:]
                elif seg[-1] == ring[-1]:
                    ring += seg[-2::-1]
                elif seg[-1] == ring[0]:
                    ring = seg[:-1] + ring
                elif seg[0] == ring[0]:
                    ring = seg[:0:-1] + ring
                else:
                    continue
                pending.pop(i)
                changed = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def element_to_feature(el: dict[str, Any], region: str) -> dict[str, Any] | None:
    etype, eid = el["type"], el["id"]
    tags = el.get("tags", {})
    geom: dict[str, Any]
    polygon_ll: list[list[tuple[float, float]]] = []  # rings as (lon, lat)

    if etype == "node":
        lat, lon = el["lat"], el["lon"]
        geom = {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]}
        bounds = (lat, lon, lat, lon)
    elif etype == "way":
        pts = [(p["lon"], p["lat"]) for p in el.get("geometry") or [] if p]
        if not pts:
            return None
        if len(pts) >= 4 and pts[0] == pts[-1]:
            polygon_ll = [pts]
        b = el.get("bounds") or {}
        bounds = (
            b.get("minlat", min(p[1] for p in pts)),
            b.get("minlon", min(p[0] for p in pts)),
            b.get("maxlat", max(p[1] for p in pts)),
            b.get("maxlon", max(p[0] for p in pts)),
        )
        if polygon_ll:
            geom = {"type": "Polygon", "coordinates": [[[round(x, 7), round(y, 7)] for x, y in pts]]}
        else:
            geom = {"type": "LineString", "coordinates": [[round(x, 7), round(y, 7)] for x, y in pts]}
    elif etype == "relation":
        outers = [
            [(p["lon"], p["lat"]) for p in m.get("geometry") or [] if p]
            for m in el.get("members", [])
            if m.get("type") == "way" and m.get("role", "outer") in ("outer", "") and m.get("geometry")
        ]
        polygon_ll = _assemble_rings(outers)
        b = el.get("bounds")
        if b:
            bounds = (b["minlat"], b["minlon"], b["maxlat"], b["maxlon"])
        elif polygon_ll:
            xs = [x for r in polygon_ll for x, _ in r]
            ys = [y for r in polygon_ll for _, y in r]
            bounds = (min(ys), min(xs), max(ys), max(xs))
        else:
            return None
        if len(polygon_ll) == 1:
            geom = {"type": "Polygon", "coordinates": [[[round(x, 7), round(y, 7)] for x, y in polygon_ll[0]]]}
        elif polygon_ll:
            geom = {
                "type": "MultiPolygon",
                "coordinates": [[[[round(x, 7), round(y, 7)] for x, y in r]] for r in polygon_ll],
            }
        else:
            clat, clon = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
            geom = {"type": "Point", "coordinates": [round(clon, 7), round(clat, 7)]}
    else:
        return None

    # Center = bbox center, which is what Overpass "out center" reports.
    center_lat = (bounds[0] + bounds[2]) / 2
    center_lon = (bounds[1] + bounds[3]) / 2

    area_m2: float | None = None
    if polygon_ll:
        proj = LocalMeters(center_lat, center_lon)
        area_m2 = 0.0
        for ring in polygon_ll:
            area_m2 += Polygon([proj.fwd(lat, lon) for lon, lat in ring]).area
        area_m2 = round(area_m2, 1)

    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "osm_type": etype,
            "osm_id": eid,
            "osm_ref": f"{etype}/{eid}",
            "region": region,
            "center_lat": round(center_lat, 7),
            "center_lon": round(center_lon, 7),
            "area_m2": area_m2,
            "has_polygon": bool(polygon_ll),
            "tags": tags,
        },
    }


def negative_element_to_feature(el: dict[str, Any], region: str) -> dict[str, Any] | None:
    c = el.get("center") or ({"lat": el["lat"], "lon": el["lon"]} if "lat" in el else None)
    if not c:
        return None
    tags = el.get("tags", {})
    if tags.get("leisure") == "pitch":
        kind = f"pitch:{tags.get('sport', '?')}"
    elif tags.get("amenity") == "parking":
        kind = "parking"
    elif tags.get("leisure") == "playground":
        kind = "playground"
    else:
        kind = "park"
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(c["lon"], 7), round(c["lat"], 7)]},
        "properties": {"osm_ref": f"{el['type']}/{el['id']}", "kind": kind, "region": region},
    }


# --------------------------------------------------------------------------- drivers
def fetch_courts(
    region: config.Region,
    endpoints: list[str] | None = None,
    max_tile_deg: float = config.OVERPASS_MAX_TILE_DEG,
    sleep_s: float = config.OVERPASS_SLEEP_S,
    timeout_s: int = config.OVERPASS_TIMEOUT_S,
) -> list[dict[str, Any]]:
    tiles = split_bbox(region.bbox, max_tile_deg)
    log.info(
        "%s: querying %d Overpass tile(s) over %.0f km^2",
        region.name, len(tiles), bbox_area_km2(region.bbox),
    )
    session = requests.Session()
    by_ref: dict[str, dict[str, Any]] = {}
    for i, tile in enumerate(tiles):
        if i:
            time.sleep(sleep_s)
        payload = overpass_query(courts_query(tile, timeout_s), endpoints, timeout_s, session=session)
        n_new = 0
        for el in payload.get("elements", []):
            feat = element_to_feature(el, region.name)
            if feat is None:
                continue
            ref = feat["properties"]["osm_ref"]
            if ref not in by_ref:
                by_ref[ref] = feat
                n_new += 1
        log.info("  tile %d/%d: %d elements, %d new", i + 1, len(tiles), len(payload.get("elements", [])), n_new)
    feats = list(by_ref.values())
    # Keep only features whose center lies inside the requested bbox (tiles return
    # anything that *intersects*, so big polygons can poke in from outside).
    s, w, n, e = region.bbox
    feats = [
        f for f in feats
        if s <= f["properties"]["center_lat"] <= n and w <= f["properties"]["center_lon"] <= e
    ]
    feats.sort(key=lambda f: ({"node": 0, "way": 1, "relation": 2}[f["properties"]["osm_type"]], f["properties"]["osm_id"]))
    return feats


def fetch_negatives(
    region: config.Region,
    endpoints: list[str] | None = None,
    max_tile_deg: float = config.OVERPASS_MAX_TILE_DEG,
    sleep_s: float = config.OVERPASS_SLEEP_S,
    timeout_s: int = config.OVERPASS_TIMEOUT_S,
    per_kind: int = 400,
) -> list[dict[str, Any]]:
    tiles = split_bbox(region.bbox, max_tile_deg)
    per_tile = max(25, per_kind // len(tiles))
    session = requests.Session()
    by_ref: dict[str, dict[str, Any]] = {}
    for i, tile in enumerate(tiles):
        time.sleep(sleep_s)
        payload = overpass_query(negatives_query(tile, timeout_s, per_tile), endpoints, timeout_s, session=session)
        for el in payload.get("elements", []):
            feat = negative_element_to_feature(el, region.name)
            if feat:
                by_ref.setdefault(feat["properties"]["osm_ref"], feat)
        log.info("  negatives tile %d/%d: %d elements", i + 1, len(tiles), len(payload.get("elements", [])))
    return list(by_ref.values())


def summarize(feats: list[dict[str, Any]]) -> str:
    n_poly = sum(1 for f in feats if f["properties"]["has_polygon"])
    by_type: dict[str, int] = {}
    for f in feats:
        by_type[f["properties"]["osm_type"]] = by_type.get(f["properties"]["osm_type"], 0) + 1
    pitch = sum(1 for f in feats if f["properties"]["tags"].get("leisure") == "pitch")
    return (
        f"{len(feats)} features ({', '.join(f'{k}={v}' for k, v in sorted(by_type.items()))}); "
        f"{n_poly} with polygons; {pitch} tagged leisure=pitch"
    )


def run_region(
    region: config.Region,
    out_dir: Path,
    force: bool,
    negatives: bool,
    endpoints: list[str] | None,
    max_tile_deg: float,
    sleep_s: float,
    timeout_s: int,
) -> Path:
    out = out_dir / f"{region.name}.geojson"
    if out.exists() and not force:
        feats = read_json(out)["features"]
        log.info("%s: cached %s (%s); use --force to refetch", region.name, out, summarize(feats))
    else:
        feats = fetch_courts(region, endpoints, max_tile_deg, sleep_s, timeout_s)
        write_json(
            out,
            feature_collection(
                feats,
                region=region.name,
                bbox=list(region.bbox),
                fetched=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                attribution=ATTRIBUTION,
            ),
        )
        log.info("%s: wrote %s -> %s", region.name, out, summarize(feats))
    if negatives:
        neg_out = out_dir / f"{region.name}.negatives.geojson"
        if neg_out.exists() and not force:
            log.info("%s: cached negatives %s", region.name, neg_out)
        else:
            negs = fetch_negatives(region, endpoints, max_tile_deg, sleep_s, timeout_s)
            write_json(neg_out, feature_collection(negs, region=region.name, attribution=ATTRIBUTION))
            kinds: dict[str, int] = {}
            for f in negs:
                k = f["properties"]["kind"].split(":")[0]
                kinds[k] = kinds.get(k, 0) + 1
            log.info("%s: wrote %d negative locations %s -> %s", region.name, len(negs), kinds, neg_out)
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--regions", nargs="+", help="region and/or group names from regions.json")
    g.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    ap.add_argument("--name", help="output name for --bbox (default derived from the bbox)")
    ap.add_argument("--negatives", action="store_true",
                    help="also fetch park / parking / non-basketball pitch / playground centers for negative chips")
    ap.add_argument("--force", action="store_true", help="refetch even if a cached file exists")
    ap.add_argument("--out-dir", type=Path, default=config.OSM_DIR)
    ap.add_argument("--endpoint", action="append", help="Overpass endpoint URL (repeatable; default: kumi then overpass-api.de)")
    ap.add_argument("--max-tile-deg", type=float, default=config.OVERPASS_MAX_TILE_DEG)
    ap.add_argument("--sleep", type=float, default=config.OVERPASS_SLEEP_S, help="seconds between requests")
    ap.add_argument("--timeout", type=int, default=config.OVERPASS_TIMEOUT_S)
    add_common_args(ap)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)

    regions = config.resolve_regions(args.regions) if args.regions else [config.bbox_region(args.bbox, args.name)]
    failed: list[str] = []
    for i, region in enumerate(regions):
        if i:
            time.sleep(args.sleep)
        try:
            run_region(region, args.out_dir, args.force, args.negatives, args.endpoint,
                       args.max_tile_deg, args.sleep, args.timeout)
        except OverpassError as exc:
            # Nothing is written for a region with a failed tile, so a re-run fetches it whole.
            log.error("%s: giving up for now (%s); re-run later to fetch it", region.name, exc)
            failed.append(region.name)
    if failed:
        raise SystemExit(f"failed regions (re-run them later): {' '.join(failed)}")


if __name__ == "__main__":
    main()
