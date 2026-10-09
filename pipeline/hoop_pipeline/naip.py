"""USDA NAIP imagery from Microsoft Planetary Computer: item lookup + windowed chip reads.

NAIP items are cloud-optimized GeoTIFF quarter-quads (~6 x 7 km, 4 bands R,G,B,NIR,
UTM / NAD83). We never download whole tiles: every read is an HTTP range request
for just the window we need, via rasterio + a SAS-signed URL from the
``planetary-computer`` package.

Examples
--------
    # list NAIP acquisitions over a point
    python -m hoop_pipeline.naip items --lat 35.0662 --lon -85.2537

    # one 320x320 chip (0.6 m/px -> 192 m square) centered on a point
    python -m hoop_pipeline.naip chip --lat 35.0662 --lon -85.2537 --out data/chips/test.png

    # chips for the first 3 OSM courts of a region (PNG + sidecar JSON each)
    python -m hoop_pipeline.naip chip --osm data/osm/chattanooga.geojson --limit 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import logging
import math
import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from shapely.geometry import Point, box, shape
from shapely.geometry.base import BaseGeometry

from . import config
from .common import M_PER_DEG_LAT, add_common_args, m_per_deg_lon, read_json, setup_logging, write_json

log = logging.getLogger("naip")

GDAL_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    GDAL_HTTP_MULTIPLEX="YES",
    GDAL_HTTP_VERSION="2",
    GDAL_HTTP_MAX_RETRY="4",
    GDAL_HTTP_RETRY_DELAY="2",
    VSI_CACHE="TRUE",
    VSI_CACHE_SIZE=str(64 * 1024 * 1024),
    GDAL_CACHEMAX=128 * 1024 * 1024,  # bytes; must be an int for rasterio
)

_stac_lock = threading.Lock()


# --------------------------------------------------------------------------- items
@dataclass
class NaipItem:
    id: str
    href: str            # unsigned COG URL; signed on demand
    datetime: str        # ISO 8601
    year: int
    state: str
    gsd: float
    crs: str             # e.g. "EPSG:26916"
    geometry: dict[str, Any]  # lon/lat footprint (GeoJSON)
    _shape: BaseGeometry | None = field(default=None, repr=False, compare=False)

    @property
    def footprint(self) -> BaseGeometry:
        if self._shape is None:
            self._shape = shape(self.geometry)
        return self._shape

    @property
    def date(self) -> str:
        return self.datetime[:10]

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_shape", None)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> NaipItem:
        return cls(**{k: v for k, v in d.items() if k != "_shape"})

    @classmethod
    def from_stac(cls, it: Any) -> NaipItem:
        p = it.properties
        crs = p.get("proj:code") or (f"EPSG:{p['proj:epsg']}" if p.get("proj:epsg") else "")
        return cls(
            id=it.id,
            href=it.assets["image"].href,
            datetime=(it.datetime or dt.datetime(int(p["naip:year"]), 1, 1)).isoformat(),
            year=int(p.get("naip:year") or it.datetime.year),
            state=str(p.get("naip:state", "")),
            gsd=float(p.get("gsd", 0.6)),
            crs=crs,
            geometry=it.geometry,
        )


@lru_cache(maxsize=1)
def _stac_client() -> pystac_client.Client:
    return pystac_client.Client.open(config.STAC_API_URL)


def search_items(
    bbox: tuple[float, float, float, float] | None = None,
    point: tuple[float, float] | None = None,
    min_year: int = config.NAIP_MIN_YEAR,
) -> list[NaipItem]:
    """STAC search. bbox is (south, west, north, east); point is (lat, lon)."""
    kwargs: dict[str, Any] = {"collections": [config.NAIP_COLLECTION], "datetime": f"{min_year}-01-01/.."}
    if point is not None:
        kwargs["intersects"] = {"type": "Point", "coordinates": [point[1], point[0]]}
    elif bbox is not None:
        s, w, n, e = bbox
        kwargs["bbox"] = [w, s, e, n]
    else:
        raise ValueError("need bbox or point")
    with _stac_lock:
        items = [NaipItem.from_stac(it) for it in _stac_client().search(**kwargs).items()]
    items.sort(key=lambda i: (i.datetime, i.id))
    return items


class NaipIndex:
    """All NAIP items over an area, with local 'best item for this point' lookup."""

    def __init__(self, items: list[NaipItem]) -> None:
        self.items = items

    @classmethod
    def for_bbox(
        cls,
        bbox: tuple[float, float, float, float],
        min_year: int = config.NAIP_MIN_YEAR,
        cache_dir: Path = config.NAIP_INDEX_DIR,
        refresh: bool = False,
    ) -> NaipIndex:
        key = "_".join(f"{v:.4f}" for v in bbox) + f"_y{min_year}"
        cache = cache_dir / f"naip_{hashlib.sha1(key.encode()).hexdigest()[:16]}.json"
        if cache.exists() and not refresh:
            items = [NaipItem.from_json(d) for d in read_json(cache)["items"]]
            log.debug("NAIP index cache hit %s (%d items)", cache.name, len(items))
            return cls(items)
        items = search_items(bbox=bbox, min_year=min_year)
        write_json(cache, {"bbox": list(bbox), "min_year": min_year, "items": [i.to_json() for i in items]}, indent=None)
        years: dict[int, int] = {}
        for it in items:
            years[it.year] = years.get(it.year, 0) + 1
        log.info("NAIP index for %s: %d items, by year %s", key, len(items), dict(sorted(years.items())))
        return cls(items)

    def candidates(self, lat: float, lon: float, half_size_m: float = 0.0, year: int | None = None) -> list[NaipItem]:
        """Items covering the point, best first: newest year, then fully containing the
        chip box, then the point farthest from the item edge."""
        pt = Point(lon, lat)
        cands = [it for it in self.items if (year is None or it.year == year) and it.footprint.contains(pt)]
        if not cands:
            return []
        chip_box = None
        if half_size_m > 0:
            dlat = half_size_m / M_PER_DEG_LAT
            dlon = half_size_m / m_per_deg_lon(lat)
            chip_box = box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)

        def score(it: NaipItem) -> tuple:
            full = chip_box is not None and it.footprint.contains(chip_box)
            return (it.year, full, it.footprint.exterior.distance(pt), it.datetime)

        return sorted(cands, key=score, reverse=True)

    def best_item(self, lat: float, lon: float, half_size_m: float = 0.0, year: int | None = None) -> NaipItem | None:
        c = self.candidates(lat, lon, half_size_m, year)
        return c[0] if c else None


# --------------------------------------------------------------------------- chips
@dataclass
class Chip:
    image: np.ndarray        # (H, W, 3) uint8 RGB, or (H, W, 4) RGBN when NIR requested
    crs: str
    transform: Affine        # chip pixel (col, row) -> CRS (x, y)
    item: NaipItem
    gsd: float
    center_lat: float
    center_lon: float
    valid_fraction: float
    extra_items: list[str] = field(default_factory=list)  # items used to fill edge gaps

    @property
    def rgb(self) -> np.ndarray:
        return self.image[..., :3]

    @property
    def size(self) -> tuple[int, int]:
        return self.image.shape[1], self.image.shape[0]

    def pixel_to_crs(self, col: float, row: float) -> tuple[float, float]:
        return self.transform * (col, row)

    def crs_to_pixel(self, x: float, y: float) -> tuple[float, float]:
        return ~self.transform * (x, y)

    def pixel_to_lonlat(self, col: float, row: float) -> tuple[float, float]:
        x, y = self.pixel_to_crs(col, row)
        return to_lonlat(self.crs).transform(x, y)

    def metadata(self) -> dict[str, Any]:
        w, h = self.size
        x0, y0 = self.pixel_to_crs(0, 0)
        x1, y1 = self.pixel_to_crs(w, h)
        lon0, lat0 = to_lonlat(self.crs).transform(min(x0, x1), min(y0, y1))
        lon1, lat1 = to_lonlat(self.crs).transform(max(x0, x1), max(y0, y1))
        return {
            "source": "USDA NAIP via Microsoft Planetary Computer (public domain)",
            "collection": config.NAIP_COLLECTION,
            "item_id": self.item.id,
            "extra_item_ids": self.extra_items,
            "datetime": self.item.datetime,
            "date": self.item.date,
            "year": self.item.year,
            "state": self.item.state,
            "native_gsd_m": self.item.gsd,
            "gsd_m": self.gsd,
            "width": w,
            "height": h,
            "bands": ["red", "green", "blue", "nir"][: self.image.shape[2]],
            "crs": self.crs,
            "transform": list(self.transform)[:6],
            "bounds_crs": [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)],
            "bounds_lonlat": [lon0, lat0, lon1, lat1],
            "center": {"lat": self.center_lat, "lon": self.center_lon},
            "valid_fraction": round(self.valid_fraction, 4),
            "href": self.item.href,
        }


@lru_cache(maxsize=32)
def from_lonlat(crs: str) -> Transformer:
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True)


@lru_cache(maxsize=32)
def to_lonlat(crs: str) -> Transformer:
    return Transformer.from_crs(crs, "EPSG:4326", always_xy=True)


def _signed(href: str) -> str:
    return planetary_computer.sign(href)


def read_chip(
    lat: float,
    lon: float,
    size_px: int = config.CHIP_SIZE_PX,
    item: NaipItem | None = None,
    index: NaipIndex | None = None,
    target_gsd: float | None = config.TARGET_GSD_M,
    with_nir: bool = False,
    year: int | None = None,
    fill_gaps: bool = True,
) -> Chip:
    """Read a size_px x size_px chip centered on (lat, lon).

    The chip is resampled to ``target_gsd`` metres/pixel (None = native resolution)
    so models see the same scale whether a state was flown at 0.6 m or 0.3 m.
    Pixels outside the chosen item are filled from other covering items when
    ``fill_gaps`` is set (needed only near quarter-quad edges).
    """
    gsd_hint = target_gsd or 0.6
    half_m = size_px * gsd_hint / 2
    if item is not None:
        candidates = [item]
        if fill_gaps and index is not None:
            candidates += [c for c in index.candidates(lat, lon, half_m, year or item.year) if c.id != item.id]
    else:
        idx = index or NaipIndex(search_items(point=(lat, lon)))
        candidates = idx.candidates(lat, lon, half_m, year)
        if not candidates:
            raise LookupError(f"No NAIP item covers {lat:.6f},{lon:.6f}")
        first_year = candidates[0].year
        candidates = [c for c in candidates if c.year == first_year] + [c for c in candidates if c.year != first_year]
    primary = candidates[0]

    bands = [1, 2, 3, 4] if with_nir else [1, 2, 3]
    with rasterio.Env(**GDAL_ENV):
        with rasterio.open(_signed(primary.href)) as src:
            crs = src.crs.to_string()
            native = float(src.res[0])
            gsd = float(target_gsd) if target_gsd else native
            win_px = size_px * gsd / native
            x, y = from_lonlat(crs).transform(lon, lat)
            col, row = ~src.transform * (x, y)
            col_off = int(round(col - win_px / 2))
            row_off = int(round(row - win_px / 2))
            win_size = int(round(win_px))
            window = Window(col_off, row_off, win_size, win_size)
            inside = col_off >= 0 and row_off >= 0 and col_off + win_size <= src.width and row_off + win_size <= src.height
            if win_size == size_px:
                resampling = Resampling.nearest
            else:  # area-average when shrinking (0.3 m -> 0.6 m), bilinear when enlarging
                resampling = Resampling.average if win_size > size_px else Resampling.bilinear
            data = src.read(
                bands,
                window=window,
                out_shape=(len(bands), size_px, size_px),
                resampling=resampling,
                boundless=not inside,
                fill_value=0,
            )
            transform = src.window_transform(window) * Affine.scale(win_size / size_px)

        valid = data[:3].max(axis=0) > 0
        extra: list[str] = []
        if fill_gaps and not valid.all():
            for other in candidates[1:]:
                if valid.all():
                    break
                with rasterio.open(_signed(other.href)) as osrc, WarpedVRT(
                    osrc, crs=crs, transform=transform, width=size_px, height=size_px,
                    resampling=Resampling.bilinear, nodata=0,
                ) as vrt:
                    patch = vrt.read(bands)
                pvalid = patch[:3].max(axis=0) > 0
                fill = ~valid & pvalid
                if fill.any():
                    data[:, fill] = patch[:, fill]
                    valid |= fill
                    extra.append(other.id)

    img = np.ascontiguousarray(np.transpose(data, (1, 2, 0))).astype(np.uint8, copy=False)
    return Chip(
        image=img, crs=crs, transform=transform, item=primary, gsd=gsd,
        center_lat=lat, center_lon=lon, valid_fraction=float(valid.mean()), extra_items=extra,
    )


def save_chip(chip: Chip, png_path: Path, extra_meta: dict[str, Any] | None = None) -> Path:
    """Write <name>.png (RGB), optional <name>_nir.png, and <name>.json sidecar."""
    from PIL import Image

    png_path = Path(png_path)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(chip.rgb).save(png_path)
    meta = chip.metadata()
    if chip.image.shape[2] == 4:
        nir_path = png_path.with_name(png_path.stem + "_nir.png")
        Image.fromarray(chip.image[..., 3]).save(nir_path)
        meta["nir_png"] = nir_path.name
    if extra_meta:
        meta.update(extra_meta)
    write_json(png_path.with_suffix(".json"), meta)
    return png_path


# --------------------------------------------------------------------------- CLI
def _iter_osm_points(path: Path, limit: int | None, ids: list[str] | None) -> Iterator[tuple[str, float, float]]:
    feats = read_json(path)["features"]
    n = 0
    for f in feats:
        p = f["properties"]
        ref = p["osm_ref"]
        if ids and ref not in ids and str(p["osm_id"]) not in ids:
            continue
        yield ref.replace("/", "-"), p["center_lat"], p["center_lon"]
        n += 1
        if limit and n >= limit:
            return


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ai = sub.add_parser("items", help="list NAIP items covering a point or bbox")
    ai.add_argument("--lat", type=float)
    ai.add_argument("--lon", type=float)
    ai.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    ai.add_argument("--min-year", type=int, default=2010)
    add_common_args(ai)

    ac = sub.add_parser("chip", help="read chip(s) to PNG + sidecar JSON")
    ac.add_argument("--lat", type=float)
    ac.add_argument("--lon", type=float)
    ac.add_argument("--osm", type=Path, help="fetch_osm GeoJSON; chips are centered on its courts")
    ac.add_argument("--ids", nargs="*", help="with --osm: only these OSM refs/ids (e.g. way/120307977)")
    ac.add_argument("--limit", type=int, default=3, help="with --osm: max chips")
    ac.add_argument("--size", type=int, default=config.CHIP_SIZE_PX, help="chip size in pixels")
    ac.add_argument("--gsd", type=float, default=config.TARGET_GSD_M, help="output m/px (0 = native)")
    ac.add_argument("--year", type=int, help="force a NAIP year instead of the most recent")
    ac.add_argument("--nir", action="store_true", help="also write the NIR band as <name>_nir.png")
    ac.add_argument("--out", type=Path, help="PNG path (single chip) or directory (with --osm)")
    add_common_args(ac)

    args = ap.parse_args(argv)
    setup_logging(args.verbose)

    if args.cmd == "items":
        if args.lat is not None and args.lon is not None:
            items = search_items(point=(args.lat, args.lon), min_year=args.min_year)
        elif args.bbox:
            items = search_items(bbox=tuple(args.bbox), min_year=args.min_year)
        else:
            ap.error("items needs --lat/--lon or --bbox")
        for it in items:
            print(f"{it.date}  year={it.year}  state={it.state}  gsd={it.gsd}  crs={it.crs}  {it.id}")
        print(f"{len(items)} items")
        return

    target_gsd = args.gsd or None
    if args.osm:
        out_dir = args.out or config.CHIPS_DIR / args.osm.stem
        pts = list(_iter_osm_points(args.osm, args.limit, args.ids))
        bbox = (
            min(p[1] for p in pts) - 0.01, min(p[2] for p in pts) - 0.01,
            max(p[1] for p in pts) + 0.01, max(p[2] for p in pts) + 0.01,
        )
        index = NaipIndex.for_bbox(bbox)
        for name, lat, lon in pts:
            chip = read_chip(lat, lon, args.size, index=index, target_gsd=target_gsd, with_nir=args.nir, year=args.year)
            path = save_chip(chip, out_dir / f"{name}.png", {"osm_ref": name.replace("-", "/", 1)})
            log.info("%s -> %s  (NAIP %s, %s, %.2f m/px native %.2f, valid %.0f%%)",
                     name, path, chip.item.date, chip.item.id, chip.gsd, chip.item.gsd, 100 * chip.valid_fraction)
        return

    if args.lat is None or args.lon is None:
        ap.error("chip needs --lat/--lon or --osm")
    chip = read_chip(args.lat, args.lon, args.size, target_gsd=target_gsd, with_nir=args.nir, year=args.year)
    out = args.out or config.CHIPS_DIR / f"chip_{args.lat:.5f}_{args.lon:.5f}.png"
    save_chip(chip, out)
    w, h = chip.size
    log.info("wrote %s (%dx%d px, %.2f m/px = %.0f m across; NAIP %s %s)",
             out, w, h, chip.gsd, w * chip.gsd, chip.item.date, chip.item.id)
    if not math.isclose(chip.valid_fraction, 1.0):
        log.warning("only %.1f%% of the chip has imagery", 100 * chip.valid_fraction)


if __name__ == "__main__":
    main()
