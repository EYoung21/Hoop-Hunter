"""Nationwide OSM layers from a Geofabrik .osm.pbf extract (osmium-tool + Python).

This replaces per-region Overpass queries for the nationwide workflow. Steps:

1. ``osmium tags-filter`` keeps every object that any rule in ``osm_tags`` can
   match (anything with ``sport``, pitches, parks, parking, pools, schools,
   colleges, places of worship, community/sports centres, recreation grounds,
   apartment landuse), plus the nodes and member ways they reference.
2. ``osmium export`` writes GeoJSON lines. Closed ways and multipolygon
   relations become polygons (osmium assembles multipolygons), tagged nodes
   become points, and unclosed ways become linestrings.
3. Python classifies each feature in parallel (byte-range chunks), assigns a
   US state by point-in-polygon on the Census 1:500k boundaries, keeps the
   contiguous US (48 states + DC), and writes GeoParquet layers.

Outputs in ``--out-dir`` (default ``data/osm_us``):

    courts_us.parquet     every CONUS feature whose sport contains "basketball":
                          geometry (polygon / point / line), bbox center, area,
                          state, outdoor / court_like / label_ok flags, tags JSON
    indoor_us.parquet     indoor-court layer: sports / fitness / community
                          centres and sports halls tagged basketball, plus
                          basketball pitches tagged indoor (column indoor_kind)
    context_us.parquet    hard negatives + scan-mask features, one row per
                          (feature, kind); kinds in osm_tags.MASK_KINDS /
                          NEGATIVE_KINDS. Parking keeps only its center.
    courts_<ST>.geojson   fetch_osm-compatible court file per --state-geojson
    summary.json          counts (total, by state, TN, indoor, context kinds)

Examples
--------
    python -m hoop_pipeline.osm_extract --pbf data/osm_us/us-latest.osm.pbf --state-geojson TN
    python -m hoop_pipeline.osm_extract --geojsonseq data/osm_us/filtered.geojsonseq   # skip osmium

OSM data is (c) OpenStreetMap contributors, ODbL 1.0.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import shutil
import subprocess
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from shapely.geometry import shape

from . import config
from .common import M_PER_DEG_LAT, add_common_args, feature_collection, setup_logging, write_json
from .geoparquet import write_geoparquet
from .osm_tags import (
    OSMIUM_FILTERS,
    POINT_ONLY_KINDS,
    context_kinds,
    court_like,
    indoor_kind,
    is_basketball,
    is_outdoor,
)
from .states import States

log = logging.getLogger("osm_extract")

ATTRIBUTION = "(c) OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"
LABEL_MIN_AREA_M2 = 60.0
LABEL_MAX_AREA_M2 = 6000.0

EXPORT_CONFIG = {
    "attributes": {"type": True, "id": True},
    "linear_tags": False,     # closed ways are polygons only (no duplicate linestring)
    "area_tags": True,
    "format_options": {"print_record_separator": False},
}

try:  # orjson is ~3x faster for the big context layer; json works too
    import orjson

    _loads = orjson.loads
except ImportError:  # pragma: no cover
    _loads = json.loads

_KEYWORDS = (b"basketball", b'"leisure"', b'"amenity"', b'"landuse"', b'"residential"', b'"sport"')


# --------------------------------------------------------------------------- osmium
def _run(cmd: list[str]) -> None:
    log.info("$ %s", " ".join(cmd))
    t0 = time.monotonic()
    subprocess.run(cmd, check=True)
    log.info("  done in %.1f min", (time.monotonic() - t0) / 60)


def osmium_filter(pbf: Path, out_pbf: Path, force: bool = False) -> Path:
    if out_pbf.exists() and not force and out_pbf.stat().st_mtime > pbf.stat().st_mtime:
        log.info("reusing %s", out_pbf)
        return out_pbf
    expr = out_pbf.with_suffix(".filters.txt")
    expr.write_text("\n".join(OSMIUM_FILTERS) + "\n", encoding="utf-8")
    _run(["osmium", "tags-filter", str(pbf), "-e", str(expr), "-o", str(out_pbf), "--overwrite"])
    return out_pbf


def osmium_export(pbf: Path, out_seq: Path, force: bool = False) -> Path:
    if out_seq.exists() and not force and out_seq.stat().st_mtime > pbf.stat().st_mtime:
        log.info("reusing %s", out_seq)
        return out_seq
    cfg = out_seq.with_suffix(".export.json")
    cfg.write_text(json.dumps(EXPORT_CONFIG, indent=1), encoding="utf-8")
    _run(["osmium", "export", str(pbf), "-c", str(cfg), "-f", "geojsonseq", "-o", str(out_seq), "--overwrite"])
    return out_seq


# --------------------------------------------------------------------------- parsing
def _area_m2(geom: Any, lat: float) -> float:
    return float(geom.area) * M_PER_DEG_LAT * M_PER_DEG_LAT * math.cos(math.radians(lat))


def _geom_kind(gtype: str) -> str:
    if gtype in ("Polygon", "MultiPolygon"):
        return "polygon"
    if gtype in ("LineString", "MultiLineString"):
        return "line"
    return "point"


def _orig_ref(props: dict[str, Any]) -> tuple[str, int]:
    t = str(props.pop("@type", "") or "")
    i = int(props.pop("@id", 0) or 0)
    if t == "area":  # libosmium area ids: way -> 2*id, relation -> 2*id+1
        return ("relation", (i - 1) // 2) if i % 2 else ("way", i // 2)
    return t, i


COURT_COLS = ["osm_type", "osm_id", "name", "leisure", "amenity", "sport", "tags", "center_lon", "center_lat",
              "area_m2", "geom_kind", "outdoor", "court_like", "indoor_kind", "wkb"]
CONTEXT_COLS = ["kind", "osm_type", "osm_id", "sport", "center_lon", "center_lat", "area_m2", "geom_kind", "wkb"]


def _parse_chunk(task: tuple[str, int, int, str, int]) -> dict[str, int]:
    """Parse lines whose first byte is in [start, end) and write two parquet parts."""
    path, start, end, part_dir, idx = task
    courts: dict[str, list[Any]] = {c: [] for c in COURT_COLS}
    ctx: dict[str, list[Any]] = {c: [] for c in CONTEXT_COLS}
    n_lines = n_bad = 0
    with open(path, "rb") as fh:
        if start > 0:
            fh.seek(start - 1)
            if fh.read(1) != b"\n":
                fh.readline()
        while fh.tell() < end:
            line = fh.readline()
            if not line:
                break
            n_lines += 1
            if not any(k in line for k in _KEYWORDS):
                continue
            line = line.strip().lstrip(b"\x1e")
            if not line:
                continue
            try:
                feat = _loads(line)
                props = dict(feat.get("properties") or {})
                osm_type, osm_id = _orig_ref(props)
                tags = {k: str(v) for k, v in props.items() if not k.startswith("@")}
                bb = is_basketball(tags)
                kinds = context_kinds(tags, bb)
                if not bb and not kinds:
                    continue
                gj = feat.get("geometry")
                if not gj:
                    continue
                geom = shape(gj)
                if geom.is_empty:
                    continue
                gkind = _geom_kind(geom.geom_type)
                minx, miny, maxx, maxy = geom.bounds
                clon, clat = (minx + maxx) / 2, (miny + maxy) / 2
                area = _area_m2(geom, clat) if gkind == "polygon" else float("nan")
                if bb:
                    courts["osm_type"].append(osm_type)
                    courts["osm_id"].append(osm_id)
                    courts["name"].append(tags.get("name"))
                    courts["leisure"].append(tags.get("leisure"))
                    courts["amenity"].append(tags.get("amenity"))
                    courts["sport"].append(tags.get("sport"))
                    courts["tags"].append(json.dumps(tags, ensure_ascii=False, sort_keys=True))
                    courts["center_lon"].append(clon)
                    courts["center_lat"].append(clat)
                    courts["area_m2"].append(area)
                    courts["geom_kind"].append(gkind)
                    courts["outdoor"].append(is_outdoor(tags))
                    courts["court_like"].append(court_like(tags))
                    courts["indoor_kind"].append(indoor_kind(tags))
                    courts["wkb"].append(shapely.to_wkb(geom))
                if kinds and gkind != "line":
                    if gkind == "polygon":
                        rp = shapely.point_on_surface(geom)
                        rlon, rlat = rp.x, rp.y
                    else:
                        rlon, rlat = clon, clat
                    wkb_full = shapely.to_wkb(geom)
                    for kind in kinds:
                        ctx["kind"].append(kind)
                        ctx["osm_type"].append(osm_type)
                        ctx["osm_id"].append(osm_id)
                        ctx["sport"].append(tags.get("sport"))
                        ctx["center_lon"].append(rlon)
                        ctx["center_lat"].append(rlat)
                        ctx["area_m2"].append(area)
                        ctx["geom_kind"].append(gkind)
                        ctx["wkb"].append(None if kind in POINT_ONLY_KINDS else wkb_full)
            except Exception:  # malformed feature: count and move on
                n_bad += 1
    out_dir = Path(part_dir)
    for name, cols, schema in (("courts", courts, _court_schema()), ("context", ctx, _context_schema())):
        table = pa.table({c: pa.array(cols[c], type=schema.field(c).type) for c in schema.names}, schema=schema)
        pq.write_table(table, out_dir / f"{name}_{idx:04d}.parquet", compression="zstd")
    return {"lines": n_lines, "bad": n_bad, "courts": len(courts["osm_id"]), "context": len(ctx["osm_id"])}


def _court_schema() -> pa.Schema:
    return pa.schema([
        ("osm_type", pa.string()), ("osm_id", pa.int64()), ("name", pa.string()), ("leisure", pa.string()),
        ("amenity", pa.string()), ("sport", pa.string()), ("tags", pa.string()), ("center_lon", pa.float64()),
        ("center_lat", pa.float64()), ("area_m2", pa.float64()), ("geom_kind", pa.string()), ("outdoor", pa.bool_()),
        ("court_like", pa.bool_()), ("indoor_kind", pa.string()), ("wkb", pa.binary()),
    ])


def _context_schema() -> pa.Schema:
    return pa.schema([
        ("kind", pa.string()), ("osm_type", pa.string()), ("osm_id", pa.int64()), ("sport", pa.string()),
        ("center_lon", pa.float64()), ("center_lat", pa.float64()), ("area_m2", pa.float64()),
        ("geom_kind", pa.string()), ("wkb", pa.binary()),
    ])


def parse_geojsonseq(path: Path, part_dir: Path, workers: int) -> dict[str, int]:
    if part_dir.exists():
        shutil.rmtree(part_dir)
    part_dir.mkdir(parents=True)
    size = path.stat().st_size
    n_chunks = max(1, min(4 * workers, size // (8 << 20) + 1))
    bounds = np.linspace(0, size, n_chunks + 1).astype(np.int64)
    tasks = [(str(path), int(bounds[i]), int(bounds[i + 1]), str(part_dir), i) for i in range(n_chunks)]
    log.info("parsing %s (%.2f GB) in %d chunks with %d processes", path.name, size / 1e9, n_chunks, workers)
    total: Counter[str] = Counter()
    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for i, res in enumerate(pool.map(_parse_chunk, tasks)):
            total.update(res)
            if (i + 1) % max(1, n_chunks // 10) == 0:
                log.info("  %d/%d chunks, %s, %.0fs", i + 1, n_chunks, dict(total), time.monotonic() - t0)
    return dict(total)


# --------------------------------------------------------------------------- assembly
def _dedupe_courts(table: pa.Table) -> pa.Table:
    """One row per OSM element; a polygon beats a line beats a point."""
    rank = {"polygon": 0, "line": 1, "point": 2}
    types = table.column("osm_type").to_pylist()
    ids = table.column("osm_id").to_pylist()
    kinds = table.column("geom_kind").to_pylist()
    best: dict[tuple[str, int], int] = {}
    for i, key in enumerate(zip(types, ids)):
        j = best.get(key)
        if j is None or rank[kinds[i]] < rank[kinds[j]]:
            best[key] = i
    keep = sorted(best.values())
    return table.take(pa.array(keep, type=pa.int64()))


def _counts(values: Any) -> dict[str, int]:
    c = Counter(v if v is not None else "" for v in values)
    return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def write_state_geojson(courts: pa.Table, geoms: np.ndarray, state: str, path: Path, region_label: str) -> int:
    """fetch_osm-compatible GeoJSON of one state's basketball features (for export_app)."""
    st = courts.column("state").to_numpy(zero_copy_only=False)
    idx = np.flatnonzero(st == state.upper())
    order = {"node": 0, "way": 1, "relation": 2}
    rows = courts.take(pa.array(idx, type=pa.int64())).to_pylist()
    feats = []
    for row, gi in zip(rows, idx):
        g = geoms[gi]
        g7 = shapely.set_precision(g, 1e-7)
        gj = json.loads(shapely.to_geojson(g if g7.is_empty else g7))
        feats.append({
            "type": "Feature",
            "geometry": gj,
            "properties": {
                "osm_type": row["osm_type"],
                "osm_id": row["osm_id"],
                "osm_ref": f"{row['osm_type']}/{row['osm_id']}",
                "region": region_label,
                "center_lat": round(row["center_lat"], 7),
                "center_lon": round(row["center_lon"], 7),
                "area_m2": None if row["area_m2"] is None or math.isnan(row["area_m2"]) else round(row["area_m2"], 1),
                "has_polygon": row["geom_kind"] == "polygon",
                "state": row["state"],
                "tags": json.loads(row["tags"]),
            },
        })
    feats.sort(key=lambda f: (order.get(f["properties"]["osm_type"], 3), f["properties"]["osm_id"]))
    write_json(path, feature_collection(
        feats, region=region_label, state=state.upper(),
        fetched=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        source="Geofabrik US extract via osm_extract", attribution=ATTRIBUTION,
    ), indent=None)
    return len(feats)


