"""Public / institutional site layer and residential context for the privacy filter (privacy.py).

A court is only shown in the app when it sits on a site the public can use. This module
builds, for the contiguous US (or a few states), from open data:

``sites_<scope>.parquet``  (GeoParquet, lon/lat) one row per site feature:
    site      park | school | college | church | rec | community | restricted
    source    osm | padus | nces_public | nces_private | ipeds | overture
    private   the site itself is tagged access=private/no/... (an HOA park, a members' club)
    geom_kind polygon | point;  buffer_m: radius used for point sites (0 for polygons)
``residential_<scope>.parquet``  OSM landuse=residential polygons (kind residential / apartments)
    and building=apartments|dormitory polygons (apartments_bldg)
``buildings_<ST>.parquet``  Overture buildings: centroid, footprint area, subtype/class

Sources and licenses (recorded in ``data/sites/sources.json``):

* OpenStreetMap (Geofabrik extract via osm_extract): parks, playgrounds, recreation grounds,
  sports centres, schools, colleges/universities, community centres, places of worship,
  landuse=education/religious/recreation_ground/residential/military, prisons. **ODbL 1.0.**
* USGS PAD-US 4.1 Fee layer (national File Geodatabase): publicly owned or managed land
  (federal/state/local/district/joint), excluding closed access (Pub_Access=XA); MIL
  designations are restricted. **Public domain** (US Government work). USGS notes PAD-US is
  not meant for mapping finer than 1:24,000, so privacy.py only trusts a PAD-US polygon when
  the court is well inside it.
* NCES EDGE school geocodes: public schools 2024-25, private schools 2023-24 (PSS),
  postsecondary 2025-26 (IPEDS). **Public domain.** Points only.
* Overture Maps places 2026-09-23.1: places of worship, community/civic centres, youth
  organisations, parks, playgrounds, schools, colleges, sports complexes, basketball courts,
  YMCA/YWCA/Boys & Girls Clubs/recreation centres (name match). Licensed **CDLA-Permissive-2.0
  and Apache-2.0** (Foursquare rows; NOTICE at https://opensource.foursquare.com/places-notice-txt/),
  some rows CC0 (AllThePlaces); contains no OSM data. Points only.
* Overture Maps buildings 2026-09-23.1 (OSM + Microsoft + Google + Esri footprints): **ODbL 1.0.**

Point sites get a radius (``POINT_BUFFER_M``), justified by ``buffer-eval``: for OSM courts that
lie inside an OSM school / place-of-worship / ... polygon, the distance from the court to that
site's point (NCES / Overture) is measured, and the radius covers ~90 % of them.

Example
-------
    python -m hoop_pipeline.sites download
    python -m hoop_pipeline.sites overture-places
    python -m hoop_pipeline.sites buildings --states TN
    python -m hoop_pipeline.sites build --states TN      # or CONUS
    python -m hoop_pipeline.sites buffer-eval --states TN
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import logging
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
import shapely
from pyproj import Transformer

from . import config
from .common import add_common_args, setup_logging, write_json
from .geoparquet import read_geoparquet, write_geoparquet
from .osm_tags import PRIVATE_ACCESS, RESIDENTIAL_KINDS, RESTRICTED_KINDS, SITE_OF_KIND
from .wkb_curves import from_wkb_linear

log = logging.getLogger("sites")
SITES_DIR = config.DATA_DIR / "sites"
ALBERS = "EPSG:5070"
CONUS_BBOX = (-125.0, 24.3, -66.8, 49.5)
OVERTURE_RELEASE = "2026-09-23.1"
OVERTURE_S3 = "s3://overturemaps-us-west-2/release/{release}/theme={theme}/type={type}/*"

PADUS_URL = "https://www.sciencebase.gov/catalog/file/get/652d4fc5d34e44db0e2ee45e?name=PADUS4_1Geodatabase.zip"
NCES_URLS = {
    "nces_public": "https://nces.ed.gov/programs/edge/data/EDGE_GEOCODE_PUBLICSCH_2425.zip",
    "nces_private": "https://nces.ed.gov/programs/edge/data/EDGE_GEOCODE_PRIVATESCH_2324.zip",
    "ipeds": "https://nces.ed.gov/programs/edge/data/EDGE_GEOCODE_POSTSECSCH_2526.zip",
}
# Radius around point-only sites (metres); see buffer-eval and README "Privacy".
POINT_BUFFER_M = {"school": 150.0, "college": 250.0, "church": 80.0, "community": 80.0, "rec": 80.0, "park": 80.0}
PADUS_PUBLIC_TYPES = {"FED", "STAT", "LOC", "DIST", "JNT", "TERR"}

SOURCES_INFO = {
    "osm": {"name": "OpenStreetMap (Geofabrik US extract)", "license": "ODbL 1.0",
            "attribution": "(c) OpenStreetMap contributors"},
    "padus": {"name": "USGS Protected Areas Database of the United States (PAD-US) 4.1, Fee layer",
              "url": PADUS_URL, "doi": "https://doi.org/10.5066/P96WBCHS", "license": "Public domain (US Government work)"},
    "nces_public": {"name": "NCES EDGE public school geocodes 2024-25", "url": NCES_URLS["nces_public"],
                    "license": "Public domain"},
    "nces_private": {"name": "NCES EDGE private school geocodes 2023-24 (PSS)", "url": NCES_URLS["nces_private"],
                     "license": "Public domain"},
    "ipeds": {"name": "NCES EDGE postsecondary geocodes 2025-26 (IPEDS)", "url": NCES_URLS["ipeds"],
              "license": "Public domain"},
    "overture": {"name": f"Overture Maps places {OVERTURE_RELEASE}",
                 "license": "CDLA-Permissive-2.0 and Apache-2.0 (Foursquare-sourced rows), some rows CC0-1.0",
                 "url": "https://docs.overturemaps.org/guides/places/"},
    "overture_buildings": {"name": f"Overture Maps buildings {OVERTURE_RELEASE}", "license": "ODbL 1.0",
                           "attribution": "(c) OpenStreetMap contributors, Overture Maps Foundation"},
}


def _tf(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


def _download(url: str, dest: Path, chunk: int = 8 << 20) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": config.USER_AGENT}) as r:
        r.raise_for_status()
        n = 0
        with open(tmp, "wb") as fh:
            for b in r.iter_content(chunk):
                fh.write(b)
                n += len(b)
    tmp.replace(dest)
    log.info("downloaded %s (%.1f MB, %.0f s)", dest.name, n / 1e6, time.monotonic() - t0)
    return dest


# --------------------------------------------------------------------------- download
def cmd_download(args: argparse.Namespace) -> None:
    out = args.dir
    for key, url in NCES_URLS.items():
        _download(url, out / "nces" / f"{key}.zip")
    if not args.no_padus:
        z = _download(PADUS_URL, out / "padus" / "PADUS4_1Geodatabase.zip")
        gdb = out / "padus" / "PADUS4_1Geodatabase.gdb"
        if not gdb.exists():
            with zipfile.ZipFile(z) as zf:
                zf.extractall(out / "padus")
            found = [p for p in (out / "padus").rglob("*.gdb") if p.is_dir()]
            if found and found[0] != gdb:
                found[0].rename(gdb)
    info: dict[str, Any] = dict(SOURCES_INFO)
    info["downloaded"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    write_json(out / "sources.json", info)


# --------------------------------------------------------------------------- readers
def read_nces(path: Path, kind: str) -> dict[str, np.ndarray]:
    """Name / state / lon / lat of every record in an EDGE geocode zip (dbf read with pyshp, nested zips ok)."""
    import shapefile

    def open_dbf(zf: zipfile.ZipFile) -> Any:
        dbfs = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
        if dbfs:
            return shapefile.Reader(dbf=io.BytesIO(zf.read(dbfs[0])))
        inner = [n for n in zf.namelist() if n.lower().endswith(".zip")]
        for n in inner:
            with zipfile.ZipFile(io.BytesIO(zf.read(n))) as z2:
                r = open_dbf(z2)
                if r is not None:
                    return r
        return None

    with zipfile.ZipFile(path) as zf:
        r = open_dbf(zf)
    if r is None:
        raise RuntimeError(f"no dbf in {path}")
    fields = [f[0] for f in r.fields[1:]]
    recs = [dict(zip(fields, rec)) for rec in r.iterRecords()]
    idk = {"nces_public": "NCESSCH", "nces_private": "PPIN", "ipeds": "UNITID"}[kind]
    out = {
        "id": np.array([str(x.get(idk, "")) for x in recs], dtype=object),
        "name": np.array([str(x.get("NAME", "")) for x in recs], dtype=object),
        "state": np.array([str(x.get("STATE", "")) for x in recs], dtype=object),
        "lon": np.array([float(x.get("LON") or np.nan) for x in recs]),
        "lat": np.array([float(x.get("LAT") or np.nan) for x in recs]),
    }
    log.info("%s: %d records", kind, len(recs))
    return out


def read_padus(gdb: Path, bbox: tuple[float, float, float, float] | None) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """PAD-US Fee polygons that are publicly owned/managed and not closed; lon/lat geometries."""
    import pyogrio

    cols = ["Category", "Own_Type", "Mang_Type", "Mang_Name", "Des_Tp", "Unit_Nm", "Pub_Access", "State_Nm"]
    layer = "PADUS4_1Fee"
    info = pyogrio.read_info(gdb, layer=layer)
    crs = info.get("crs") or "ESRI:102039"
    q_bbox = None
    if bbox is not None:  # bbox in lon/lat -> layer CRS
        tr = _tf("EPSG:4326", crs)
        xs, ys = tr.transform([bbox[0], bbox[2], bbox[0], bbox[2]], [bbox[1], bbox[1], bbox[3], bbox[3]])
        q_bbox = (min(xs), min(ys), max(xs), max(ys))
    meta, table = pyogrio.read_arrow(gdb, layer=layer, columns=cols, bbox=q_bbox)
    gcol = meta.get("geometry_name") or "wkb_geometry"
    geoms = from_wkb_linear(table.column(gcol).to_numpy(zero_copy_only=False))  # PAD-US has curve geometries
    a = {c: table.column(c).to_numpy(zero_copy_only=False).astype(str) for c in cols}
    public = np.isin(a["Own_Type"], list(PADUS_PUBLIC_TYPES)) | np.isin(a["Mang_Type"], list(PADUS_PUBLIC_TYPES))
    keep = public & (a["Pub_Access"] != "XA") & (a["Category"] == "Fee")
    geoms = shapely.make_valid(geoms[keep])
    tr = _tf(crs, "EPSG:4326")
    geoms = shapely.transform(geoms, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))
    out = {k: v[keep] for k, v in a.items()}
    log.info("PAD-US Fee: %d features read, %d public & not closed", len(keep), int(keep.sum()))
    return out, geoms


def duckdb_conn() -> Any:
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    return con


PLACE_SQL = """
SELECT id, names.primary AS name, basic_category, taxonomy.primary AS cat,
       array_to_string(taxonomy.hierarchy, '>') AS hier, confidence, operating_status,
       bbox.xmin AS lon, bbox.ymin AS lat,
       array_to_string(list_transform(sources, s -> s.dataset), ';') AS datasets,
       array_to_string(list_transform(sources, s -> s.license), ';') AS licenses
