"""Official basketball-court "truth" layer from open city / county / state data.

OpenStreetMap misses many courts, so scoring the detector against OSM counts real
unmapped finds as errors. This module builds an independent reference set from
official, openly licensed park-department and state inventories, listed in
``pipeline/truth_sources.json`` (the registry: endpoint, basketball filter, field
mapping, license, coverage layer, location precision).

Commands (``python -m hoop_pipeline.truth <cmd> --help``)::

    list            registry + manifest summary
    refresh         download every source (courts + coverage polygons) into data/truth/raw/, then build
    build           normalise the raw downloads (no network) -> courts_truth.parquet, coverage.parquet, sources.json
    osm-compare     share of official courts that OSM already has within --radius (default 40 m)
    evaluate        recall of a candidates/detections file against the truth, and the share of detections
                    inside covered parks that match an official court
    export-labels   official courts with no OSM court nearby -> candidate extra training positives (unsnapped)
    chips           NAIP chips (CPU) around sampled truth courts with the official geometry drawn on

Outputs (``data/truth/``)::

    raw/<key>/courts.geojsonl, coverage.geojsonl, fetch.json   raw downloads (one GeoJSON feature per line)
    courts_truth.parquet   GeoParquet, one row per official court record, point geometry (EPSG:4326)
    coverage.parquet       GeoParquet, polygons each source is complete for (parks / city), coverage_id
    sources.json           manifest: per-source URL, license, dates, counts, coverage, precision
    osm_match.parquet / osm_compare.json   per-row nearest OSM court; per-source OSM coverage
    new_positive_candidates.geojson        export-labels output

Precision classes (per source, checked on NAIP chips; see README):
``court_polygon`` the court surface itself; ``court_point`` a point on the court;
``feature_point`` one point per court, placement not verified / sometimes off the court;
``park_point`` the park centroid or address, the court is somewhere in the park.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import random
import re
import sys
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
import shapely
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.strtree import STRtree

from . import config
from .common import add_common_args, read_json, setup_logging, write_json
from .geoparquet import read_geoparquet, write_geoparquet

log = logging.getLogger("truth")

TRUTH_DIR = config.DATA_DIR / "truth"
RAW_DIR = TRUTH_DIR / "raw"
REGISTRY_FILE = config.PIPELINE_ROOT / "truth_sources.json"
COURTS_FILE = TRUTH_DIR / "courts_truth.parquet"
COVERAGE_FILE = TRUTH_DIR / "coverage.parquet"
MANIFEST_FILE = TRUTH_DIR / "sources.json"
OSM_MATCH_FILE = TRUTH_DIR / "osm_match.parquet"
OSM_COMPARE_FILE = TRUTH_DIR / "osm_compare.json"
LABELS_FILE = TRUTH_DIR / "new_positive_candidates.geojson"
OSM_COURTS_FILE = config.OSM_US_DIR / "courts_us.parquet"
OVERTURE_PLACES_FILE = config.DATA_DIR / "sites" / "overture_places.parquet"

COURT_LEVEL = ("court_polygon", "court_point", "feature_point")
LIVE_STATUS = ("active", "closed", "unknown")       # physically present; "inactive" is reported separately
HIDDEN = ("indoor", "covered")                        # not visible from above: gyms, roofed or fully shaded courts
ALBERS = "EPSG:5070"                                 # CONUS Albers, metres
PAGE_SLEEP_S = 0.5


# --------------------------------------------------------------------------- projection helpers
_TO_M = Transformer.from_crs("EPSG:4326", ALBERS, always_xy=True)


def to_m(geoms: np.ndarray) -> np.ndarray:
    """lon/lat shapely geometries -> CONUS Albers metres (vectorised)."""
    geoms = np.asarray(geoms, dtype=object)
    if not len(geoms):
        return geoms
    return shapely.transform(geoms, lambda xy: np.column_stack(_TO_M.transform(xy[:, 0], xy[:, 1])))


def pts_m(lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
    x, y = _TO_M.transform(np.asarray(lons, float), np.asarray(lats, float))
    return shapely.points(x, y)


# --------------------------------------------------------------------------- registry
def load_registry(path: Path | None = None) -> list[dict]:
    path = Path(path or REGISTRY_FILE)
    reg = read_json(path)
    sources = reg["sources"] if isinstance(reg, dict) else reg
    keys = [s["key"] for s in sources]
    dup = [k for k, n in Counter(keys).items() if n > 1]
    if dup:
        raise ValueError(f"duplicate source keys in {path}: {dup}")
    return sources


def pick_sources(sources: list[dict], wanted: list[str] | None, include_disabled: bool = False) -> list[dict]:
    if wanted:
        names = {w.strip() for part in wanted for w in part.split(",") if w.strip()}
        unknown = names - {s["key"] for s in sources}
        if unknown:
            raise SystemExit(f"unknown source key(s): {sorted(unknown)}")
        return [s for s in sources if s["key"] in names]
    return [s for s in sources if include_disabled or s.get("enabled", True)]


# --------------------------------------------------------------------------- HTTP
def http_session() -> requests.Session:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT
    retry = Retry(total=5, backoff_factor=2.0, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET", "POST"}), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def _json(resp: requests.Response) -> Any:
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"non-JSON response from {resp.url[:200]}: {resp.text[:200]!r}") from exc


def esri_geometry_to_geojson(g: dict | None) -> dict | None:
    """Esri JSON geometry -> GeoJSON geometry (fallback when a server has no f=geojson)."""
    if not g:
        return None
    if "x" in g:
        if g["x"] is None or g["y"] is None or (isinstance(g["x"], float) and math.isnan(g["x"])):
            return None
        return {"type": "Point", "coordinates": [g["x"], g["y"]]}
    if "points" in g:
        return {"type": "MultiPoint", "coordinates": g["points"]}
    if "paths" in g:
        return {"type": "MultiLineString", "coordinates": g["paths"]}
    if "rings" in g:
        rings = [shapely.LinearRing(r) for r in g["rings"] if len(r) >= 4]
        shells = [r for r in rings if not shapely.is_ccw(r)]          # Esri outer rings are clockwise
        holes = [r for r in rings if shapely.is_ccw(r)]
        if not shells:                                                # mis-oriented data: treat all as shells
            shells, holes = rings, []
        polys = []
        for sh in shells:
            sp = shapely.Polygon(sh)
            mine = [h for h in holes if sp.contains(shapely.Point(h.coords[0]))]
            polys.append(shapely.Polygon(sh, mine))
        geom = polys[0] if len(polys) == 1 else shapely.MultiPolygon(polys)
        return mapping(shapely.make_valid(geom))
    return None


def fetch_arcgis(sess: requests.Session, url: str, where: str, out: Path, polygons_simplify: bool = False) -> dict:
    """All features of one ArcGIS layer matching ``where`` -> GeoJSON lines (lon/lat)."""
    url = url.rstrip("/")
    meta = _json(sess.get(url, params={"f": "json"}, timeout=60))
    if "error" in meta:
        raise RuntimeError(f"{url}: {meta['error']}")
    ids_resp = _json(sess.post(url + "/query", data={"where": where or "1=1", "returnIdsOnly": "true", "f": "json"},
                               timeout=120))
    if "error" in ids_resp:
        raise RuntimeError(f"{url} ids: {ids_resp['error']}")
    ids = sorted(ids_resp.get("objectIds") or [])
    oid = ids_resp.get("objectIdFieldName") or meta.get("objectIdField") or "OBJECTID"
    chunk = max(1, min(int(meta.get("maxRecordCount") or 1000), 250 if polygons_simplify else 500))
    tmp = out.with_name(out.name + ".tmp")
    n = 0
    use_geojson = True
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for i in range(0, len(ids), chunk):
            sub = ids[i:i + chunk]
            params = {"objectIds": ",".join(map(str, sub)), "outFields": "*", "outSR": "4326",
                      "returnGeometry": "true", "f": "geojson" if use_geojson else "json"}
            if polygons_simplify:
                params.update(geometryPrecision="6", maxAllowableOffset="0.000005")
            data = _json(sess.post(url + "/query", data=params, timeout=180))
            if use_geojson and ("error" in data or "features" not in data):
                log.info("%s: f=geojson not supported, falling back to Esri JSON", url)
                use_geojson = False
                params["f"] = "json"
                data = _json(sess.post(url + "/query", data=params, timeout=180))
            if "error" in data:
                raise RuntimeError(f"{url} query: {data['error']}")
            for feat in data.get("features", []):
                if not use_geojson:
                    feat = {"type": "Feature", "properties": feat.get("attributes", {}),
                            "geometry": esri_geometry_to_geojson(feat.get("geometry"))}
                fh.write(json.dumps(feat, separators=(",", ":")) + "\n")
                n += 1
            time.sleep(PAGE_SLEEP_S)
    tmp.replace(out)
    edit = (meta.get("editingInfo") or {})
    last_edit = edit.get("dataLastEditDate") or edit.get("lastEditDate")
    return {"rows": n, "object_ids": len(ids), "oid_field": oid, "layer_name": meta.get("name"),
            "geometry_type": meta.get("geometryType"),
            "native_wkid": ((meta.get("extent") or {}).get("spatialReference") or {}).get("wkid"),
            "last_edit": _ms_to_iso(last_edit)}


def fetch_socrata(sess: requests.Session, domain: str, ds_id: str, where: str | None, out: Path,
                  page: int = 5000) -> dict:
    """All rows of a Socrata dataset (optionally filtered) -> GeoJSON lines."""
    base = f"https://{domain}/resource/{ds_id}.geojson"
    tmp = out.with_name(out.name + ".tmp")
    n, offset = 0, 0
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        while True:
            params = {"$limit": page, "$offset": offset, "$order": ":id"}
            if where:
                params["$where"] = where
            data = _json(sess.get(base, params=params, timeout=180))
            feats = data.get("features", [])
            for feat in feats:
                fh.write(json.dumps(feat, separators=(",", ":")) + "\n")
            n += len(feats)
            if len(feats) < page:
                break
            offset += page
            time.sleep(PAGE_SLEEP_S)
    tmp.replace(out)
    info: dict[str, Any] = {"rows": n}
    try:
        m = _json(sess.get(f"https://{domain}/api/views/{ds_id}.json", timeout=60))
        info.update(layer_name=m.get("name"), last_edit=_s_to_iso(m.get("rowsUpdatedAt")),
                    socrata_license=(m.get("license") or {}).get("name"))
    except Exception as exc:  # metadata is nice-to-have
        log.warning("socrata metadata %s/%s: %s", domain, ds_id, exc)
    return info


def fetch_file(sess: requests.Session, url: str, fmt: str, out: Path, raw_dir: Path, spec: dict) -> dict:
    """Plain download (geojson / csv / zipped shapefile) -> GeoJSON lines."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.rstrip("/").split("/")[-1].split("?")[0]) or "download"
    dl = raw_dir / ("dl_" + name)
    with sess.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dl, "wb") as fh:
            for block in r.iter_content(1 << 20):
                fh.write(block)
    n = 0
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for feat in _iter_file_features(dl, fmt, spec):
            fh.write(json.dumps(feat, separators=(",", ":")) + "\n")
            n += 1
    tmp.replace(out)
    return {"rows": n, "downloaded_file": dl.name, "bytes": dl.stat().st_size}


