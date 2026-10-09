"""Nationwide NAIP item index (Planetary Computer GeoParquet) and grouped COG window reads.

Per-chip STAC searches don't scale to tens of thousands of chips, so this
module loads every NAIP item footprint at once from the ``naip`` collection's
``geoparquet-items`` asset (stac-geoparquet, partitioned by year; ~25 MB per
year). The files are fetched once with a Planetary Computer SAS token into
``data/naip_index/geoparquet/`` and condensed into one small catalog parquet.

Item choice for a location (``NaipCatalog.select``):

1. items whose footprint contains the point (``naip:year`` >= ``min_year``);
2. prefer flights of the location's own state (``naip:state``) unless that
   state's newest covering item is more than ``slack_years`` older than the
   newest overall. This keeps e.g. Georgia's October leaf-on flight off
   Tennessee locations that a Tennessee flight also covers;
3. then the most recent ``naip:year``;
4. then an item that contains the whole chip, then the latest acquisition.

``GroupedReader`` reads many windows per item: each COG is opened once per
task and serves many windows (sorted for block-cache locality); tasks run in a
thread pool, with retries and exponential backoff around opens and reads.
Window georeferencing comes from the catalog's ``proj:transform``, so chip
geometry (and labels) can be computed before any pixels are fetched.
"""

from __future__ import annotations

import calendar
import logging
import os
import random
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import requests
import shapely
from shapely import STRtree

from . import config
from .common import M_PER_DEG_LAT

log = logging.getLogger("naip_index")

# GDAL settings for many concurrent ranged reads of NAIP COGs (512 px DEFLATE tiles).
GDAL_ENV_BULK: dict[str, Any] = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    GDAL_HTTP_MULTIPLEX="YES",
    GDAL_HTTP_MAX_RETRY="5",
    GDAL_HTTP_RETRY_DELAY="1",
    GDAL_HTTP_TIMEOUT="60",
    GDAL_INGESTED_BYTES_AT_OPEN="65536",
    VSI_CACHE="TRUE",
    VSI_CACHE_SIZE=str(64 * 1024 * 1024),
    CPL_VSIL_CURL_CACHE_SIZE=str(512 * 1024 * 1024),
    GDAL_CACHEMAX=2 * 1024 * 1024 * 1024,
)

_PART_RE = re.compile(r"part-\d+_(\d{4})-")


# --------------------------------------------------------------------------- catalog download
def _sas_token(account: str, container: str) -> str:
    url = config.PC_SAS_TOKEN_URL.format(account=account, container=container)
    resp = requests.get(url, timeout=60, headers={"User-Agent": config.USER_AGENT})
    resp.raise_for_status()
    return resp.json()["token"]


def download_partitions(cache_dir: Path, min_year: int, refresh: bool = False) -> list[Path]:
    """Download the yearly GeoParquet partitions that can hold items with naip:year >= min_year."""
    coll = requests.get(f"{config.STAC_API_URL}/collections/{config.NAIP_COLLECTION}", timeout=60,
                        headers={"User-Agent": config.USER_AGENT}).json()
    asset = coll["assets"]["geoparquet-items"]
    account = asset["table:storage_options"]["account_name"]
    container, prefix = asset["href"].removeprefix("abfs://").split("/", 1)
    token = _sas_token(account, container)
    base = f"https://{account}.blob.core.windows.net/{container}"
    listing = requests.get(f"{base}?restype=container&comp=list&prefix={urllib.parse.quote(prefix)}/&{token}", timeout=60)
    listing.raise_for_status()
    names = re.findall(r"<Name>(.*?)</Name>", listing.text)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for name in names:
        m = _PART_RE.search(name)
        if not m or int(m.group(1)) < min_year:
            continue
        dest = cache_dir / Path(name).name.replace(":", "_")
        if not dest.exists() or refresh:
            t0 = time.monotonic()
            resp = requests.get(f"{base}/{urllib.parse.quote(name)}?{token}", timeout=600)
            resp.raise_for_status()
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(resp.content)
            tmp.replace(dest)
            log.info("downloaded %s (%.1f MB, %.1fs)", dest.name, len(resp.content) / 1e6, time.monotonic() - t0)
        out.append(dest)
    if not out:
        raise RuntimeError(f"no NAIP GeoParquet partitions found under {asset['href']}")
    return out


_CATALOG_COLS = ["id", "geometry", "naip:state", "naip:year", "datetime", "gsd", "proj:epsg", "proj:shape",
                 "proj:transform", "assets"]


