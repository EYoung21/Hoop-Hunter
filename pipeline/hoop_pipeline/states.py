"""US state boundaries (Census cartographic boundary file, public domain) and point-in-state lookup.

The 1:500k Census file (``cb_2023_us_state_500k.zip``) is downloaded once to
``data/census/``. It is read with pyshp, so no GDAL vector driver is needed.
"Contiguous US" (CONUS) is the 48 states plus DC; NAIP only covers those
(plus a little of HI/PR, which we ignore).
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import requests
import shapely
from shapely import STRtree
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from . import config

log = logging.getLogger("states")

# AK, HI, American Samoa, Guam, N. Mariana Is., Puerto Rico, US Virgin Is.
NON_CONUS_FIPS = {"02", "15", "60", "66", "69", "72", "78"}


def ensure_states_file(path: Path = config.STATES_FILE, url: str = config.CENSUS_STATES_URL) -> Path:
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s", url)
    resp = requests.get(url, timeout=120, headers={"User-Agent": config.USER_AGENT})
    resp.raise_for_status()
    tmp = path.with_suffix(".part")
    tmp.write_bytes(resp.content)
    tmp.replace(path)
    return path


@dataclass
class States:
    codes: np.ndarray            # USPS, e.g. "TN"
    fips: np.ndarray             # "47"
    names: np.ndarray
    geoms: np.ndarray            # shapely (Multi)Polygons, lon/lat
    tree: STRtree = field(repr=False)

    @classmethod
    def load(cls, path: Path | None = None, conus_only: bool = True) -> States:
        import shapefile  # pyshp

        path = ensure_states_file(path or config.STATES_FILE)
        with zipfile.ZipFile(path) as zf:
            stem = next(n[:-4] for n in zf.namelist() if n.endswith(".shp"))
            with zf.open(stem + ".shp") as shp, zf.open(stem + ".dbf") as dbf, zf.open(stem + ".shx") as shx:
                reader = shapefile.Reader(shp=shp, dbf=dbf, shx=shx)
                fields = [f[0] for f in reader.fields[1:]]
                codes, fips, names, geoms = [], [], [], []
                for sr in reader.iterShapeRecords():
                    rec = dict(zip(fields, sr.record))
                    fp = str(rec["STATEFP"]).zfill(2)
                    if conus_only and fp in NON_CONUS_FIPS:
                        continue
                    geom = shape(sr.shape.__geo_interface__)
                    if not geom.is_valid:
                        geom = shapely.make_valid(geom)
                    codes.append(str(rec["STUSPS"]))
                    fips.append(fp)
                    names.append(str(rec["NAME"]))
                    geoms.append(geom)
        g = np.asarray(geoms, dtype=object)
        log.info("loaded %d state boundaries from %s", len(codes), path.name)
        return cls(np.asarray(codes), np.asarray(fips), np.asarray(names), g, STRtree(g))

    def geometry(self, code: str) -> BaseGeometry:
        idx = np.flatnonzero(self.codes == code.upper())
        if not len(idx):
            raise KeyError(f"unknown or non-CONUS state {code!r}")
        return self.geoms[idx[0]]

    def assign(self, lons: np.ndarray, lats: np.ndarray, snap_deg: float = 0.02) -> np.ndarray:
        """USPS code of the state containing each point ('' if none).

        Points just outside every polygon (piers, coastline generalisation of the
        1:500k file) are snapped to the nearest state within ``snap_deg``.
        """
        lons = np.asarray(lons, dtype=float)
        lats = np.asarray(lats, dtype=float)
        out = np.full(len(lons), "", dtype=object)
        if not len(lons):
            return out
        # Prepared polygon + vectorised point test per state, prefiltered by the state's bbox.
        # (An STRtree point query would test points against unprepared 10k-vertex polygons.)
        for code, geom in zip(self.codes, self.geoms):
            shapely.prepare(geom)
            minx, miny, maxx, maxy = geom.bounds
            sel = np.flatnonzero((out == "") & (lons >= minx) & (lons <= maxx) & (lats >= miny) & (lats <= maxy))
            if len(sel):
                hit = shapely.intersects_xy(geom, lons[sel], lats[sel])
                out[sel[hit]] = code
        missing = np.flatnonzero(out == "")
        if len(missing) and snap_deg > 0:
            simple = shapely.simplify(self.geoms, 0.002)
            pts = shapely.points(lons[missing], lats[missing])
            mp, ms = STRtree(simple).query_nearest(pts, max_distance=snap_deg, all_matches=False)
            out[missing[mp]] = self.codes[ms]
        return out


@lru_cache(maxsize=1)
def load_states() -> States:
    return States.load()