def assemble(part_dir: Path, out_dir: Path, states: States, state_geojson: list[str], pbf_name: str) -> dict[str, Any]:
    t0 = time.monotonic()
    # ---------------- courts
    courts = pq.read_table(sorted(part_dir.glob("courts_*.parquet")))
    n_raw = courts.num_rows
    courts = _dedupe_courts(courts)
    lon = courts.column("center_lon").to_numpy()
    lat = courts.column("center_lat").to_numpy()
    st = states.assign(lon, lat)
    courts = courts.append_column("state", pa.array(st.tolist(), type=pa.string()))
    non_conus = int((st == "").sum())
    courts = courts.filter(pa.array(st != ""))
    courts = courts.append_column(
        "osm_ref", pa.array([f"{t}/{i}" for t, i in zip(courts.column("osm_type").to_pylist(),
                                                           courts.column("osm_id").to_pylist())]))
    area = courts.column("area_m2").to_numpy(zero_copy_only=False)
    gk = courts.column("geom_kind").to_numpy(zero_copy_only=False)
    outdoor = courts.column("outdoor").to_numpy(zero_copy_only=False)
    clike = courts.column("court_like").to_numpy(zero_copy_only=False)
    label_ok = outdoor & clike & (gk == "polygon") & (area >= LABEL_MIN_AREA_M2) & (area <= LABEL_MAX_AREA_M2)
    courts = courts.append_column("has_polygon", pa.array(gk == "polygon"))
    courts = courts.append_column("label_ok", pa.array(label_ok))
    geoms = shapely.from_wkb(courts.column("wkb").to_numpy(zero_copy_only=False))
    attrs = courts.drop(["wkb"])
    cols = {name: attrs.column(name) for name in attrs.column_names}
    write_geoparquet(out_dir / "courts_us.parquet", cols, geoms)
    ik = courts.column("indoor_kind").to_numpy(zero_copy_only=False)
    indoor_idx = np.flatnonzero(np.array([v is not None for v in ik]))
    indoor = attrs.take(pa.array(indoor_idx, type=pa.int64()))
    write_geoparquet(out_dir / "indoor_us.parquet", {n: indoor.column(n) for n in indoor.column_names}, geoms[indoor_idx])
    log.info("courts: %d raw rows, %d elements, %d CONUS (%d outside CONUS dropped), %d label_ok, %d indoor-layer",
             n_raw, len(st), courts.num_rows, non_conus, int(label_ok.sum()), len(indoor_idx))

    state_files = {}
    for code in state_geojson:
        path = out_dir / f"courts_{code.upper()}.geojson"
        n = write_state_geojson(courts.drop(["wkb"]), geoms, code, path, region_label=code.lower())
        state_files[code.upper()] = {"path": path.name, "features": n}
        log.info("wrote %s (%d features)", path, n)

    # ---------------- context
    ctx = pq.read_table(sorted(part_dir.glob("context_*.parquet")))
    n_ctx_raw = ctx.num_rows
    cst = states.assign(ctx.column("center_lon").to_numpy(), ctx.column("center_lat").to_numpy())
    ctx = ctx.append_column("state", pa.array(cst.tolist(), type=pa.string()))
    ctx = ctx.filter(pa.array(cst != ""))
    cgeoms = shapely.from_wkb(ctx.column("wkb").to_numpy(zero_copy_only=False))
    cattrs = ctx.drop(["wkb"])
    write_geoparquet(out_dir / "context_us.parquet", {n: cattrs.column(n) for n in cattrs.column_names}, cgeoms)
    log.info("context: %d rows, %d CONUS", n_ctx_raw, ctx.num_rows)

    # ---------------- summary
    c_state = courts.column("state").to_pylist()
    by_state = _counts(c_state)
    tn_mask = np.array([s == "TN" for s in c_state])
    ctx_kind = ctx.column("kind").to_pylist()
    ctx_state = ctx.column("state").to_pylist()
    summary: dict[str, Any] = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source_pbf": pbf_name,
        "courts": {
            "total_conus": courts.num_rows,
            "outside_conus_dropped": non_conus,
            "by_geometry": _counts(gk.tolist()),
            "outdoor": int(outdoor.sum()),
            "court_like_outdoor_polygons": int((outdoor & clike & (gk == "polygon")).sum()),
            "label_ok": int(label_ok.sum()),
            "by_leisure": dict(list(_counts(courts.column("leisure").to_pylist()).items())[:12]),
            "top10_states": dict(list(by_state.items())[:10]),
            "by_state": by_state,
            "TN": {
                "total": int(tn_mask.sum()),
                "label_ok": int((label_ok & tn_mask).sum()),
                "by_geometry": _counts(gk[tn_mask].tolist()),
                "indoor_layer": int(sum(1 for i in indoor_idx if tn_mask[i])),
            },
        },
        "indoor_layer": {
            "total": len(indoor_idx),
            "by_kind": _counts(ik[indoor_idx].tolist()),
            "spec_kinds_only": int(sum(1 for v in ik[indoor_idx] if v != "indoor_pitch")),
        },
        "context": {
            "by_kind": _counts(ctx_kind),
            "TN_by_kind": _counts([k for k, s in zip(ctx_kind, ctx_state) if s == "TN"]),
        },
        "state_geojson": state_files,
        "seconds": round(time.monotonic() - t0, 1),
    }
    write_json(out_dir / "summary.json", summary)
    return summary


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pbf", type=Path, help="Geofabrik extract, e.g. data/osm_us/us-latest.osm.pbf")
    src.add_argument("--geojsonseq", type=Path, help="existing osmium export output (skips osmium)")
    ap.add_argument("--out-dir", type=Path, default=config.OSM_US_DIR)
    ap.add_argument("--states", type=Path, default=config.STATES_FILE, help="Census cartographic boundary zip")
    ap.add_argument("--state-geojson", nargs="*", default=["TN"], help="also write courts_<ST>.geojson for these states")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--force", action="store_true", help="re-run osmium even if outputs are newer than the input")
    ap.add_argument("--keep-parts", action="store_true")
    add_common_args(ap)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    t0 = time.monotonic()
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.pbf:
        if shutil.which("osmium") is None:
            raise SystemExit("osmium-tool not found (sudo apt-get install -y osmium-tool)")
        filtered = osmium_filter(args.pbf, out_dir / "filtered.osm.pbf", args.force)
        seq = osmium_export(filtered, out_dir / "filtered.geojsonseq", args.force)
        pbf_name = args.pbf.name
    else:
        seq = args.geojsonseq
        pbf_name = seq.name
    part_dir = out_dir / "parts"
    stats = parse_geojsonseq(seq, part_dir, args.workers)
    log.info("parsed: %s", stats)
    states = States.load(args.states)
    summary = assemble(part_dir, out_dir, states, args.state_geojson, pbf_name)
    summary["parse"] = stats
    summary["total_minutes"] = round((time.monotonic() - t0) / 60, 1)
    write_json(out_dir / "summary.json", summary)
    if not args.keep_parts:
        shutil.rmtree(part_dir, ignore_errors=True)
    c = summary["courts"]
    log.info("US (CONUS) basketball features: %d; TN: %d; indoor layer: %d; top states: %s",
             c["total_conus"], c["TN"]["total"], summary["indoor_layer"]["total"], c["top10_states"])


if __name__ == "__main__":
    main()