def _condense(path: Path, min_year: int) -> pa.Table:
    t = pq.read_table(path, columns=_CATALOG_COLS)
    t = t.filter(pc.greater_equal(t.column("naip:year"), min_year))
    href = pc.struct_field(pc.struct_field(t.column("assets"), "image"), "href")
    tr = t.column("proj:transform").to_pylist()
    sh = t.column("proj:shape").to_pylist()
    return pa.table({
        "id": t.column("id"),
        "state": t.column("naip:state"),
        "year": t.column("naip:year").cast(pa.int32()),
        "datetime": t.column("datetime").cast(pa.timestamp("us", tz="UTC")),
        "gsd": t.column("gsd").cast(pa.float64()),
        "epsg": t.column("proj:epsg").cast(pa.int32()),
        "height": pa.array([int(s[0]) for s in sh], type=pa.int32()),
        "width": pa.array([int(s[1]) for s in sh], type=pa.int32()),
        "t_a": pa.array([float(x[0]) for x in tr]), "t_b": pa.array([float(x[1]) for x in tr]),
        "t_c": pa.array([float(x[2]) for x in tr]), "t_d": pa.array([float(x[3]) for x in tr]),
        "t_e": pa.array([float(x[4]) for x in tr]), "t_f": pa.array([float(x[5]) for x in tr]),
        "href": href,
        "geometry": t.column("geometry"),
    })


# --------------------------------------------------------------------------- catalog
@dataclass
class NaipCatalog:
    ids: np.ndarray
    states: np.ndarray        # upper-case USPS of the flight ("TN")
    years: np.ndarray
    dates: np.ndarray         # datetime64[us]
    gsd: np.ndarray
    epsg: np.ndarray
    width: np.ndarray
    height: np.ndarray
    transform: np.ndarray     # (n, 6) affine a, b, c, d, e, f (proj:transform)
    hrefs: np.ndarray
    geoms: np.ndarray         # lon/lat footprints
    tree: STRtree = field(repr=False)

    def __len__(self) -> int:
        return len(self.ids)

    @classmethod
    def load(cls, cache_dir: Path = config.NAIP_GEOPARQUET_DIR, min_year: int = config.NAIP_MIN_YEAR,
             refresh: bool = False) -> NaipCatalog:
        condensed = cache_dir / f"naip_catalog_y{min_year}.parquet"
        if not condensed.exists() or refresh:
            parts = download_partitions(cache_dir, min_year, refresh)
            tables = [_condense(p, min_year) for p in parts]
            table = pa.concat_tables(tables)
            pq.write_table(table, condensed, compression="zstd")
            log.info("condensed %d NAIP items from %d partitions -> %s", table.num_rows, len(parts), condensed)
        t = pq.read_table(condensed)
        geoms = shapely.from_wkb(t.column("geometry").to_numpy(zero_copy_only=False))
        tf = np.column_stack([t.column(f"t_{k}").to_numpy() for k in "abcdef"])
        cat = cls(
            ids=t.column("id").to_numpy(zero_copy_only=False),
            states=np.char.upper(t.column("state").to_numpy(zero_copy_only=False).astype(str)),
            years=t.column("year").to_numpy(),
            dates=t.column("datetime").cast(pa.timestamp("us")).to_numpy(),
            gsd=t.column("gsd").to_numpy(),
            epsg=t.column("epsg").to_numpy(),
            width=t.column("width").to_numpy(),
            height=t.column("height").to_numpy(),
            transform=tf,
            hrefs=t.column("href").to_numpy(zero_copy_only=False),
            geoms=geoms,
            tree=STRtree(geoms),
        )
        log.info("NAIP catalog: %d items (year >= %d), years %s", len(cat), min_year,
                 dict(zip(*np.unique(cat.years, return_counts=True))))
        return cat

    def date_str(self, i: int) -> str:
        return str(self.dates[i])[:10]

    def select(
        self, lons: np.ndarray, lats: np.ndarray, states: np.ndarray | None = None,
        half_m: float = 96.0, slack_years: int = 2, year: int | None = None,
    ) -> np.ndarray:
        """Best item index for each point (-1 if none). See module docstring for the rule."""
        lons = np.asarray(lons, dtype=float)
        lats = np.asarray(lats, dtype=float)
        n = len(lons)
        best = np.full(n, -1, dtype=np.int64)
        if n == 0:
            return best
        pts = shapely.points(lons, lats)
        p, it = self.tree.query(pts, predicate="intersects")
        if year is not None:
            keep = self.years[it] == year
            p, it = p[keep], it[keep]
        if not len(p):
            return best
        dlat = half_m / M_PER_DEG_LAT
        dlon = half_m / (M_PER_DEG_LAT * np.cos(np.radians(lats)))
        boxes = shapely.box(lons - dlon, lats - dlat, lons + dlon, lats + dlat)
        fp, fit = self.tree.query(boxes, predicate="within")
        m = np.int64(len(self.ids))
        full = np.isin(p.astype(np.int64) * m + it, fp.astype(np.int64) * m + fit)
        yr = self.years[it].astype(np.int64)
        if states is not None:
            st = np.asarray(states).astype(str)
            same = self.states[it] == np.char.upper(st[p])
            maxy_all = np.full(n, -1, dtype=np.int64)
            np.maximum.at(maxy_all, p, yr)
            maxy_same = np.full(n, -1, dtype=np.int64)
            np.maximum.at(maxy_same, p[same], yr[same])
            eff_same = same & (maxy_same[p] >= maxy_all[p] - slack_years)
        else:
            eff_same = np.zeros(len(p), dtype=bool)
        date = self.dates[it].astype("datetime64[s]").astype(np.int64)
        order = np.lexsort((-date, -full.astype(np.int64), -yr, -eff_same.astype(np.int64), p))
        ps = p[order]
        first = np.r_[True, ps[1:] != ps[:-1]]
        best[ps[first]] = it[order][first]
        return best

    def items_for_state(self, state: str, min_year: int | None = None) -> np.ndarray:
        sel = self.states == state.upper()
        if min_year is not None:
            sel &= self.years >= min_year
        return np.flatnonzero(sel)