FROM read_parquet('{path}', hive_partitioning = 1)
WHERE bbox.xmin BETWEEN {xmin} AND {xmax} AND bbox.ymin BETWEEN {ymin} AND {ymax}
  AND (list_contains(taxonomy.hierarchy, 'place_of_worship')
       OR taxonomy.primary IN ({cats}) OR basic_category IN ({cats})
       OR regexp_matches(lower(coalesce(names.primary, '')), '{names}'))
"""
OVERTURE_SITE = {
    "park": ["park", "playground", "state_park", "national_park"],
    "school": ["school", "elementary_school", "middle_school", "high_school", "public_school", "private_school",
               "charter_school", "religious_school", "montessori_school", "waldorf_school", "place_of_learning"],
    "college": ["college_university", "campus"],
    "church": ["christian_place_of_worship", "religious_organization", "religious_retreat_or_center",
               "place_of_worship", "church_cathedral"],
    "community": ["community_center", "civic_center", "youth_organization"],
    "rec": ["sports_complex", "basketball_court", "sport_court", "recreation_center"],
}
NAME_RE = (r"\bymca\b|\bywca\b|boys (&|and) girls club|recreation (center|centre)|\brec (center|centre)\b"
           r"|community (center|centre)")


def overture_site(cat: str | None, basic: str | None, hier: str | None, name: str | None) -> str | None:
    import re

    for site, cats in OVERTURE_SITE.items():
        if (cat in cats) or (basic in cats):
            return site
    if hier and "place_of_worship" in hier:
        return "church"
    if name and re.search(NAME_RE, name.lower()):
        nm = name.lower()
        return "community" if "community" in nm else "rec"
    return None


def cmd_overture_places(args: argparse.Namespace) -> None:
    t0 = time.monotonic()
    con = duckdb_conn()
    cats = ",".join(f"'{c}'" for v in OVERTURE_SITE.values() for c in v)
    sql = PLACE_SQL.format(path=OVERTURE_S3.format(release=args.release, theme="places", type="place"),
                           xmin=args.bbox[0], ymin=args.bbox[1], xmax=args.bbox[2], ymax=args.bbox[3], cats=cats,
                           names=NAME_RE.replace("'", "''"))
    table = con.execute(sql).arrow()
    if hasattr(table, "read_all"):
        table = table.read_all()
    d = table.to_pydict()
    site = [overture_site(c, b, h, n) for c, b, h, n in zip(d["cat"], d["basic_category"], d["hier"], d["name"])]
    keep = [s is not None and (cf or 0) >= args.min_confidence and (st or "") != "permanently_closed"
            for s, cf, st in zip(site, d["confidence"], d["operating_status"])]
    idx = np.flatnonzero(keep)
    out = {k: np.asarray(v, dtype=object)[idx] for k, v in d.items()}
    out["site"] = np.asarray(site, dtype=object)[idx]
    geoms = shapely.points(np.asarray(out["lon"], float), np.asarray(out["lat"], float))
    cols = {k: pa.array(v.tolist()) for k, v in out.items() if k not in ("lon", "lat")}
    cols["lon"] = pa.array(np.asarray(out["lon"], float))
    cols["lat"] = pa.array(np.asarray(out["lat"], float))
    path = args.dir / "overture_places.parquet"
    write_geoparquet(path, cols, geoms)
    log.info("Overture places: %d rows matched, %d kept (%s) -> %s in %.0f s", table.num_rows, len(idx),
             dict(Counter(out["site"].tolist())), path, time.monotonic() - t0)


def overture_files(theme: str, type_: str, bbox: tuple[float, float, float, float], release: str = OVERTURE_RELEASE,
                   cache_dir: Path = SITES_DIR) -> list[str]:
    """S3 paths of the Overture files whose STAC bbox intersects ``bbox`` (W, S, E, N).

    Reading only these files avoids scanning the footers of every file of a theme (the buildings
    theme has 512 files); the per-file STAC items are fetched once and cached."""
    from concurrent.futures import ThreadPoolExecutor as _Pool

    cache = cache_dir / f"overture_stac_{release}_{theme}_{type_}.json"
    if cache.exists():
        items = json.loads(cache.read_text(encoding="utf-8"))
    else:
        base = f"https://stac.overturemaps.org/{release}/{theme}/{type_}"
        coll = requests.get(f"{base}/collection.json", timeout=60).json()
        hrefs = [ln["href"] for ln in coll["links"] if ln.get("rel") == "item"]

        def get(h: str) -> dict[str, Any]:
            it = requests.get(h, timeout=60).json()
            return {"bbox": it["bbox"], "href": it["assets"]["aws"]["href"]}

        with _Pool(32) as pool:
            items = list(pool.map(get, hrefs))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(items), encoding="utf-8")
    w, s_, e, n = bbox
    out = []
    for it in items:
        b = it["bbox"]
        if b[0] <= e and b[2] >= w and b[1] <= n and b[3] >= s_:
            key = it["href"].split(".amazonaws.com/", 1)[1]
            out.append(f"s3://overturemaps-us-west-2/{key}")
    return out


BUILDING_SQL = """
SELECT id, subtype, class, height, bbox.xmin AS xmin, bbox.ymin AS ymin, bbox.xmax AS xmax, bbox.ymax AS ymax,
       geometry