def _iter_file_features(path: Path, fmt: str, spec: dict):
    if fmt == "geojson":
        with open(path, encoding="utf-8") as fh:
            yield from json.load(fh).get("features", [])
    elif fmt == "csv":
        import csv

        lon_f, lat_f = spec["fields"]["lon"], spec["fields"]["lat"]
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    geom = {"type": "Point", "coordinates": [float(row[lon_f]), float(row[lat_f])]}
                except (TypeError, ValueError, KeyError):
                    geom = None
                yield {"type": "Feature", "properties": row, "geometry": geom}
    elif fmt == "shp-zip":
        import shapefile  # pyshp

        with zipfile.ZipFile(path) as zf:
            stem = next(n[:-4] for n in zf.namelist() if n.lower().endswith(".shp"))
            prj = next((n for n in zf.namelist() if n.lower().endswith(".prj")), None)
            tr = None
            if prj:
                from pyproj import CRS

                crs = CRS.from_wkt(zf.read(prj).decode("utf-8", "replace"))
                if crs.to_epsg() != 4326:
                    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            with zf.open(stem + ".shp") as shp, zf.open(stem + ".dbf") as dbf:
                rd = shapefile.Reader(shp=shp, dbf=dbf)
                fields = [f[0] for f in rd.fields[1:]]
                for sr in rd.iterShapeRecords():
                    g = shape(sr.shape.__geo_interface__) if sr.shape.shapeType else None
                    if g is not None and tr is not None:
                        g = shapely.transform(g, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))
                    yield {"type": "Feature", "properties": dict(zip(fields, sr.record)),
                           "geometry": mapping(g) if g is not None else None}
    else:
        raise ValueError(f"unknown file format {fmt!r}")


def fetch_overture_local(out: Path) -> dict:
    """Overture places in category basketball_court, from the pipeline's existing places extract."""
    if not OVERTURE_PLACES_FILE.exists():
        raise FileNotFoundError(f"{OVERTURE_PLACES_FILE} missing: run `python -m hoop_pipeline.sites overture-places`")
    from .states import load_states

    cols, _ = read_geoparquet(OVERTURE_PLACES_FILE, with_geometry=False)
    sel = np.flatnonzero((cols["cat"] == "basketball_court") | (cols["basic_category"] == "basketball_court"))
    st = load_states().assign(cols["lon"][sel], cols["lat"][sel])
    n = 0
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for j, i in enumerate(sel):
            if not st[j]:
                continue  # outside CONUS (Mexico, Canada)
            props = {k: (v[i].item() if hasattr(v[i], "item") else v[i]) for k, v in cols.items()}
            props["state"] = str(st[j])
            feat = {"type": "Feature", "properties": props,
                    "geometry": {"type": "Point", "coordinates": [float(cols["lon"][i]), float(cols["lat"][i])]}}
            fh.write(json.dumps(feat, separators=(",", ":"), default=str) + "\n")
            n += 1
    tmp.replace(out)
    meta_file = config.DATA_DIR / "sites" / "sources.json"
    rel = read_json(meta_file).get("overture", {}).get("name") if meta_file.exists() else None
    return {"rows": n, "layer_name": rel, "local_file": str(OVERTURE_PLACES_FILE.relative_to(config.PIPELINE_ROOT))}


def _ms_to_iso(v: Any) -> str | None:
    try:
        return dt.datetime.fromtimestamp(int(v) / 1000, dt.timezone.utc).date().isoformat() if v else None
    except (TypeError, ValueError, OSError):
        return None


def _s_to_iso(v: Any) -> str | None:
    try:
        return dt.datetime.fromtimestamp(int(v), dt.timezone.utc).date().isoformat() if v else None
    except (TypeError, ValueError, OSError):
        return None


def fetch_layer(sess: requests.Session, api: dict, out: Path, raw_dir: Path, spec: dict, polygons: bool = False) -> dict:
    kind = api["kind"]
    if kind == "arcgis":
        return fetch_arcgis(sess, api["url"], api.get("where") or "1=1", out, polygons_simplify=polygons)
    if kind == "socrata":
        return fetch_socrata(sess, api["domain"], api["id"], api.get("where"), out)
    if kind == "file":
        return fetch_file(sess, api["url"], api.get("format", "geojson"), out, raw_dir, spec)
    if kind == "overture_local":
        return fetch_overture_local(out)
    raise ValueError(f"unknown api kind {kind!r}")