# --------------------------------------------------------------------------- windows
@dataclass
class WindowPlan:
    """Square chip windows: native-pixel window + the output chip's affine."""

    item: np.ndarray          # catalog index
    col_off: np.ndarray       # native px
    row_off: np.ndarray
    win: np.ndarray           # native px per side
    out_size: int
    chip_transform: np.ndarray  # (n, 6): a, b, c, d, e, f of the output chip
    epsg: np.ndarray


def plan_windows(cat: NaipCatalog, item: np.ndarray, lons: np.ndarray, lats: np.ndarray,
                 size_px: int = config.CHIP_SIZE_PX, gsd: float = config.TARGET_GSD_M) -> WindowPlan:
    """Windows of size_px x size_px output pixels at ``gsd`` centred on each point."""
    from pyproj import Transformer

    n = len(item)
    x = np.full(n, np.nan)
    y = np.full(n, np.nan)
    epsg = np.where(item >= 0, cat.epsg[np.clip(item, 0, None)], 0)
    for code in np.unique(epsg[item >= 0]):
        sel = (epsg == code) & (item >= 0)
        tr = Transformer.from_crs("EPSG:4326", f"EPSG:{int(code)}", always_xy=True)
        x[sel], y[sel] = tr.transform(np.asarray(lons)[sel], np.asarray(lats)[sel])
    it = np.clip(item, 0, None)
    a, b, c, d, e, f = (cat.transform[it, k] for k in range(6))
    native = np.abs(a)
    win = np.rint(size_px * gsd / native).astype(np.int64)
    col = np.nan_to_num((x - c) / a)
    row = np.nan_to_num((y - f) / e)
    col_off = np.rint(col - win / 2).astype(np.int64)
    row_off = np.rint(row - win / 2).astype(np.int64)
    scale = win / size_px
    chip_tf = np.column_stack([a * scale, b, c + col_off * a, d, e * scale, f + row_off * e])
    return WindowPlan(item=np.asarray(item), col_off=col_off, row_off=row_off, win=win, out_size=size_px,
                      chip_transform=chip_tf, epsg=epsg)


# --------------------------------------------------------------------------- reads
_token_lock = threading.Lock()
_tokens: dict[str, tuple[str, float]] = {}  # "account/container" -> (SAS token, expiry epoch seconds)
# Containers that answered anonymous range reads on 2026-10-08. Opt in with HH_ANONYMOUS_READS=1;
# Planetary Computer documents SAS tokens as the access path, so keep tokens as the default.
ANONYMOUS_CONTAINERS = {"naipeuwest/naip"}


