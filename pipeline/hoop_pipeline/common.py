"""Small helpers shared by every step: logging, geodesy, JSON IO, stable ids."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

EARTH_RADIUS_M = 6_371_008.8
M_PER_DEG_LAT = 111_320.0


# --------------------------------------------------------------------------- logging
def setup_logging(verbose: int = 0) -> None:
    level = logging.DEBUG if verbose > 0 else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Third-party libraries are chatty at DEBUG.
    for noisy in ("urllib3", "rasterio", "fiona", "PIL", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-v", "--verbose", action="count", default=0, help="debug logging")


# --------------------------------------------------------------------------- geodesy
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def m_per_deg_lon(lat: float) -> float:
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


def offset_latlon(lat: float, lon: float, dx_m: float, dy_m: float) -> tuple[float, float]:
    """Shift a point by dx (east) / dy (north) metres. Fine for offsets < a few km."""
    return lat + dy_m / M_PER_DEG_LAT, lon + dx_m / m_per_deg_lon(lat)


class LocalMeters:
    """Equirectangular projection around a reference point (metres, x east / y north).

    Accurate to well under 1 m over the few-km extents we compare distances in.
    """

    def __init__(self, lat0: float, lon0: float) -> None:
        self.lat0, self.lon0 = lat0, lon0
        self.kx = m_per_deg_lon(lat0)
        self.ky = M_PER_DEG_LAT

    def fwd(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky

    def inv(self, x: float, y: float) -> tuple[float, float]:
        return self.lat0 + y / self.ky, self.lon0 + x / self.kx


def split_bbox(
    bbox: tuple[float, float, float, float], max_deg: float
) -> list[tuple[float, float, float, float]]:
    """Split (south, west, north, east) into a grid of tiles no larger than max_deg."""
    s, w, n, e = bbox
    ny = max(1, math.ceil((n - s) / max_deg - 1e-9))
    nx = max(1, math.ceil((e - w) / max_deg - 1e-9))
    dy, dx = (n - s) / ny, (e - w) / nx
    tiles = []
    for iy in range(ny):
        for ix in range(nx):
            tiles.append((s + iy * dy, w + ix * dx, s + (iy + 1) * dy, w + (ix + 1) * dx))
    return tiles


def bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    s, w, n, e = bbox
    lat = (s + n) / 2
    return ((n - s) * M_PER_DEG_LAT / 1000) * ((e - w) * m_per_deg_lon(lat) / 1000)


# --------------------------------------------------------------------------- ids
def stable_detection_id(lat: float, lon: float, decimals: int = 4) -> str:
    """'det-<hash>' from coordinates rounded to ~11 m, stable across re-runs."""
    key = f"{round(lat, decimals):.{decimals}f},{round(lon, decimals):.{decimals}f}"
    return "det-" + hashlib.sha1(key.encode("ascii")).hexdigest()[:12]


# --------------------------------------------------------------------------- json io
def read_json(path: Path | str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path | str, data: Any, indent: int | None = 1) -> None:
    """Atomic JSON write (temp file + rename) with LF newlines and a trailing newline."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=indent)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def feature_collection(features: Iterable[dict], **extra: Any) -> dict:
    fc: dict[str, Any] = {"type": "FeatureCollection"}
    fc.update(extra)
    fc["features"] = list(features)
    return fc