def refresh_source(sess: requests.Session, spec: dict, force: bool = False) -> dict:
    key = spec["key"]
    d = RAW_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    courts = d / "courts.geojsonl"
    info_path = d / "fetch.json"
    info = read_json(info_path) if info_path.exists() and not force else {}
    if force or not courts.exists():
        t0 = time.monotonic()
        info["courts"] = fetch_layer(sess, spec["api"], courts, d, spec)
        info["courts"]["seconds"] = round(time.monotonic() - t0, 1)
        info["fetched_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        log.info("%s: %d court rows", key, info["courts"]["rows"])
    cov = (spec.get("coverage") or {}).get("layer")
    cov_path = d / "coverage.geojsonl"
    if cov and (force or not cov_path.exists()):
        t0 = time.monotonic()
        info["coverage"] = fetch_layer(sess, cov, cov_path, d, spec, polygons=True)
        info["coverage"]["seconds"] = round(time.monotonic() - t0, 1)
        log.info("%s: %d coverage polygons", key, info["coverage"]["rows"])
    info["api"] = spec["api"]
    write_json(info_path, info)
    return info


# --------------------------------------------------------------------------- normalisation
def _get(props: dict, field: str | None) -> Any:
    if not field:
        return None
    if field in props:
        return props[field]
    lower = {k.lower(): k for k in props}
    k = lower.get(field.lower())
    return props[k] if k is not None else None


def predicate(spec: Any, props: dict) -> bool:
    """Tiny filter language: {"field","eq"|"in"|"notin"|"regex"|"gt"|"truthy"|"isnull"} / all / any / not."""
    if spec is None:
        return True
    if "all" in spec:
        return all(predicate(p, props) for p in spec["all"])
    if "any" in spec:
        return any(predicate(p, props) for p in spec["any"])
    if "not" in spec:
        return not predicate(spec["not"], props)
    if "sum_gt" in spec:  # {"sum_gt": [fields], "value": 0}
        return _sum_fields(props, spec["sum_gt"]) > spec.get("value", 0)
    v = _get(props, spec["field"])
    if "isnull" in spec:
        return (v in (None, "")) == bool(spec["isnull"])
    if "eq" in spec:
        return _norm(v) == _norm(spec["eq"])
    if "in" in spec:
        return _norm(v) in {_norm(x) for x in spec["in"]}
    if "notin" in spec:
        return _norm(v) not in {_norm(x) for x in spec["notin"]}
    if "regex" in spec:
        return v is not None and re.search(spec["regex"], str(v), re.I) is not None
    if "gt" in spec:
        return _num(v) is not None and _num(v) > spec["gt"]
    if "truthy" in spec:
        return _truthy(v) == bool(spec["truthy"])
    raise ValueError(f"bad predicate {spec}")


def _norm(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        m = re.search(r"\d+(\.\d+)?", str(v))
        return float(m.group()) if m else None
    return None if math.isnan(f) else f


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "y", "yes", "true", "t", "x")
    return bool(v) and not (isinstance(v, float) and math.isnan(v))


def _sum_fields(props: dict, fields: list[str]) -> float:
    return sum((_num(_get(props, f)) or 0.0) for f in fields)


def _court_count(props: dict, spec: Any) -> int | None:
    if spec is None:
        return None
    if isinstance(spec, int):
        return spec
    if isinstance(spec, str):
        v = _num(_get(props, spec))
        return int(round(v)) if v is not None else None
    if isinstance(spec, list):
        vals = [_num(_get(props, f)) for f in spec]
        if all(v is None for v in vals):
            return None
        return int(round(sum(v or 0 for v in vals)))
    if isinstance(spec, dict) and "weights" in spec:  # {"weights": {"FULL": 1, "HALF": 0.5}} -> rounded up
        tot = sum((_num(_get(props, f)) or 0) * w for f, w in spec["weights"].items())
        return int(math.ceil(tot)) if tot else None  # no number in the field: count unknown
    raise ValueError(f"bad court_count spec {spec}")


def _mapped(props: dict, spec: Any, default: str) -> str:
    """{"field": F, "map": {raw: value}, "default": d, "regex_map": [[regex, value], ...]} or a constant string."""
    if spec is None:
        return default
    if isinstance(spec, str):
        return spec
    v = _get(props, spec.get("field"))
    if v is not None:
        mp = {_norm(k): val for k, val in (spec.get("map") or {}).items()}
        if _norm(v) in mp:
            return mp[_norm(v)]
        for rx, val in spec.get("regex_map") or []:
            if re.search(rx, str(v), re.I):
                return val
    return spec.get("default", default)


def _text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _source_id(props: dict, fields: Any, fallback: int) -> str:
    if isinstance(fields, list):
        parts = [_text(_get(props, f)) for f in fields]
        if any(parts):
            return "|".join(p or "" for p in parts)
    elif fields:
        v = _text(_get(props, fields))
        if v:
            return v.removesuffix(".0") if re.fullmatch(r"\d+\.0", v) else v
    return f"row{fallback}"


def _point_of(geom: Any) -> Any:
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Point":
        return geom
    c = geom.centroid
    if geom.geom_type in ("Polygon", "MultiPolygon") and not geom.contains(c):
        c = geom.point_on_surface()
    return c


def iter_geojsonl(path: Path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def normalise_source(spec: dict) -> tuple[dict[str, list], dict]:
    """Raw GeoJSON lines of one source -> column lists + stats."""
    key = spec["key"]
    path = RAW_DIR / key / "courts.geojsonl"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run `truth refresh --sources {key}`")
    f = spec.get("fields", {})
    cols: dict[str, list] = defaultdict(list)
    stats = Counter()
    seen_ids: Counter = Counter()
    for i, feat in enumerate(iter_geojsonl(path)):
        stats["raw_rows"] += 1
        props = feat.get("properties") or {}
        if not predicate(spec.get("select"), props):
            stats["not_selected"] += 1
            continue
        status = _mapped(props, f.get("status"), "unknown")
        if status == "removed":
            stats["removed"] += 1
            continue
        geom = None
        if feat.get("geometry"):
            try:
                geom = shape(feat["geometry"])
                if not geom.is_valid:
                    geom = shapely.make_valid(geom)
            except Exception:
                geom = None
        if (geom is None or geom.is_empty) and f.get("lon") and f.get("lat"):
            lon, lat = _num(_get(props, f["lon"])), _num(_get(props, f["lat"]))
            if lon is not None and lat is not None:
                geom = shapely.Point(lon, lat)
        pt = _point_of(geom)
        if pt is None or not (-125.5 < pt.x < -66.0 and 24.0 < pt.y < 50.0):
            stats["no_or_bad_geometry"] += 1
            continue
        feature_kind = _mapped(props, f.get("feature"), "court")
        if feature_kind == "drop":
            stats["not_selected"] += 1
            continue
        sid = _source_id(props, f.get("source_id"), i)
        seen_ids[sid] += 1
        if seen_ids[sid] > 1:
            sid = f"{sid}~{seen_ids[sid]}"
        precision = _mapped(props, f.get("precision"), spec["precision"])
        # a polygon is a court footprint only for court-level sources (park-level rows carry the park polygon)
        footprint = geom if geom.geom_type in ("Polygon", "MultiPolygon") and precision == "court_polygon" else None
        public = spec.get("public", True)
        if f.get("public") is not None:
            public = _mapped(props, f["public"], "unknown")
            public = {"yes": True, "no": False}.get(public, None)
        cols["truth_id"].append(f"{key}:{sid}")
        cols["source"].append(key)
        cols["source_id"].append(sid)
        cols["city"].append(_text(_get(props, f["city"])) if isinstance(f.get("city"), str) and _get(props, f["city"]) is not None
                            else spec.get("city"))
        cols["state"].append(_text(_get(props, f["state"])) if f.get("state") else spec.get("state"))
        cols["name"].append(_text(_get(props, f.get("name"))))
        cols["park_name"].append(_text(_get(props, f.get("park_name"))))
        cols["park_ref"].append(_text(_get(props, f.get("park_ref"))))
        cols["courts"].append(_court_count(props, f.get("court_count")))
        cols["full_half"].append(_text(_get(props, f.get("full_half"))) if isinstance(f.get("full_half"), str)
                                 else (_mapped(props, f.get("full_half"), None) if f.get("full_half") else None))
        cols["indoor"].append(_mapped(props, f.get("indoor"), "unknown"))
        cols["status"].append(status)
        cols["feature"].append(feature_kind)
        cols["public"].append(public)
        cols["precision"].append(precision)
        cols["tier"].append(spec.get("tier", "official"))
        cols["lon"].append(float(pt.x))
        cols["lat"].append(float(pt.y))
        cols["_footprint"].append(footprint)
        keep = f.get("keep_attrs") or []
        cols["attrs"].append(json.dumps({k: _get(props, k) for k in keep}, default=str) if keep else None)
        stats["kept"] += 1
    return cols, dict(stats)


def load_coverage(spec: dict) -> tuple[list[str], list[str], list[Any]]:
    """(coverage_id, name, polygon) for one source's coverage layer (dissolved by id)."""
    key = spec["key"]
    cov = spec.get("coverage") or {}
    layer = cov.get("layer")
    if not layer:
        return [], [], []
    path = RAW_DIR / key / "coverage.geojsonl"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run `truth refresh --sources {key}`")
    by_id: dict[str, list] = defaultdict(list)
    names: dict[str, str | None] = {}
    sel = layer.get("select")
    for i, feat in enumerate(iter_geojsonl(path)):
        props = feat.get("properties") or {}
        if not predicate(sel, props) or not feat.get("geometry"):
            continue
        try:
            g = shapely.make_valid(shape(feat["geometry"]))
        except Exception:
            continue
        polys = [p for p in getattr(g, "geoms", [g]) if p.geom_type in ("Polygon", "MultiPolygon")]
        if not polys:
            continue
        cid = _source_id(props, layer.get("id_field"), i)
        by_id[cid].extend(polys)
        names.setdefault(cid, _text(_get(props, layer.get("name_field"))))
    ids, nm, geoms = [], [], []
    for cid, parts in by_id.items():
        g = parts[0] if len(parts) == 1 else shapely.union_all(parts)
        ids.append(f"{key}:{cid}")
        nm.append(names.get(cid))
        geoms.append(g)
    return ids, nm, geoms


def assign_coverage(lons: list[float], lats: list[float], cov_ids: list[str], cov_geoms: list[Any],
                    tol_m: float = 25.0) -> list[str | None]:
    if not cov_ids or not lons:
        return [None] * len(lons)
    gm = to_m(np.asarray(cov_geoms, dtype=object))
    tree = STRtree(gm)
    p = pts_m(np.asarray(lons), np.asarray(lats))
    out: list[str | None] = [None] * len(lons)
    pi, gi = tree.query(p, predicate="intersects")
    for a, b in zip(pi, gi):
        if out[a] is None:
            out[a] = cov_ids[b]
    missing = [i for i, v in enumerate(out) if v is None]
    if missing:
        mi, mg = tree.query_nearest(p[missing], max_distance=tol_m, all_matches=False)
        for a, b in zip(mi, mg):
            out[missing[a]] = cov_ids[b]
    return out


def build(sources: list[dict]) -> dict:
    """Raw downloads -> courts_truth.parquet + coverage.parquet + sources.json."""
    all_cols: dict[str, list] = defaultdict(list)
    cov_cols: dict[str, list] = defaultdict(list)
    manifest_sources: dict[str, dict] = {}
    failed: list[str] = []
    for spec in sources:
        key = spec["key"]
        try:
            cols, stats = normalise_source(spec)
            cids, cnames, cgeoms = load_coverage(spec)
        except FileNotFoundError as exc:
            log.warning("%s: %s", key, exc)
            failed.append(key)
            continue
        cov_id = assign_coverage(cols["lon"], cols["lat"], cids, cgeoms)
        cov_name = dict(zip(cids, cnames))
        park_names = [pn if pn else cov_name.get(c) if c else None for pn, c in zip(cols["park_name"], cov_id)]
        cols["park_name"] = park_names
        cols["coverage_id"] = cov_id
        n = len(cols["truth_id"])
        # duplicate coordinates within a source = several records on one point (often a park centroid)
        xy = Counter(zip(np.round(cols["lon"], 6), np.round(cols["lat"], 6))) if n else Counter()
        cols["same_xy"] = [xy[(round(a, 6), round(b, 6))] for a, b in zip(cols["lon"], cols["lat"])]
        fp = cols.pop("_footprint")
        area = [None] * n
        if n:
            fpm = to_m(np.asarray([g if g is not None else shapely.Point() for g in fp], dtype=object))
            area = [float(shapely.area(g)) if f is not None else None for g, f in zip(fpm, fp)]
        cols["area_m2"] = area
        cols["footprint_wkb"] = [shapely.to_wkb(g) if g is not None else None for g in fp]
        for k, v in cols.items():
            all_cols[k].extend(v)
        if cids:
            cm = to_m(np.asarray(cgeoms, dtype=object))
            cov_cols["coverage_id"].extend(cids)
            cov_cols["source"].extend([key] * len(cids))
            cov_cols["name"].extend(cnames)
            cov_cols["kind"].extend([spec["coverage"].get("kind", "park")] * len(cids))
            cov_cols["area_m2"].extend(float(a) for a in shapely.area(cm))
            cov_cols["_geom"].extend(cgeoms)
        fetch = read_json(RAW_DIR / key / "fetch.json") if (RAW_DIR / key / "fetch.json").exists() else {}
        courts_known = [c for c in cols["courts"] if c is not None]
        live = [s in LIVE_STATUS for s in cols["status"]]
        manifest_sources[key] = {
            **{k: spec.get(k) for k in ("name", "publisher", "city", "state", "landing_url", "api", "license",
                                        "attribution", "precision", "tier", "public", "notes")},
            "coverage": {k: v for k, v in (spec.get("coverage") or {}).items()},
            "fetched_at": fetch.get("fetched_at"),
            "source_last_updated": (fetch.get("courts") or {}).get("last_edit"),
            "raw": fetch.get("courts"),
            "coverage_raw": fetch.get("coverage"),
            "stats": stats,
            "rows": n,
            "rows_live": int(sum(live)),
            "rows_by_status": dict(Counter(cols["status"])),
            "rows_by_feature": dict(Counter(cols["feature"])),
            "rows_by_indoor": dict(Counter(cols["indoor"])),
            "courts_sum_where_given": int(sum(courts_known)) if courts_known else None,
            "rows_with_court_count": len(courts_known),
            "courts_estimate": int(sum((c if c is not None else 1) for c in cols["courts"])),
            "coverage_polygons": len(cids),
            "coverage_km2": round(sum(cov_cols["area_m2"][-len(cids):]) / 1e6, 2) if cids else None,
            "rows_in_coverage": int(sum(c is not None for c in cov_id)),
            "rows_sharing_coordinates": int(sum(s > 1 for s in cols["same_xy"])),
            "bbox": [round(min(cols["lon"]), 4), round(min(cols["lat"]), 4), round(max(cols["lon"]), 4),
                     round(max(cols["lat"]), 4)] if n else None,
        }
        log.info("%-24s %5d rows (%s), %d coverage polygons, %d rows in coverage", key, n, dict(stats), len(cids),
                 manifest_sources[key]["rows_in_coverage"])
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    geoms = shapely.points(np.asarray(all_cols["lon"], float), np.asarray(all_cols["lat"], float)) \
        if all_cols["lon"] else np.array([], dtype=object)
    columns = {k: pa.array(v) for k, v in all_cols.items() if k not in ("footprint_wkb", "courts", "area_m2", "public")}
    columns["courts"] = pa.array(all_cols["courts"], type=pa.int32())
    columns["area_m2"] = pa.array(all_cols["area_m2"], type=pa.float64())
    columns["public"] = pa.array(all_cols["public"], type=pa.bool_())
    columns["footprint_wkb"] = pa.array(all_cols["footprint_wkb"], type=pa.binary())
    write_geoparquet(COURTS_FILE, columns, geoms)
    cg = cov_cols.pop("_geom", [])
    write_geoparquet(COVERAGE_FILE, {k: pa.array(v) for k, v in cov_cols.items()}, np.asarray(cg, dtype=object))
    manifest = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "registry": str(REGISTRY_FILE.relative_to(config.PIPELINE_ROOT)),
        "files": {"courts": COURTS_FILE.name, "coverage": COVERAGE_FILE.name},
        "totals": {
            "sources": len(manifest_sources),
            "rows": len(all_cols["truth_id"]),
            "official_rows": int(sum(t == "official" for t in all_cols["tier"])),
            "official_live_court_rows": int(sum(t == "official" and s in LIVE_STATUS and fk == "court"
                                                for t, s, fk in zip(all_cols["tier"], all_cols["status"],
                                                                    all_cols["feature"]))),
            "official_courts_estimate": int(sum((c if c is not None else 1) for c, t, s, fk in zip(
                all_cols["courts"], all_cols["tier"], all_cols["status"], all_cols["feature"])
                if t == "official" and s in LIVE_STATUS and fk == "court")),
            "coverage_polygons": len(cg),
        },
        "failed": failed,
        "sources": manifest_sources,
    }
    if MANIFEST_FILE.exists():  # keep the OSM comparison from the last osm-compare run
        old = read_json(MANIFEST_FILE)
        for k, v in old.get("sources", {}).items():
            if k in manifest["sources"] and "osm" in v:
                manifest["sources"][k]["osm"] = v["osm"]
    write_json(MANIFEST_FILE, manifest)
    log.info("truth: %d rows from %d sources -> %s; %d coverage polygons -> %s", len(all_cols["truth_id"]),
             len(manifest_sources), COURTS_FILE, len(cg), COVERAGE_FILE)
    return manifest


# --------------------------------------------------------------------------- loading
def load_truth(path: Path | None = None) -> tuple[dict[str, np.ndarray], np.ndarray]:
    path = Path(path or COURTS_FILE)
    if not path.exists():
        raise SystemExit(f"{path} missing: run `python -m hoop_pipeline.truth refresh` (or build)")
    cols, geoms = read_geoparquet(path)
    return cols, geoms


def truth_geoms_m(cols: dict[str, np.ndarray]) -> np.ndarray:
    """Court footprint where the source has one, else the point, in Albers metres."""
    pts = pts_m(cols["lon"], cols["lat"])
    fp = cols["footprint_wkb"]
    has = np.array([b is not None for b in fp])
    if has.any():
        polys = to_m(shapely.from_wkb(np.asarray([b for b in fp[has]], dtype=object)))
        pts = pts.copy()
        pts[has] = polys
    return pts


def load_osm(bbox: tuple[float, float, float, float] | None = None, pad: float = 0.05):
    cols, geoms = read_geoparquet(OSM_COURTS_FILE, columns=["osm_ref", "center_lon", "center_lat", "outdoor",
                                                            "label_ok", "state", "area_m2"])
    if bbox is not None:
        w, s, e, n = bbox
        m = ((cols["center_lon"] >= w - pad) & (cols["center_lon"] <= e + pad)
             & (cols["center_lat"] >= s - pad) & (cols["center_lat"] <= n + pad))
        cols = {k: v[m] for k, v in cols.items()}
        geoms = geoms[m]
    return cols, to_m(geoms)


def _park_coverage() -> tuple[dict[str, int], np.ndarray]:
    """Coverage polygons of kind 'park' (not whole-city / county extents): {coverage_id: index}, Albers geometries."""
    if not COVERAGE_FILE.exists():
        return {}, np.array([], dtype=object)
    cc, cg = read_geoparquet(COVERAGE_FILE)
    keep = np.flatnonzero(cc["kind"] == "park")
    return {c: i for i, c in enumerate(cc["coverage_id"][keep])}, to_m(cg[keep])


# --------------------------------------------------------------------------- osm-compare
def osm_compare(radius: float, park_radius: float) -> dict:
    cols, _ = load_truth()
    n = len(cols["truth_id"])
    bbox = (float(cols["lon"].min()), float(cols["lat"].min()), float(cols["lon"].max()), float(cols["lat"].max()))
    ocols, ogeoms = load_osm(bbox)
    log.info("OSM: %d basketball features near the truth sources (%s)", len(ogeoms), OSM_COURTS_FILE.name)
    tg = truth_geoms_m(cols)
    tree = STRtree(ogeoms)
    near_i, near_d = np.full(n, -1), np.full(n, np.inf)
    ni, oi = tree.query_nearest(tg, max_distance=2000.0, return_distance=False, all_matches=False)
    d = shapely.distance(tg[ni], ogeoms[oi])
    near_i[ni], near_d[ni] = oi, d
    ti, oj = tree.query(tg, predicate="dwithin", distance=radius)
    n_within = np.bincount(ti, minlength=n)
    # park-level: any OSM basketball feature inside the same coverage polygon (or within park_radius)
    park_match = np.zeros(n, dtype=bool)
    cov_index, cov_m = _park_coverage()
    cov_has_osm: dict[str, bool] = {}
    for i in range(n):
        cid = cols["coverage_id"][i]
        if cid is not None and cid in cov_index:
            if cid not in cov_has_osm:
                cov_has_osm[cid] = len(tree.query(cov_m[cov_index[cid]], predicate="intersects")) > 0
            park_match[i] = cov_has_osm[cid]
        else:
            park_match[i] = near_d[i] <= park_radius
    park_match |= near_d <= radius
    refs = np.where(near_i >= 0, ocols["osm_ref"][np.maximum(near_i, 0)], None)
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "truth_id": pa.array(cols["truth_id"]),
        "nearest_osm_ref": pa.array(refs.tolist()),
        "nearest_osm_m": pa.array(np.where(np.isfinite(near_d), np.round(near_d, 1), np.nan)),
        "n_osm_within_r": pa.array(n_within.astype(np.int32)),
        "osm_within_r": pa.array(n_within > 0),
        "osm_in_same_park": pa.array(park_match),
        "radius_m": pa.array(np.full(n, radius)),
    }), OSM_MATCH_FILE, compression="zstd")

    def summarise(mask: np.ndarray) -> dict:
        k = int(mask.sum())
        if not k:
            return {"rows": 0}
        courts = np.array([c if c is not None and not (isinstance(c, float) and math.isnan(c)) else 1
                           for c in cols["courts"][mask]], dtype=float)
        courts = np.maximum(courts, 1)
        hit = n_within[mask] > 0
        return {
            "rows": k,
            "osm_within_r": int(hit.sum()),
            "share": round(float(hit.mean()), 3),
            "courts_estimate": int(courts.sum()),
            "court_weighted_share": round(float(np.minimum(courts, n_within[mask]).sum() / courts.sum()), 3),
            "osm_in_same_park_share": round(float(park_match[mask].mean()), 3),
            "median_nearest_osm_m": round(float(np.median(near_d[mask][np.isfinite(near_d[mask])])), 1)
            if np.isfinite(near_d[mask]).any() else None,
        }

    base = np.isin(cols["status"], LIVE_STATUS) & (cols["feature"] == "court") & ~np.isin(cols["indoor"], HIDDEN)
    out: dict[str, Any] = {"radius_m": radius, "park_radius_m": park_radius,
                           "osm_file": str(OSM_COURTS_FILE.relative_to(config.PIPELINE_ROOT)),
                           "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                           "sources": {}}
    for key in sorted(set(cols["source"].tolist())):
        m = base & (cols["source"] == key)
        prec = Counter(cols["precision"][m].tolist())
        entry = {"precision": dict(prec), **summarise(m)}
        hoops = (cols["source"] == key) & (cols["feature"] == "hoop") & np.isin(cols["status"], LIVE_STATUS)
        if hoops.any():
            entry["hoops"] = summarise(hoops)
        area = cols["area_m2"].astype(float)
        if (m & np.isfinite(area)).any():  # court polygons: also without backyard pads / half-court keys
            entry["area_ge_150m2"] = summarise(m & np.isfinite(area) & (area >= 150))
        inactive = (cols["source"] == key) & (cols["status"] == "inactive") & (cols["feature"] == "court")
        if inactive.any():
            entry["inactive"] = summarise(inactive)
        out["sources"][key] = entry
    official = base & (cols["tier"] == "official")
    out["all_official"] = summarise(official)
    out["all_official_court_level"] = summarise(official & np.isin(cols["precision"], COURT_LEVEL))
    write_json(OSM_COMPARE_FILE, out)
    if MANIFEST_FILE.exists():
        man = read_json(MANIFEST_FILE)
        for key, v in out["sources"].items():
            if key in man["sources"]:
                man["sources"][key]["osm"] = {"radius_m": radius, **v}
        man["totals"]["osm_all_official"] = out["all_official"]
        write_json(MANIFEST_FILE, man)
    return out


def print_osm_table(res: dict) -> None:
    print(f"\nOSM coverage of official courts (match = an OSM basketball feature within {res['radius_m']:.0f} m "
          f"of the court point/polygon)")
    print(f"{'source':26} {'precision':16} {'rows':>6} {'in OSM':>7} {'share':>6} {'court-wtd':>9} {'same park':>9} "
          f"{'med m':>6}")
    for key, e in res["sources"].items():
        if not e.get("rows"):
            continue
        prec = ",".join(f"{k}" for k in e["precision"])
        med = e["median_nearest_osm_m"]
        print(f"{key:26} {prec[:16]:16} {e['rows']:6d} {e['osm_within_r']:7d} {e['share']:6.2f} "
              f"{e['court_weighted_share']:9.2f} {e['osm_in_same_park_share']:9.2f} "
              f"{(med if med is not None else float('nan')):6.0f}")
        big = e.get("area_ge_150m2") or {}
        if big.get("rows") and big["rows"] < e["rows"]:
            print(f"{'  polygons >= 150 m2':26} {'':16} {big['rows']:6d} {big['osm_within_r']:7d} {big['share']:6.2f} "
                  f"{big['court_weighted_share']:9.2f} {big['osm_in_same_park_share']:9.2f}")
    a = res["all_official"]
    print(f"{'ALL official':26} {'':16} {a['rows']:6d} {a['osm_within_r']:7d} {a['share']:6.2f} "
          f"{a['court_weighted_share']:9.2f} {a['osm_in_same_park_share']:9.2f}")
    a = res["all_official_court_level"]
    if a.get("rows"):
        print(f"{'  court-level only':26} {'':16} {a['rows']:6d} {a['osm_within_r']:7d} {a['share']:6.2f} "
              f"{a['court_weighted_share']:9.2f} {a['osm_in_same_park_share']:9.2f}")


# --------------------------------------------------------------------------- evaluate
def load_candidates(path: Path, conf_col: str | None) -> dict[str, np.ndarray]:
    """Candidates / detections from GeoParquet, parquet with lon/lat, or GeoJSON -> lon, lat, conf (+ state)."""
    path = Path(path)
    out: dict[str, np.ndarray] = {}
    if path.suffix.lower() == ".parquet":
        schema = pq.read_schema(path)
        names = schema.names
        conf = conf_col or next((c for c in ("confidence", "conf", "score", "conf2") if c in names), None)
        if conf is None:
            raise SystemExit(f"{path}: no confidence column (use --conf-col); columns: {names}")
        extra = [c for c in ("state", "near_known_osm") if c in names]
        if "lon" in names and "lat" in names:
            t = pq.read_table(path, columns=["lon", "lat", conf] + extra)
            out["lon"] = t.column("lon").to_numpy().astype(float)
            out["lat"] = t.column("lat").to_numpy().astype(float)
        elif "geometry" in names:
            t = pq.read_table(path, columns=["geometry", conf] + extra)
            g = shapely.centroid(shapely.from_wkb(t.column("geometry").to_numpy(zero_copy_only=False)))
            out["lon"], out["lat"] = shapely.get_x(g), shapely.get_y(g)
        else:
            raise SystemExit(f"{path}: needs lon/lat columns or a WKB geometry column")
        out["conf"] = t.column(conf).to_numpy(zero_copy_only=False).astype(float)
        for c in extra:
            out[c] = t.column(c).to_numpy(zero_copy_only=False)
    else:
        with open(path, encoding="utf-8") as fh:
            fc = json.load(fh)
        feats = fc["features"]
        props0 = feats[0]["properties"] if feats else {}
        conf = conf_col or next((c for c in ("confidence", "conf", "score") if c in props0), None)
        if conf is None:
            raise SystemExit(f"{path}: no confidence property (use --conf-col)")
        g = shapely.centroid(np.asarray([shape(f["geometry"]) for f in feats], dtype=object))
        out["lon"], out["lat"] = shapely.get_x(g), shapely.get_y(g)
        out["conf"] = np.asarray([float(f["properties"].get(conf) or 0) for f in feats])
        if "state" in props0:
            out["state"] = np.asarray([f["properties"].get("state") for f in feats], dtype=object)
    log.info("%s: %d candidates (confidence column %r)", path.name, len(out["conf"]), conf)
    return out


def evaluate(cand_path: Path, conf_col: str | None, thresholds: list[float], radius: float, poly_radius: float,
             park_radius: float, states: list[str] | None, sources: list[str] | None, include_weak: bool,
             scanned_area: Path | None, only_not_in_osm: bool = False, min_area: float = 0.0) -> dict:
    cand = load_candidates(cand_path, conf_col)
    cols, _ = load_truth()
    tm = np.isin(cols["status"], LIVE_STATUS) & (cols["feature"] == "court") & ~np.isin(cols["indoor"], HIDDEN)
    if not include_weak:
        tm &= cols["tier"] == "official"
    if sources:
        tm &= np.isin(cols["source"], sources)
    if states is None and "state" in cand:
        states = sorted({str(s) for s in cand["state"] if s})
    if states:
        tm &= np.isin(cols["state"], states)
    else:  # no state info: restrict to the candidates' bounding box
        w, e = np.nanmin(cand["lon"]), np.nanmax(cand["lon"])
        s, n = np.nanmin(cand["lat"]), np.nanmax(cand["lat"])
        tm &= (cols["lon"] >= w) & (cols["lon"] <= e) & (cols["lat"] >= s) & (cols["lat"] <= n)
    if scanned_area is not None:
        sa = _load_polygons(scanned_area)
        tm &= shapely.intersects_xy(shapely.union_all(sa), cols["lon"], cols["lat"])
    if min_area > 0:  # planimetric "all visible courts" layers include backyard pads and half-court keys
        area = cols["area_m2"].astype(float)
        tm &= ~(np.isfinite(area) & (area < min_area))
    if only_not_in_osm:  # courts OSM lacks were never training positives: the honest subset outside Tennessee
        if not OSM_MATCH_FILE.exists():
            raise SystemExit("--only-not-in-osm needs `truth osm-compare` first")
        om = pq.read_table(OSM_MATCH_FILE, columns=["truth_id", "n_osm_within_r"]).to_pydict()
        in_osm = {t for t, k in zip(om["truth_id"], om["n_osm_within_r"]) if k > 0}
        tm &= ~np.isin(cols["truth_id"], list(in_osm))
    idx = np.flatnonzero(tm)
    res: dict[str, Any] = {"candidates": str(cand_path), "n_candidates": int(len(cand["conf"])), "states": states,
                           "radius_m": radius, "poly_radius_m": poly_radius, "park_radius_m": park_radius,
                           "only_not_in_osm": only_not_in_osm, "min_area_m2": min_area, "thresholds": thresholds,
                           "sources": {}}
    if not len(idx):
        log.warning("no truth courts in the candidates' area (states=%s)", states)
        return res
    if "near_known_osm" in cand:
        res["note"] = "candidates file has near_known_osm: detections near OSM courts are included (good)"
    cp = pts_m(cand["lon"], cand["lat"])
    ctree = STRtree(cp)
    tg_all = truth_geoms_m({k: v[idx] for k, v in cols.items()})
    is_poly = np.array([b is not None for b in cols["footprint_wkb"][idx]])
    # best candidate confidence per truth row (court-level match)
    best = np.zeros(len(idx))
    for poly_flag, r in ((False, radius), (True, poly_radius)):
        sel = np.flatnonzero(is_poly == poly_flag)
        if not len(sel):
            continue
        ti, ci = ctree.query(tg_all[sel], predicate="dwithin", distance=r)
        if len(ti):
            np.maximum.at(best, sel[ti], cand["conf"][ci])
    # park-level match for park_point rows: any candidate in the same coverage polygon (or within park_radius)
    cov_index, cov_m = _park_coverage()
    best_park = best.copy()
    for j, i in enumerate(idx):
        if cols["precision"][i] != "park_point":
            continue
        cid = cols["coverage_id"][i]
        if cid in cov_index:
            hits = ctree.query(cov_m[cov_index[cid]], predicate="intersects")
        else:
            hits = ctree.query(tg_all[j], predicate="dwithin", distance=park_radius)
        if len(hits):
            best_park[j] = max(best_park[j], float(cand["conf"][hits].max()))
    for key in sorted(set(cols["source"][idx].tolist())):
        sm = cols["source"][idx] == key
        prec = cols["precision"][idx][sm]
        court_level = np.isin(prec, COURT_LEVEL)
        entry: dict[str, Any] = {"truth_rows": int(sm.sum()), "precision": dict(Counter(prec.tolist()))}
        if court_level.any():
            b = best[sm][court_level]
            entry["recall"] = {f"{t:.2f}": round(float((b >= t).mean()), 3) for t in thresholds}
            entry["recall_rows"] = int(court_level.sum())
        if (~court_level).any():
            b = best_park[sm][~court_level]
            entry["park_level_recall"] = {f"{t:.2f}": round(float((b >= t).mean()), 3) for t in thresholds}
            entry["park_level_rows"] = int((~court_level).sum())
        # detections inside covered parks: share matching an official court of this source
        spec_cov = [c for c in cov_index if c.startswith(key + ":")]
        if spec_cov and court_level.any():
            src_all = (cols["source"] == key) & (cols["status"] != "removed")
            sg = truth_geoms_m({k: v[src_all] for k, v in cols.items()})
            s_poly = np.array([b is not None for b in cols["footprint_wkb"][src_all]])
            cov_union = shapely.union_all(cov_m[[cov_index[c] for c in spec_cov]])
            shapely.prepare(cov_union)
            inside = np.flatnonzero(shapely.contains(cov_union, cp))
            matched = np.zeros(len(inside), dtype=bool)
            if len(inside):
                stree = STRtree(sg)
                for poly_flag, r in ((False, radius), (True, poly_radius)):
                    ci, si = stree.query(cp[inside], predicate="dwithin", distance=r)
                    ok = s_poly[si] == poly_flag
                    matched[ci[ok]] = True
            entry["in_covered_parks"] = {}
            for t in thresholds:
                m = cand["conf"][inside] >= t
                k = int(m.sum())
                entry["in_covered_parks"][f"{t:.2f}"] = {
                    "detections": k, "match_official": int(matched[m].sum()),
                    "share": round(float(matched[m].mean()), 3) if k else None}
        res["sources"][key] = entry
    allb = best[np.isin(cols["precision"][idx], COURT_LEVEL)]
    res["all_court_level_recall"] = {f"{t:.2f}": round(float((allb >= t).mean()), 3) for t in thresholds} \
        if len(allb) else None
    res["all_court_level_rows"] = int(len(allb))
    return res


def _load_polygons(path: Path) -> np.ndarray:
    if path.suffix.lower() == ".parquet":
        _, g = read_geoparquet(path)
        return g
    with open(path, encoding="utf-8") as fh:
        fc = json.load(fh)
    return np.asarray([shape(f["geometry"]) for f in fc["features"]], dtype=object)


def print_eval(res: dict) -> None:
    th = [f"{t:.2f}" for t in res["thresholds"]]
    print(f"\nRecall of official courts in {res['candidates']} (court match within {res['radius_m']:.0f} m of a point "
          f"/ {res['poly_radius_m']:.0f} m of a court polygon)")
    print(f"{'source':26} {'rows':>5} " + " ".join(f"{t:>6}" for t in th))
    for key, e in res["sources"].items():
        if "recall" in e:
            print(f"{key:26} {e['recall_rows']:5d} " + " ".join(f"{e['recall'][t]:6.2f}" for t in th))
        if "park_level_recall" in e:
            print(f"{key + ' (park-level)':26} {e['park_level_rows']:5d} "
                  + " ".join(f"{e['park_level_recall'][t]:6.2f}" for t in th))
    if res.get("all_court_level_recall"):
        print(f"{'ALL court-level':26} {res['all_court_level_rows']:5d} "
              + " ".join(f"{res['all_court_level_recall'][t]:6.2f}" for t in th))
    rows = [(k, e["in_covered_parks"]) for k, e in res["sources"].items() if "in_covered_parks" in e]
    if rows:
        print("\nDetections inside covered parks that match an official court (matched / detections)")
        print(f"{'source':26} " + " ".join(f"{t:>11}" for t in th))
        for key, cp in rows:
            print(f"{key:26} " + " ".join(f"{cp[t]['match_official']:>5}/{cp[t]['detections']:<5}" for t in th))


# --------------------------------------------------------------------------- export-labels
def export_labels(out: Path, radius: float, min_area: float = 0.0) -> int:
    cols, _ = load_truth()
    if not OSM_MATCH_FILE.exists():
        raise SystemExit("run `truth osm-compare` first")
    m = pq.read_table(OSM_MATCH_FILE).to_pydict()
    near = dict(zip(m["truth_id"], m["nearest_osm_m"]))
    nwr = dict(zip(m["truth_id"], m["n_osm_within_r"]))
    feats = []
    for i, tid in enumerate(cols["truth_id"]):
        if not (cols["tier"][i] == "official" and cols["status"][i] in ("active", "unknown") and cols["feature"][i] == "court"
                and cols["indoor"][i] not in HIDDEN and cols["precision"][i] in ("court_polygon", "court_point")
                and cols["public"][i] is not False):
            continue
        d = near.get(tid)
        if nwr.get(tid, 0) > 0 or (d is not None and not math.isnan(d) and d <= radius):
            continue
        area = cols["area_m2"][i]
        has_area = area is not None and not (isinstance(area, float) and math.isnan(area))
        if min_area > 0 and has_area and area < min_area:
            continue
        fp = cols["footprint_wkb"][i]
        geom = mapping(shapely.from_wkb(fp)) if fp is not None else {"type": "Point", "coordinates": [
            round(float(cols["lon"][i]), 7), round(float(cols["lat"][i]), 7)]}
        courts = cols["courts"][i]
        feats.append({"type": "Feature", "geometry": geom, "properties": {
            "truth_id": tid, "source": cols["source"][i], "city": cols["city"][i], "state": cols["state"][i],
            "name": cols["name"][i], "park_name": cols["park_name"][i],
            "courts": None if courts is None or (isinstance(courts, float) and math.isnan(courts)) else int(courts),
            "full_half": cols["full_half"][i], "precision": cols["precision"][i],
            "area_m2": round(float(area), 1) if has_area else None, "public": cols["public"][i],
            "lon": round(float(cols["lon"][i]), 7), "lat": round(float(cols["lat"][i]), 7),
            "nearest_osm_m": None if d is None or math.isnan(d) else float(d),
            "needs_snap": True}})
    write_json(out, {"type": "FeatureCollection", "metadata": {
        "what": "official courts with no OSM basketball feature within radius_m; candidate extra positives",
        "radius_m": radius, "min_area_m2": min_area, "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "next_step": "snap each to a visible court box on NAIP before using as a label",
        "by_source": dict(Counter(f["properties"]["source"] for f in feats))}, "features": feats}, indent=None)
    log.info("%d candidate extra positives -> %s (%s)", len(feats), out,
             dict(Counter(f["properties"]["source"] for f in feats)))
    return len(feats)


# --------------------------------------------------------------------------- chips
def chips(sources: list[str] | None, per_source: int, size: int, out_dir: Path, seed: int, only_unmatched: bool) -> None:
    from PIL import Image, ImageDraw

    from .naip import from_lonlat, read_chip

    cols, _ = load_truth()
    ocols, ogeoms_m = load_osm()
    _, ogeoms = read_geoparquet(OSM_COURTS_FILE, columns=["osm_ref"])
    otree = STRtree(ogeoms_m)
    unmatched = set()
    if only_unmatched and OSM_MATCH_FILE.exists():
        m = pq.read_table(OSM_MATCH_FILE).to_pydict()
        unmatched = {t for t, k in zip(m["truth_id"], m["n_osm_within_r"]) if k == 0}
    rng = random.Random(seed)
    keys = sources or sorted(set(cols["source"].tolist()))
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for key in keys:
        pool = [i for i in np.flatnonzero((cols["source"] == key) & np.isin(cols["status"], LIVE_STATUS)
                                          & (cols["feature"] == "court"))
                if not only_unmatched or cols["truth_id"][i] in unmatched]
        rng.shuffle(pool)
        tiles = []
        for i in pool[:per_source]:
            lon, lat = float(cols["lon"][i]), float(cols["lat"][i])
            try:
                chip = read_chip(lat, lon, size_px=size)
            except Exception as exc:
                log.warning("%s: no chip at %.6f,%.6f (%s)", cols["truth_id"][i], lat, lon, exc)
                continue
            img = Image.fromarray(chip.rgb).convert("RGB")
            dr = ImageDraw.Draw(img)
            fwd = from_lonlat(chip.crs)
            inv = ~chip.transform

            def px(xs, ys, fwd=fwd, inv=inv):
                x, y = fwd.transform(np.asarray(xs), np.asarray(ys))
                return [inv * (a, b) for a, b in zip(x, y)]

            p = shapely.points(*_TO_M.transform(lon, lat))
            for j in otree.query(p, predicate="dwithin", distance=size * chip.gsd):
                g = ogeoms[j]
                for part in getattr(g, "geoms", [g]):
                    ring = part.exterior.coords if part.geom_type == "Polygon" else part.coords
                    xy = np.asarray(ring)
                    pts = px(xy[:, 0], xy[:, 1])
                    if len(pts) > 1:
                        dr.line(pts + [pts[0]], fill=(60, 140, 255), width=1)
                    else:
                        dr.ellipse([pts[0][0] - 3, pts[0][1] - 3, pts[0][0] + 3, pts[0][1] + 3], outline=(60, 140, 255))
            fp = cols["footprint_wkb"][i]
            if fp is not None:
                g = shapely.from_wkb(fp)
                for part in getattr(g, "geoms", [g]):
                    xy = np.asarray(part.exterior.coords)
                    pts = px(xy[:, 0], xy[:, 1])
                    dr.line(pts + [pts[0]], fill=(255, 40, 40), width=1)
            (cx, cy), = px([lon], [lat])
            dr.line([cx - 6, cy, cx + 6, cy], fill=(255, 40, 40), width=1)
            dr.line([cx, cy - 6, cx, cy + 6], fill=(255, 40, 40), width=1)
            name = re.sub(r"[^A-Za-z0-9_-]", "_", str(cols["truth_id"][i]))[:80]
            path = out_dir / key / f"{name}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            img.save(path)
            tiles.append((img, cols["truth_id"][i]))
            index.append({"truth_id": cols["truth_id"][i], "png": str(path.relative_to(out_dir)),
                          "lat": lat, "lon": lon, "naip_item": chip.item.id, "naip_date": str(chip.item.datetime)[:10],
                          "precision": cols["precision"][i], "park_name": cols["park_name"][i]})
        if tiles:
            ncol = min(4, len(tiles))
            nrow = math.ceil(len(tiles) / ncol)
            sheet = Image.new("RGB", (ncol * size, nrow * (size + 14)), (255, 255, 255))
            d2 = ImageDraw.Draw(sheet)
            for k, (img, tid) in enumerate(tiles):
                x, y = (k % ncol) * size, (k // ncol) * (size + 14)
                sheet.paste(img, (x, y + 14))
                d2.text((x + 2, y + 1), str(tid)[:48], fill=(0, 0, 0))
            sheet.save(out_dir / f"sheet_{key}.jpg", quality=90)
            log.info("%s: %d chips -> %s", key, len(tiles), out_dir / f"sheet_{key}.jpg")
    write_json(out_dir / "index.json", index)


# --------------------------------------------------------------------------- CLI
def cmd_list(args: argparse.Namespace) -> None:
    sources = load_registry()
    man = read_json(MANIFEST_FILE)["sources"] if MANIFEST_FILE.exists() else {}
    print(f"{'key':26} {'tier':8} {'precision':14} {'license':18} {'rows':>6} {'courts':>6} {'OSM':>5}  coverage")
    for s in sources:
        m = man.get(s["key"], {})
        lic = (s.get("license") or {}).get("status", "?")
        osm_e = m.get("osm") or {}
        osm = osm_e.get("osm_in_same_park_share" if s["precision"] == "park_point" else "share")  # park rows: same park
        print(f"{s['key']:26} {s.get('tier', 'official'):8} {s['precision']:14} {lic[:18]:18} "
              f"{m.get('rows', 0):6d} {m.get('courts_estimate', 0):6d} "
              f"{(f'{osm:.2f}' if osm is not None else '-'):>5}  "
              f"{(s.get('coverage') or {}).get('complete', '-')[:60]}{'' if s.get('enabled', True) else '  [disabled]'}")


def cmd_refresh(args: argparse.Namespace) -> None:
    sources = pick_sources(load_registry(), args.sources, args.include_disabled)
    sess = http_session()
    failed = []
    for spec in sources:
        try:
            refresh_source(sess, spec, force=args.force)
        except Exception as exc:
            log.error("%s: download failed: %s", spec["key"], exc)
            failed.append(spec["key"])
        time.sleep(PAGE_SLEEP_S)
    if not args.no_build:
        build(pick_sources(load_registry(), None, args.include_disabled))
    if failed:
        log.error("failed sources (re-run with --sources %s): %s", ",".join(failed), failed)
        sys.exit(1)


def cmd_build(args: argparse.Namespace) -> None:
    man = build(pick_sources(load_registry(), args.sources, args.include_disabled))
    print(json.dumps(man["totals"], indent=1))


def cmd_osm_compare(args: argparse.Namespace) -> None:
    res = osm_compare(args.radius, args.park_radius)
    print_osm_table(res)


def cmd_evaluate(args: argparse.Namespace) -> None:
    res = evaluate(Path(args.candidates), args.conf_col, args.thresholds, args.radius, args.poly_radius,
                   args.park_radius, args.states, args.sources, args.include_weak,
                   Path(args.scanned_area) if args.scanned_area else None, args.only_not_in_osm, args.min_area)
    out = Path(args.out) if args.out else config.OUT_DIR / f"truth_eval_{Path(args.candidates).stem}.json"
    write_json(out, res)
    print_eval(res)
    print(f"\n-> {out}")


def cmd_export_labels(args: argparse.Namespace) -> None:
    export_labels(Path(args.out), args.radius, args.min_area)


def cmd_chips(args: argparse.Namespace) -> None:
    chips(args.sources, args.per_source, args.size, Path(args.out), args.seed, args.only_unmatched)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m hoop_pipeline.truth", description=__doc__.split("\n\n")[0])
    add_common_args(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="registry and manifest summary")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("refresh", help="download sources (courts + coverage) and rebuild")
    p.add_argument("--sources", nargs="*", help="source keys (default: all enabled)")
    p.add_argument("--force", action="store_true", help="re-download even if raw files exist")
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--include-disabled", action="store_true")
    p.set_defaults(func=cmd_refresh)
    p = sub.add_parser("build", help="normalise raw downloads (no network)")
    p.add_argument("--sources", nargs="*")
    p.add_argument("--include-disabled", action="store_true")
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("osm-compare", help="share of official courts OSM already has")
    p.add_argument("--radius", type=float, default=config.KNOWN_COURT_RADIUS_M)
    p.add_argument("--park-radius", type=float, default=250.0,
                   help="park-level match radius for park_point rows without a coverage polygon")
    p.set_defaults(func=cmd_osm_compare)
    p = sub.add_parser("evaluate", help="recall / in-park match share of a candidates file")
    p.add_argument("--candidates", required=True, help="GeoParquet, parquet with lon/lat, or GeoJSON")
    p.add_argument("--conf-col", default=None)
    p.add_argument("--thresholds", type=float, nargs="+", default=[0.1, 0.25, 0.4, 0.5, 0.6, 0.7])
    p.add_argument("--radius", type=float, default=config.KNOWN_COURT_RADIUS_M, help="match radius to a court point")
    p.add_argument("--poly-radius", type=float, default=15.0, help="match radius to a court polygon")
    p.add_argument("--park-radius", type=float, default=250.0)
    p.add_argument("--states", nargs="*", default=None, help="restrict truth to these states (default: from file)")
    p.add_argument("--sources", nargs="*", default=None)
    p.add_argument("--include-weak", action="store_true", help="also score weak sources (Overture)")
    p.add_argument("--scanned-area", default=None, help="polygons (GeoJSON/GeoParquet) the scan covered")
    p.add_argument("--min-area", type=float, default=0.0,
                   help="drop court polygons smaller than this (m2), e.g. 150 for planimetric layers with backyard pads")
    p.add_argument("--only-not-in-osm", action="store_true",
                   help="score only official courts with no OSM court within the osm-compare radius (never training positives)")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_evaluate)
    p = sub.add_parser("export-labels", help="official courts missing from OSM -> candidate positives")
    p.add_argument("--out", default=str(LABELS_FILE))
    p.add_argument("--radius", type=float, default=config.KNOWN_COURT_RADIUS_M)
    p.add_argument("--min-area", type=float, default=0.0, help="skip court polygons smaller than this (m2)")
    p.set_defaults(func=cmd_export_labels)
    p = sub.add_parser("chips", help="NAIP chips around sampled truth courts (CPU)")
    p.add_argument("--sources", nargs="*", default=None)
    p.add_argument("--per-source", type=int, default=4)
    p.add_argument("--size", type=int, default=256)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--only-unmatched", action="store_true", help="only courts with no OSM court nearby")
    p.add_argument("--out", default=str(TRUTH_DIR / "chips"))
    p.set_defaults(func=cmd_chips)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