def _signed(href: str, refresh_margin_s: float = 300.0, retries: int = 10) -> str:
    """SAS-sign a blob URL. One token per container, fetched by a single thread under a
    lock and refreshed ``refresh_margin_s`` before it expires: the anonymous token endpoint
    throttles bursts, and dozens of threads re-fetching at once stalls every read."""
    p = urllib.parse.urlparse(href)
    account, container = p.netloc.split(".")[0], p.path.lstrip("/").split("/")[0]
    if os.environ.get("HH_ANONYMOUS_READS") and f"{account}/{container}" in ANONYMOUS_CONTAINERS:
        return href  # public container: skip the (throttled) token endpoint entirely
    key = f"{account}/{container}"
    with _token_lock:
        tok = _tokens.get(key)
        if tok is None or tok[1] - time.time() < refresh_margin_s:
            url = config.PC_SAS_TOKEN_URL.format(account=account, container=container)
            last: Exception | None = None
            for attempt in range(retries):
                try:
                    resp = requests.get(url, timeout=60, headers={"User-Agent": config.USER_AGENT})
                    if resp.status_code == 429:
                        raise RuntimeError(f"429 from token endpoint (Retry-After {resp.headers.get('Retry-After')})")
                    resp.raise_for_status()
                    data = resp.json()
                    expiry = data.get("msft:expiry", "")
                    try:
                        exp: float = float(calendar.timegm(time.strptime(expiry[:19], "%Y-%m-%dT%H:%M:%S")))
                    except ValueError:
                        exp = time.time() + 1800
                    _tokens[key] = (data["token"], exp)
                    log.info("SAS token for %s valid until %s", key, expiry)
                    break
                except Exception as exc:
                    last = exc
                    time.sleep(min(60.0, 2.0 * 2 ** attempt) * (0.5 + random.random()))
            else:
                raise RuntimeError(f"could not get a SAS token for {key}: {last}")
        token = _tokens[key][0]
    return f"{href}?{token}"


def read_square(src: Any, col_off: int, row_off: int, win: int, out_size: int,
                bands: tuple[int, ...] = (1, 2, 3)) -> np.ndarray:
    """(bands, out, out) uint8; pixels outside the raster are 0."""
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    if win == out_size:
        resampling = Resampling.nearest
    else:
        resampling = Resampling.average if win > out_size else Resampling.bilinear
    W, H = src.width, src.height
    c0, r0, c1, r1 = max(0, col_off), max(0, row_off), min(W, col_off + win), min(H, row_off + win)
    out = np.zeros((len(bands), out_size, out_size), dtype=np.uint8)
    if c1 <= c0 or r1 <= r0:
        return out
    if (c0, r0, c1, r1) == (col_off, row_off, col_off + win, row_off + win):
        return src.read(list(bands), window=Window(col_off, row_off, win, win),
                        out_shape=(len(bands), out_size, out_size), resampling=resampling)
    s = out_size / win
    oc0, or0 = int(round((c0 - col_off) * s)), int(round((r0 - row_off) * s))
    oc1, or1 = int(round((c1 - col_off) * s)), int(round((r1 - row_off) * s))
    if oc1 <= oc0 or or1 <= or0:
        return out
    out[:, or0:or1, oc0:oc1] = src.read(list(bands), window=Window(c0, r0, c1 - c0, r1 - r0),
                                        out_shape=(len(bands), or1 - or0, oc1 - oc0), resampling=resampling)
    return out


@dataclass
class ReadStats:
    items: int = 0
    windows: int = 0
    ok: int = 0
    failed: int = 0
    retries: int = 0
    transform_mismatch: int = 0
    seconds: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, **kw: int) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)