FROM read_parquet('{path}', hive_partitioning = 1)
WHERE bbox.xmin >= {xmin} AND bbox.xmax <= {xmax} AND bbox.ymin >= {ymin} AND bbox.ymax <= {ymax}
"""


def buildings_for_state(st: str, release: str, out_dir: Path, states_file: Path, duckdb_threads: int = 8) -> str:
    """Overture buildings of one state -> buildings_<ST>.parquet (centroid, Albers footprint area, subtype, class)."""
    from .states import States

    t0 = time.monotonic()
    states = States.load(states_file)
    con = duckdb_conn()
    con.execute(f"SET threads={int(duckdb_threads)};")
    to_alb = _tf("EPSG:4326", ALBERS)
    geom = states.geometry(st)
    b = shapely.bounds(geom)
    files = overture_files("buildings", "building", (b[0] - 0.01, b[1] - 0.01, b[2] + 0.01, b[3] + 0.01), release, out_dir)
    log.info("%s: %d Overture building files intersect the state", st, len(files))
    sql = BUILDING_SQL.format(path="', '".join(files), xmin=b[0] - 0.01, ymin=b[1] - 0.01, xmax=b[2] + 0.01,
                              ymax=b[3] + 0.01).replace("read_parquet('", "read_parquet(['", 1).replace(
                                  "', hive_partitioning", "'], hive_partitioning", 1)
    table = con.execute(sql).arrow()
    if hasattr(table, "read_all"):
        table = table.read_all()
    g = shapely.from_wkb(table.column("geometry").to_numpy(zero_copy_only=False))
    c = shapely.centroid(g)
    cx, cy = shapely.get_x(c), shapely.get_y(c)
    geom_b = shapely.buffer(geom, 0.01)
    shapely.prepare(geom_b)
    inside = shapely.intersects_xy(geom_b, cx, cy)
    g_alb = shapely.transform(g[inside], lambda xy: np.column_stack(to_alb.transform(xy[:, 0], xy[:, 1])))
    area = shapely.area(g_alb)
    m = pa.array(inside)
    cols = {"id": table.column("id").filter(m), "subtype": table.column("subtype").filter(m),
            "class": table.column("class").filter(m), "height": table.column("height").filter(m),
            "lon": pa.array(cx[inside]), "lat": pa.array(cy[inside]), "area_m2": pa.array(area)}
    path = out_dir / f"buildings_{st}.parquet"
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(pa.table(cols), tmp, compression="zstd")
    tmp.replace(path)
    msg = f"{st}: {int(inside.sum())} Overture buildings ({table.num_rows} in bbox) -> {path} in {time.monotonic() - t0:.0f} s"
    log.info(msg)
    return msg


def cmd_buildings(args: argparse.Namespace) -> None:
    """Overture buildings per state, ``--workers`` states at a time (each its own DuckDB process).
    Tennessee alone took 398 s on an A100 node, so CONUS needs the parallelism (~40 min with 8 workers)."""
    from concurrent.futures import ProcessPoolExecutor

    from .states import States

    codes = sorted(States.load(args.states_file).codes.tolist()) if args.states == ["CONUS"] else args.states
    todo = [c for c in codes if args.overwrite or not (args.dir / f"buildings_{c}.parquet").exists()]
    overture_files("buildings", "building", CONUS_BBOX, args.release, args.dir)  # fetch the STAC index once
    if args.workers <= 1 or len(todo) <= 1:
        for st in todo:
            buildings_for_state(st, args.release, args.dir, args.states_file)
        return
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(buildings_for_state, st, args.release, args.dir, args.states_file,
                            max(2, 32 // args.workers)) for st in todo]
        for f in futs:
            log.info("done: %s", f.result())


# --------------------------------------------------------------------------- build
def cmd_build(args: argparse.Namespace) -> None:
    from .states import States

    t0 = time.monotonic()
    out = args.dir
    states = States.load(args.states_file)
    codes = None if args.states == ["CONUS"] else [s.upper() for s in args.states]
    scope = "us" if codes is None else "_".join(codes)
    bbox: tuple[float, float, float, float] = CONUS_BBOX
    if codes is not None:
        b = shapely.bounds(shapely.union_all([states.geometry(c) for c in codes]))
        bbox = (float(b[0]) - 0.05, float(b[1]) - 0.05, float(b[2]) + 0.05, float(b[3]) + 0.05)
    rows: dict[str, list[Any]] = {k: [] for k in ("site", "source", "name", "private", "geom_kind", "buffer_m", "ref")}
    geoms: list[Any] = []

    def add(site: str, source: str, name: Any, private: bool, gk: str, buf: float, ref: str, g: Any) -> None:
        rows["site"].append(site)
        rows["source"].append(source)
        rows["name"].append(None if name in (None, "", "None") else str(name))
        rows["private"].append(bool(private))
        rows["geom_kind"].append(gk)
        rows["buffer_m"].append(float(buf))
        rows["ref"].append(ref)
        geoms.append(g)

    # ---- OSM
    kinds = list(SITE_OF_KIND) + list(RESTRICTED_KINDS)
    filt: list[Any] = [("kind", "in", kinds)]
    if codes:
        filt.append(("state", "in", codes))
    a, g = read_geoparquet(args.context, columns=["kind", "osm_type", "osm_id", "access", "name", "geom_kind", "center_lon",
                                                  "center_lat", "state"], filters=filt)
    n_osm = 0
    for i in range(len(g)):
        kind = str(a["kind"][i])
        site = "restricted" if kind in RESTRICTED_KINDS else SITE_OF_KIND[kind]
        is_pt = a["geom_kind"][i] != "polygon" or g[i] is None
        geom = shapely.Point(a["center_lon"][i], a["center_lat"][i]) if is_pt else g[i]
        acc = (a["access"][i] or "").lower() if a["access"][i] is not None else ""
        add(site, "osm", a["name"][i], acc in PRIVATE_ACCESS, "point" if is_pt else "polygon",
            POINT_BUFFER_M.get(site, 60.0) if is_pt else 0.0, f"{a['osm_type'][i]}/{a['osm_id'][i]}", geom)
        n_osm += 1
    log.info("OSM sites: %d", n_osm)

    # ---- PAD-US
    gdb = out / "padus" / "PADUS4_1Geodatabase.gdb"
    if gdb.exists():
        pa_attrs, pa_geoms = read_padus(gdb, None if codes is None else bbox)
        for i in range(len(pa_geoms)):
            site = "restricted" if pa_attrs["Des_Tp"][i] == "MIL" else "park"
            add(site, "padus", pa_attrs["Unit_Nm"][i], False, "polygon", 0.0,
                f"{pa_attrs['Des_Tp'][i]}/{pa_attrs['Mang_Type'][i]}/{pa_attrs['Pub_Access'][i]}", pa_geoms[i])
    else:
        log.warning("PAD-US not downloaded (%s); skipping", gdb)

    # ---- NCES
    for key, site in (("nces_public", "school"), ("nces_private", "school"), ("ipeds", "college")):
        z = out / "nces" / f"{key}.zip"
        if not z.exists():
            log.warning("%s missing; skipping", z)
            continue
        d = read_nces(z, key)
        ok = np.isfinite(d["lon"]) & np.isfinite(d["lat"])
        ok &= (d["lon"] >= bbox[0]) & (d["lon"] <= bbox[2]) & (d["lat"] >= bbox[1]) & (d["lat"] <= bbox[3])
        for i in np.flatnonzero(ok).tolist():
            add(site, key, d["name"][i], False, "point", POINT_BUFFER_M[site], d["id"][i],
                shapely.Point(d["lon"][i], d["lat"][i]))

    # ---- Overture places
    op = out / "overture_places.parquet"
    if op.exists():
        o, og = read_geoparquet(op, columns=["id", "name", "site", "lon", "lat"])
        ok = (o["lon"] >= bbox[0]) & (o["lon"] <= bbox[2]) & (o["lat"] >= bbox[1]) & (o["lat"] <= bbox[3])
        for i in np.flatnonzero(ok).tolist():
            add(str(o["site"][i]), "overture", o["name"][i], False, "point", POINT_BUFFER_M[str(o["site"][i])],
                str(o["id"][i]), og[i])
    else:
        log.warning("Overture places missing (%s); skipping", op)

    garr = np.asarray(geoms, dtype=object)
    st = states.assign(shapely.get_x(shapely.centroid(garr)), shapely.get_y(shapely.centroid(garr)))
    keep = np.ones(len(garr), bool) if codes is None else np.isin(st, codes)
    cols = {k: pa.array(np.asarray(v, dtype=object)[keep].tolist()) for k, v in rows.items()}
    cols["state"] = pa.array(st[keep].tolist())
    path = out / f"sites_{scope}.parquet"
    write_geoparquet(path, cols, garr[keep])
    by = Counter(zip(np.asarray(rows["source"])[keep].tolist(), np.asarray(rows["site"])[keep].tolist()))
    log.info("wrote %s: %d sites; %s", path, int(keep.sum()), dict(sorted(by.items())))

    # ---- residential context
    filt = [("kind", "in", list(RESIDENTIAL_KINDS))]
    if codes:
        filt.append(("state", "in", codes))
    r, rg = read_geoparquet(args.context, columns=["kind", "osm_type", "osm_id", "access", "geom_kind", "state"],
                            filters=filt)
    poly = np.array([gk == "polygon" and gg is not None for gk, gg in zip(r["geom_kind"], rg)], dtype=bool)
    rcols = {"kind": pa.array(r["kind"][poly].tolist()),
             "access": pa.array([None if v is None else str(v) for v in r["access"][poly]]),
             "ref": pa.array([f"{t}/{i}" for t, i in zip(r["osm_type"][poly], r["osm_id"][poly])]),
             "state": pa.array(r["state"][poly].tolist())}
    rpath = out / f"residential_{scope}.parquet"
    write_geoparquet(rpath, rcols, rg[poly])
    log.info("wrote %s: %d residential polygons %s; %.0f s", rpath, int(poly.sum()),
             dict(Counter(r["kind"][poly].tolist())), time.monotonic() - t0)


# --------------------------------------------------------------------------- buffer evaluation
def cmd_buffer_eval(args: argparse.Namespace) -> None:
    """How far are courts at a site from that site's point? (justifies POINT_BUFFER_M)

    Courts: OSM basketball/tennis/other pitches. For each court inside an OSM polygon of a site
    type, take the nearest point-source site (NCES for schools/colleges, Overture for churches,
    community/rec, parks) of the same type that also lies inside that polygon, and record the
    court-to-point distance. Also reports how much of each buffer falls on residential landuse.
    """
    scope = "us" if args.states == ["CONUS"] else "_".join(s.upper() for s in args.states)
    s, sg = read_geoparquet(args.dir / f"sites_{scope}.parquet", columns=["site", "source", "geom_kind", "private"])
    to_alb = _tf("EPSG:4326", ALBERS)
    sg_alb = shapely.transform(sg, lambda xy: np.column_stack(to_alb.transform(xy[:, 0], xy[:, 1])))
    c, cg = read_geoparquet(args.courts, columns=["state", "label_ok", "center_lon", "center_lat"])
    sel = c["label_ok"].astype(bool)
    if args.states != ["CONUS"]:
        sel &= np.isin(c["state"].astype(str), [x.upper() for x in args.states])
    cpts = shapely.transform(shapely.points(c["center_lon"][sel], c["center_lat"][sel]),
                             lambda xy: np.column_stack(to_alb.transform(xy[:, 0], xy[:, 1])))
    res: dict[str, Any] = {"courts": int(sel.sum())}
    poly_mask = (s["geom_kind"] == "polygon") & (s["source"] == "osm")
    pt_mask = s["geom_kind"] == "point"
    pidx = np.flatnonzero(poly_mask)
    for site in ("school", "college", "church", "community", "rec", "park"):
        polys = pidx[s["site"][pidx] == site]
        if not len(polys):
            continue
        pts_idx = np.flatnonzero(pt_mask & (s["site"] == site) & (s["source"] != "osm"))
        if not len(pts_idx):
            continue
        site_pts = sg_alb[pts_idx]
        pt_tree = shapely.STRtree(site_pts)
        tree = shapely.STRtree(sg_alb[polys])
        ci, pi = tree.query(cpts, predicate="within")
        dists = []
        for c_, p_ in zip(ci, pi):
            poly = sg_alb[polys[p_]]
            cand = pt_tree.query(poly, predicate="intersects")
            if not len(cand):
                continue
            dists.append(float(shapely.distance(cpts[c_], site_pts[cand]).min()))
        if not dists:
            continue
        dv = np.array(dists)
        res[site] = {"court_site_pairs": len(dv), "p50_m": round(float(np.percentile(dv, 50)), 1),
                     "p75_m": round(float(np.percentile(dv, 75)), 1), "p90_m": round(float(np.percentile(dv, 90)), 1),
                     "p95_m": round(float(np.percentile(dv, 95)), 1), "buffer_m": POINT_BUFFER_M[site],
                     "share_within_buffer": round(float((dv <= POINT_BUFFER_M[site]).mean()), 3)}
    write_json(args.dir / f"buffer_eval_{scope}.json", res)
    log.info("buffer eval: %s", json.dumps(res))


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("download", help="PAD-US national GDB (1.4 GB) + NCES geocodes")
    p.add_argument("--dir", type=Path, default=SITES_DIR)
    p.add_argument("--no-padus", action="store_true")
    add_common_args(p)
    p = sub.add_parser("overture-places")
    p.add_argument("--dir", type=Path, default=SITES_DIR)
    p.add_argument("--release", default=OVERTURE_RELEASE)
    p.add_argument("--bbox", type=float, nargs=4, default=list(CONUS_BBOX), metavar=("W", "S", "E", "N"))
    p.add_argument("--min-confidence", type=float, default=0.4)
    add_common_args(p)
    p = sub.add_parser("buildings")
    p.add_argument("--dir", type=Path, default=SITES_DIR)
    p.add_argument("--release", default=OVERTURE_RELEASE)
    p.add_argument("--states", nargs="+", default=["TN"], help="USPS codes or CONUS")
    p.add_argument("--workers", type=int, default=1, help="states processed in parallel (8 for CONUS)")
    p.add_argument("--overwrite", action="store_true", help="redo states whose buildings file exists")
    p.add_argument("--states-file", type=Path, default=config.STATES_FILE)
    add_common_args(p)
    p = sub.add_parser("build")
    p.add_argument("--dir", type=Path, default=SITES_DIR)
    p.add_argument("--states", nargs="+", default=["TN"], help="USPS codes or CONUS")
    p.add_argument("--context", type=Path, default=config.OSM_US_DIR / "context_us.parquet")
    p.add_argument("--states-file", type=Path, default=config.STATES_FILE)
    add_common_args(p)
    p = sub.add_parser("buffer-eval")
    p.add_argument("--dir", type=Path, default=SITES_DIR)
    p.add_argument("--states", nargs="+", default=["TN"])
    p.add_argument("--courts", type=Path, default=config.OSM_US_DIR / "courts_us.parquet")
    add_common_args(p)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    args.dir.mkdir(parents=True, exist_ok=True)
    {"download": cmd_download, "overture-places": cmd_overture_places, "buildings": cmd_buildings, "build": cmd_build,
     "buffer-eval": cmd_buffer_eval}[args.cmd](args)


if __name__ == "__main__":
    main()
