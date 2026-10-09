"""Minimal GeoParquet (WKB, lon/lat) read/write with pyarrow + shapely.

The nationwide layers (courts, indoor courts, hard negatives / scan-mask
features) are stored as GeoParquet 1.0 files: one WKB ``geometry`` column in
OGC:CRS84 (lon/lat) plus plain attribute columns, zstd-compressed. geopandas
can read them directly (``geopandas.read_parquet``), but nothing here needs it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from shapely.geometry.base import BaseGeometry


def _as_array(values: Any) -> pa.Array | pa.ChunkedArray:
    if isinstance(values, (pa.Array, pa.ChunkedArray)):
        return values
    if isinstance(values, np.ndarray) and values.dtype.kind in "biuf":
        return pa.array(values)
    return pa.array(list(values))


def write_geoparquet(
    path: Path | str,
    columns: Mapping[str, Any],
    geometry: Sequence[BaseGeometry | None] | np.ndarray,
    geometry_column: str = "geometry",
    compression: str = "zstd",
    row_group_size: int = 200_000,
) -> int:
    """Write attribute ``columns`` plus a WKB geometry column. Returns the row count."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    geoms = np.asarray(geometry, dtype=object)
    wkb = shapely.to_wkb(geoms) if len(geoms) else np.array([], dtype=object)
    arrays = {k: _as_array(v) for k, v in columns.items()}
    arrays[geometry_column] = pa.array(list(wkb), type=pa.binary())
    table = pa.table(arrays)
    present = geoms[shapely.is_geometry(geoms)] if len(geoms) else geoms
    types = sorted({g.geom_type for g in present}) if len(present) else []
    bbox = [float(v) for v in shapely.total_bounds(present)] if len(present) else []
    geo = {
        "version": "1.0.0",
        "primary_column": geometry_column,
        "columns": {geometry_column: {"encoding": "WKB", "geometry_types": types, **({"bbox": bbox} if bbox else {})}},
    }
    meta = dict(table.schema.metadata or {})
    meta[b"geo"] = json.dumps(geo).encode()
    table = table.replace_schema_metadata(meta)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression=compression, row_group_size=row_group_size)
    tmp.replace(path)
    return table.num_rows


def read_geoparquet(
    path: Path | str,
    columns: list[str] | None = None,
    filters: Any = None,
    geometry_column: str = "geometry",
    with_geometry: bool = True,
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Read a GeoParquet file into ({column: numpy array}, geometry object array; empty if none)."""
    cols = None
    if columns is not None:
        cols = list(columns) + ([geometry_column] if with_geometry and geometry_column not in columns else [])
    elif not with_geometry:
        schema = pq.read_schema(path)
        cols = [n for n in schema.names if n != geometry_column]
    table = pq.read_table(path, columns=cols, filters=filters)
    out: dict[str, np.ndarray] = {}
    for name in table.column_names:
        if name == geometry_column:
            continue
        out[name] = table.column(name).to_numpy(zero_copy_only=False)
    geoms = np.array([], dtype=object)
    if with_geometry and geometry_column in table.column_names:
        raw = table.column(geometry_column).to_numpy(zero_copy_only=False)
        geoms = shapely.from_wkb(raw) if len(raw) else np.array([], dtype=object)
    return out, geoms