class GroupedReader:
    """Read many square windows, grouped by NAIP item, with a thread pool.

    ``handle(key, image_hwc_or_None, error)`` runs in the worker thread for
    every window (decode/encode/write there so big arrays never pile up).
    """

    def __init__(self, cat: NaipCatalog, workers: int = 48, retries: int = 5, backoff_s: float = 1.0,
                 max_per_task: int = 256, bands: tuple[int, ...] = (1, 2, 3)) -> None:
        self.cat = cat
        self.workers = workers
        self.retries = retries
        self.backoff_s = backoff_s
        self.max_per_task = max_per_task
        self.bands = bands
        self.stats = ReadStats()

    def _sleep(self, attempt: int) -> None:
        self.stats.add(retries=1)
        time.sleep(min(60.0, self.backoff_s * 2 ** attempt) * (0.5 + random.random()))

    def _open(self, item: int) -> Any:
        import rasterio

        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                return rasterio.open(_signed(str(self.cat.hrefs[item])))
            except Exception as exc:  # network / SAS / 5xx
                last = exc
                self._sleep(attempt)
        raise RuntimeError(f"open {self.cat.ids[item]} failed after {self.retries} tries: {last}")

    def _task(self, item: int, idx: np.ndarray, plan: WindowPlan, keys: np.ndarray,
              handle_fn: Callable[[Any, np.ndarray | None, str], Any]) -> list[Any]:
        import rasterio

        def handle(key: Any, img: np.ndarray | None, err: str) -> Any:
            try:
                return handle_fn(key, img, err)
            except Exception as exc:  # a bad chip must not kill the whole task
                log.warning("handler failed for %s: %s: %s", key, type(exc).__name__, exc)
                return None

        results: list[Any] = []
        with rasterio.Env(**GDAL_ENV_BULK):
            try:
                src = self._open(item)
            except Exception as exc:
                self.stats.add(failed=len(idx))
                return [handle(keys[i], None, str(exc)) for i in idx]
            try:
                tf = src.transform
                ref = self.cat.transform[item]
                if (abs(tf.a - ref[0]) > 1e-6 or abs(tf.e - ref[4]) > 1e-6 or abs(tf.c - ref[2]) > 0.01
                        or abs(tf.f - ref[5]) > 0.01):
                    self.stats.add(transform_mismatch=len(idx), failed=len(idx))
                    msg = f"transform mismatch for {self.cat.ids[item]}: {tuple(tf)[:6]} vs {tuple(ref)}"
                    return [handle(keys[i], None, msg) for i in idx]
                for i in idx:
                    img = None
                    err = ""
                    for attempt in range(self.retries):
                        try:
                            data = read_square(src, int(plan.col_off[i]), int(plan.row_off[i]), int(plan.win[i]),
                                               plan.out_size, self.bands)
                            img = np.ascontiguousarray(np.transpose(data, (1, 2, 0)))
                            break
                        except Exception as exc:
                            err = f"{type(exc).__name__}: {exc}"
                            self._sleep(attempt)
                            try:
                                src.close()
                                src = self._open(item)
                            except Exception as exc2:
                                err = f"reopen failed: {exc2}"
                    self.stats.add(windows=1, ok=int(img is not None), failed=int(img is None))
                    results.append(handle(keys[i], img, err))
            finally:
                src.close()
        self.stats.add(items=1)
        return results

    def run(self, plan: WindowPlan, keys: np.ndarray, handle: Callable[[Any, np.ndarray | None, str], Any],
            progress_every: int = 2000) -> Iterator[Any]:
        """Yield handle() results as tasks finish (order not preserved)."""
        t0 = time.monotonic()
        valid = np.flatnonzero(plan.item >= 0)
        order = valid[np.lexsort((plan.col_off[valid], plan.row_off[valid], plan.item[valid]))]
        tasks: list[tuple[int, np.ndarray]] = []
        if len(order):
            items = plan.item[order]
            cuts = np.flatnonzero(np.r_[True, items[1:] != items[:-1], True])
            for s, e in zip(cuts[:-1], cuts[1:]):
                grp = order[s:e]
                for k in range(0, len(grp), self.max_per_task):
                    tasks.append((int(items[s]), grp[k:k + self.max_per_task]))
        random.Random(0).shuffle(tasks)  # spread load over blob partitions
        if tasks:  # warm the SAS token cache once, instead of a burst of token requests from every thread
            _signed(str(self.cat.hrefs[tasks[0][0]]))
        log.info("reading %d windows from %d items in %d tasks with %d threads",
                 len(valid), len(set(t[0] for t in tasks)), len(tasks), self.workers)
        done = 0
        next_report = progress_every
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures: list[Future[list[Any]]] = [pool.submit(self._task, it, idx, plan, keys, handle) for it, idx in tasks]
            for fut in as_completed(futures):
                res = fut.result()
                done += len(res)
                yield from res
                if done >= next_report:
                    el = time.monotonic() - t0
                    log.info("  %d/%d windows (%.1f/s), ok=%d failed=%d retries=%d", done, len(valid), done / max(el, 1e-6),
                             self.stats.ok, self.stats.failed, self.stats.retries)
                    next_report += progress_every
        self.stats.seconds += time.monotonic() - t0
