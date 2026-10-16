"""Scan engine v2: every NAIP pixel of one or more states, fast and resumable.

Work unit = one NAIP item (one COG file). Each location is owned by exactly one item:

* ``plan``       per state, the state's own flights (newest year, then newest date, then id)
                 are ordered by priority; an item's *zone* is its footprint inside the state
                 (buffered ``--state-buffer-m``) minus the footprints of every higher-priority
                 item, so zones never overlap inside a state. Each item's output grid (0.6 m;
                 0.3 m items are read from their 2x overview) is cut into 320 px tiles with a
                 224 px stride (96 px overlap, more than a full court). A tile is kept when its
                 224 px core cell, dilated by one cell, touches the zone (rasterised, so planning
                 all of CONUS takes minutes). One parquet row per item: href, grid, kept-tile
                 bitmask, zone (Albers WKB), expected tiles and bytes.
* ``partition``  splits the items of a plan across N nodes x P processes (longest-first greedy
                 by kept tiles) into ``manifests/node<k>_p<j>.txt``. Static, lock-free: a node
                 only ever touches its own manifest; a dead node's leftovers are re-partitioned
                 with ``partition --pending`` (done markers are the source of truth).
* ``run``        one worker process: ``--threads`` reader threads (128-192 concurrent COG
                 streams, one shared Planetary Computer SAS token) each stream one item at a
                 time in block-aligned strips (every COG block is read once), cut the kept
                 tiles in memory and queue them; a packer thread copies tiles into pinned
                 batches; the GPU thread runs ``FastDetector`` (FP16 PyTorch or a TensorRT
                 10 engine; uint8 -> fp16 normalise/resize, decode, per-tile NMS all on the
                 GPU) and maps boxes to item pixels on the GPU. When an item's last tile is
                 done, a finaliser merges duplicates across tiles (greedy NMS with a
                 containment rule, edge-cut boxes lose ties), drops boxes outside 6-120 m,
                 keeps only boxes whose centre lies in the item's zone, and writes
                 ``results/<STATE>/<item>.parquet`` atomically plus a ``.done.json`` marker.
                 Restarts skip items with a marker, so a crash loses at most the items in
                 flight (under a minute).
* ``merge``      concatenates every per-item file, merges across zone borders within
                 ``--merge-m`` (20 m), marks detections near known OSM basketball features,
                 measures recall on held-out OSM courts (and inside / outside the v1 scan mask),
                 and writes ``candidates.parquet`` + GeoJSON for privacy.py / export_app.
* ``status``     done / pending items and throughput from the markers.

Example (Tennessee, every pixel, 4 nodes)
-------
    python -m hoop_pipeline.scan_engine plan --states TN --out data/scan2/TN
    python -m hoop_pipeline.scan_engine partition --plan data/scan2/TN --nodes 4
    python -m hoop_pipeline.scan_engine run --plan data/scan2/TN --manifest data/scan2/TN/manifests/node0_p0.txt \\
        --weights weights/best_v2.pt --imgsz 416 --threads 160
    python -m hoop_pipeline.scan_engine merge --plan data/scan2/TN --state TN --out out/scan2_tn
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import queue
import random
import socket
import subprocess
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from pyproj import Transformer

from . import config
from .common import add_common_args, setup_logging, stable_detection_id, write_json

log = logging.getLogger("scan_engine")
ALBERS = "EPSG:5070"

# GDAL settings for many concurrent streaming reads (one dataset handle per reader thread).
GDAL_ENV_SCAN: dict[str, Any] = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    GDAL_HTTP_MULTIPLEX="YES",
    GDAL_HTTP_VERSION="2",
    GDAL_HTTP_MAX_RETRY="6",
    GDAL_HTTP_RETRY_DELAY="1",
    GDAL_HTTP_TIMEOUT="90",
    GDAL_INGESTED_BYTES_AT_OPEN="65536",
    VSI_CACHE="FALSE",
    CPL_VSIL_CURL_CACHE_SIZE=str(256 * 1024 * 1024),
    GDAL_CACHEMAX=1024 * 1024 * 1024,
    GDAL_NUM_THREADS="1",
)


def _tf(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


# =========================================================================== plan
def item_priority_order(cat: Any, idx: np.ndarray) -> np.ndarray:
    """Newest year first, then newest acquisition, then id (deterministic)."""
    return idx[np.lexsort((cat.ids[idx].astype(str), -cat.dates[idx].astype("datetime64[s]").astype(np.int64),
                           -cat.years[idx].astype(np.int64)))]


def kept_tile_mask(zone_native: Any, transform: tuple[float, ...], scale: float, stride: int, n_rows: int,
                   n_cols: int, dilate: int = 1) -> np.ndarray:
    """Tiles whose stride-sized core cell (dilated by ``dilate`` cells) touches the zone."""
    from rasterio.features import rasterize
    from rasterio.transform import Affine

    a, _, c, _, e, f = transform
    cell = Affine(a * scale * stride, 0.0, c, 0.0, e * scale * stride, f)
    m = rasterize([(zone_native, 1)], out_shape=(n_rows, n_cols), transform=cell, all_touched=True, dtype="uint8")
    m = m.astype(bool)
    for _ in range(dilate):
        d = m.copy()
        d[1:, :] |= m[:-1, :]
        d[:-1, :] |= m[1:, :]
        d[:, 1:] |= m[:, :-1]
        d[:, :-1] |= m[:, 1:]
        d[1:, 1:] |= m[:-1, :-1]
        d[:-1, :-1] |= m[1:, 1:]
        d[1:, :-1] |= m[:-1, 1:]
        d[:-1, 1:] |= m[1:, :-1]
        m = d
    return m


def cmd_plan(args: argparse.Namespace) -> None:
    from .naip_index import NaipCatalog
    from .states import States

    t0 = time.monotonic()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    cat = NaipCatalog.load(args.naip_dir, min_year=args.min_year)
    states = States.load(args.states_file)
    codes = [c.upper() for c in args.states] if args.states != ["CONUS"] else sorted(states.codes.tolist())
    to_alb = _tf("EPSG:4326", ALBERS)
    stride = args.tile - args.overlap
    rows: dict[str, list[Any]] = {k: [] for k in (
        "item_id", "state", "href", "epsg", "year", "date", "gsd", "t_a", "t_c", "t_e", "t_f", "width", "height",
        "scale", "out_w", "out_h", "n_rows", "n_cols", "kept_bits", "kept_tiles", "zone_km2", "zone_wkb",
        "centroid_lon", "centroid_lat")}
    per_state: dict[str, dict[str, Any]] = {}
    tr_cache: dict[int, Transformer] = {}
    # A state without flights of its own (DC: NAIP has no "dc" items; MD and VA flights cover it) borrows other
    # states' flights. Its rows get item ids "<id>~<ST>" so they never collide with the lender's rows.
    borrow = {k.upper(): [s.upper() for s in v.split(",")] for k, v in
              (x.split("=", 1) for x in (args.flight_states or []))}
    for st in codes:
        ts = time.monotonic()
        st_alb = shapely.buffer(shapely.transform(states.geometry(st), lambda xy: np.column_stack(
            to_alb.transform(xy[:, 0], xy[:, 1]))), args.state_buffer_m)
        if st in borrow:
            idx = np.concatenate([cat.items_for_state(s, args.min_year) for s in borrow[st]])
        else:
            idx = cat.items_for_state(st, args.min_year)
        suffix = f"~{st}" if st in borrow else ""
        coarse = cat.gsd[idx] > args.max_native_gsd
        if coarse.any():
            log.info("%s: ignoring %d items coarser than %.2f m", st, int(coarse.sum()), args.max_native_gsd)
            idx = idx[~coarse]
        if not len(idx):
            log.warning("%s: no NAIP items >= %d", st, args.min_year)
            continue
        order = item_priority_order(cat, idx)
        fps = shapely.transform(cat.geoms[order], lambda xy: np.column_stack(to_alb.transform(xy[:, 0], xy[:, 1])))
        fps = shapely.make_valid(fps)
        tree = shapely.STRtree(fps)
        shapely.prepare(st_alb)
        covered = []
        n_items = n_tiles = 0
        km2 = 0.0
        years: Counter[int] = Counter()
        for k, it in enumerate(order):
            fp = fps[k]
            if not shapely.intersects(st_alb, fp):
                continue
            zone = shapely.intersection(fp, st_alb)
            higher = [j for j in tree.query(fp, predicate="intersects") if j < k]
            if higher:
                zone = shapely.difference(zone, shapely.union_all(fps[higher]))
            if zone.is_empty or zone.area < args.min_zone_m2:
                continue
            native = abs(float(cat.transform[it, 0]))
            scale = round(args.gsd / native, 6)   # native px per output px (2 for 0.3 m items)
            out_w = int(np.floor(int(cat.width[it]) / scale))
            out_h = int(np.floor(int(cat.height[it]) / scale))
            n_cols = max(1, int(np.ceil(max(0, out_w - args.tile) / stride)) + 1)
            n_rows = max(1, int(np.ceil(max(0, out_h - args.tile) / stride)) + 1)
            code = int(cat.epsg[it])
            if code not in tr_cache:
                tr_cache[code] = _tf(ALBERS, f"EPSG:{code}")
            trn = tr_cache[code]
            zone_n = shapely.transform(zone, lambda xy, trn=trn: np.column_stack(trn.transform(xy[:, 0], xy[:, 1])))
            tf = tuple(float(v) for v in cat.transform[it])
            mask = kept_tile_mask(zone_n, tf, scale, stride, n_rows, n_cols)
            if not mask.any():
                continue
            c = shapely.centroid(zone)
            clon, clat = _tf(ALBERS, "EPSG:4326").transform(c.x, c.y)
            for key, val in (("item_id", str(cat.ids[it]) + suffix), ("state", st), ("href", str(cat.hrefs[it])), ("epsg", code),
                             ("year", int(cat.years[it])), ("date", cat.date_str(int(it))), ("gsd", native),
                             ("t_a", tf[0]), ("t_c", tf[2]), ("t_e", tf[4]), ("t_f", tf[5]),
                             ("width", int(cat.width[it])), ("height", int(cat.height[it])), ("scale", scale),
                             ("out_w", out_w), ("out_h", out_h), ("n_rows", n_rows), ("n_cols", n_cols),
                             ("kept_bits", np.packbits(mask.ravel()).tobytes()), ("kept_tiles", int(mask.sum())),
                             ("zone_km2", zone.area / 1e6), ("zone_wkb", shapely.to_wkb(zone)),
                             ("centroid_lon", float(clon)), ("centroid_lat", float(clat))):
                rows[key].append(val)
            covered.append(zone)
            n_items += 1
            n_tiles += int(mask.sum())
            km2 += zone.area / 1e6
            years[int(cat.years[it])] += int(mask.sum())
        gap = shapely.difference(st_alb, shapely.union_all(covered)).area / 1e6 if covered else st_alb.area / 1e6
        per_state[st] = {"items": n_items, "tiles": n_tiles, "zone_km2": round(km2, 1), "state_km2": round(st_alb.area / 1e6, 1),
                         "uncovered_km2": round(gap, 2), "tiles_by_year": dict(years),
                         "seconds": round(time.monotonic() - ts, 1)}
        log.info("%s: %d items, %d tiles, %.0f km2 (uncovered %.1f km2), years %s, %.1fs", st, n_items, n_tiles, km2,
                 gap, dict(years), time.monotonic() - ts)
    table = pa.table(rows)
    pq.write_table(table, out / "plan.parquet", compression="zstd")
    mb_per_km2 = {"0.6": 8.4, "0.3": 10.8}
    gsds = np.array(rows["gsd"])
    est_gb = float(sum(z * (mb_per_km2["0.3"] if g < 0.45 else mb_per_km2["0.6"]) for z, g in zip(rows["zone_km2"], gsds))) / 1e3
    stats = {"states": per_state, "items": table.num_rows, "tiles": int(sum(rows["kept_tiles"])),
             "zone_km2": round(float(sum(rows["zone_km2"])), 1), "tile_px": args.tile, "overlap_px": args.overlap,
             "gsd": args.gsd, "state_buffer_m": args.state_buffer_m, "est_read_gb_at_measured_MB_per_km2": round(est_gb, 1),
             "seconds": round(time.monotonic() - t0, 1)}
    write_json(out / "plan_stats.json", stats)
    log.info("plan: %d items, %d tiles, %.0f km2, ~%.0f GB to read; %.1f min", stats["items"], stats["tiles"],
             stats["zone_km2"], est_gb, stats["seconds"] / 60)


# =========================================================================== partition
def load_plan(plan_dir: Path, item_ids: set[str] | None = None) -> pa.Table:
    t = pq.read_table(plan_dir / "plan.parquet")
    if item_ids is not None:
        mask = pa.compute.is_in(t.column("item_id"), value_set=pa.array(sorted(item_ids)))
        t = t.filter(mask)
    return t


def done_marker(results: Path, state: str, item_id: str) -> Path:
    return results / state / f"{item_id}.done.json"


def cmd_partition(args: argparse.Namespace) -> None:
    t = load_plan(args.plan)
    ids = t.column("item_id").to_pylist()
    sts = t.column("state").to_pylist()
    tiles = np.array(t.column("kept_tiles").to_pylist())
    results = args.results or args.plan / "results"
    pending = np.ones(len(ids), bool)
    if args.pending:
        pending = np.array([not done_marker(results, s, i).exists() for s, i in zip(sts, ids)])
    if args.done_list:  # item ids finished elsewhere (e.g. pulled from other nodes' packs), one per line
        done_ids: set[str] = set()
        for p in args.done_list:
            done_ids |= {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}
        pending &= np.array([i not in done_ids for i in ids])
    n_bins = args.nodes * args.procs
    load = np.zeros(n_bins)
    bins: list[list[str]] = [[] for _ in range(n_bins)]
    rng = random.Random(0)
    state_order: list[str] = []
    if args.by_state:
        # Every state is balanced across all bins on its own, and every manifest lists its states in the same
        # order (shuffled within a state), so the fleet finishes states one after another and each state's
        # results can be pulled as soon as it is done.
        st_arr = np.asarray(sts, dtype=object)
        st_tiles: Counter[str] = Counter()
        for s_, t_, p_ in zip(sts, tiles.tolist(), pending.tolist()):
            if p_:
                st_tiles[s_] += t_
        first = [s.upper() for s in (args.state_order or [])]
        state_order = [s for s in first if s in st_tiles] + sorted(
            (s for s in st_tiles if s not in first), key=lambda s: -st_tiles[s])
        for s in state_order:
            idx = np.flatnonzero((st_arr == s) & pending)
            sub = np.zeros(n_bins)
            per: list[list[str]] = [[] for _ in range(n_bins)]
            for k in idx[np.argsort(-tiles[idx], kind="stable")]:
                b = int(np.lexsort((load, sub))[0])  # least loaded in this state, then overall
                per[b].append(ids[k])
                sub[b] += tiles[k]
            for b in range(n_bins):
                rng.shuffle(per[b])
                bins[b] += per[b]
                load[b] += sub[b]
    else:
        for k in np.argsort(-tiles):
            if not pending[k]:
                continue
            b = int(np.argmin(load))
            bins[b].append(ids[k])
            load[b] += tiles[k]
    mdir = args.plan / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    for old in mdir.glob("node*_p*.txt"):
        old.unlink()
    for b, items in enumerate(bins):
        if not args.by_state:
            rng.shuffle(items)  # spread each node's reads across the storage account's partitions
        node, proc = divmod(b, args.procs)
        (mdir / f"node{node}_p{proc}.txt").write_text("\n".join(items) + "\n", encoding="utf-8")
    write_json(mdir / "partition.json", {"nodes": args.nodes, "procs": args.procs, "pending_only": args.pending,
                                         "by_state": bool(args.by_state), "state_order": state_order,
                                         "items": int(pending.sum()), "tiles_per_bin": [int(v) for v in load]})
    log.info("partitioned %d items into %d manifests%s; tiles per manifest %s", int(pending.sum()), n_bins,
             " (by state)" if args.by_state else "", [int(v) for v in load])


# =========================================================================== run
@dataclass
class ItemTask:
    idx: int
    item_id: str
    state: str
    href: str
    epsg: int
    date: str
    year: int
    gsd: float
    transform: tuple[float, float, float, float]  # a, c, e, f (native)
    width: int
    height: int
    scale: float
    out_w: int
    out_h: int
    n_rows: int
    n_cols: int
    mask: np.ndarray
    zone_wkb: bytes


@dataclass
class TileChunk:
    item: int
    rows: np.ndarray       # tile row index per tile
    cols: np.ndarray
    tiles: np.ndarray      # (n, T, T, 3) uint8


@dataclass
class ItemEnd:
    item: int
    tiles: int
    skipped_empty: int
    read_s: float
    error: str = ""


@dataclass
class ItemState:
    task: ItemTask
    queued: int = 0
    processed: int = 0
    ended: ItemEnd | None = None
    dets: list[np.ndarray] = field(default_factory=list)
    t_start: float = field(default_factory=time.monotonic)


def tasks_from_plan(t: pa.Table) -> list[ItemTask]:
    out = []
    cols = {k: t.column(k).to_pylist() for k in t.column_names}
    for i in range(t.num_rows):
        nr, nc = cols["n_rows"][i], cols["n_cols"][i]
        mask = np.unpackbits(np.frombuffer(cols["kept_bits"][i], dtype=np.uint8))[: nr * nc].reshape(nr, nc).astype(bool)
        out.append(ItemTask(i, cols["item_id"][i], cols["state"][i], cols["href"][i], int(cols["epsg"][i]), cols["date"][i],
                            int(cols["year"][i]), float(cols["gsd"][i]),
                            (cols["t_a"][i], cols["t_c"][i], cols["t_e"][i], cols["t_f"][i]), int(cols["width"][i]),
                            int(cols["height"][i]), float(cols["scale"][i]), int(cols["out_w"][i]), int(cols["out_h"][i]),
                            nr, nc, mask, cols["zone_wkb"][i]))
    return out


class NetMeter:
    """Bytes received on the default interfaces (/proc/net/dev), sampled on demand."""

    def __init__(self) -> None:
        self.ok = Path("/proc/net/dev").exists()

    def rx(self) -> int:
        if not self.ok:
            return 0
        best = 0
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            name, rest = line.split(":", 1)
            if name.strip() == "lo" or name.strip().startswith(("docker", "veth")):
                continue
            best += int(rest.split()[0])
        return best


class GpuMeter:
    """nvidia-smi sampling once per second into a list (utilisation %, memory MiB)."""

    def __init__(self) -> None:
        self.samples: list[tuple[float, float, float]] = []
        self.proc: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None
        try:
            self.proc = subprocess.Popen(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                                          "--format=csv,noheader,nounits", "-l", "1"], stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True)
        except (OSError, ValueError):
            self.proc = None
            return
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                u, m = (float(v) for v in line.strip().split(",")[:2])
                self.samples.append((time.monotonic(), u, m))
            except ValueError:
                continue

    def mean_util(self, since: float = 0.0) -> float:
        v = [u for t, u, _ in self.samples if t >= since]
        return float(np.mean(v)) if v else float("nan")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()


def read_item(task: ItemTask, tile: int, stride: int, strip_rows: int, out_q: queue.Queue[Any], min_valid: float,
              chunk_rows: int, retries: int = 5) -> None:
    """Stream one item in block-aligned strips and queue its kept tiles (runs in a reader thread)."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    from .naip_index import _signed

    t0 = time.monotonic()
    n_tiles = skipped = 0
    kept_rows = np.flatnonzero(task.mask.any(1))
    cols_any = np.flatnonzero(task.mask.any(0))
    if not len(kept_rows):
        out_q.put(ItemEnd(task.idx, 0, 0, 0.0))
        return
    # output-pixel column range covering every kept tile, aligned to 512 px (COG blocks / overview blocks)
    x0 = (int(cols_any[0]) * stride // 512) * 512
    x1a = int(np.ceil((int(cols_any[-1]) * stride + tile) / 512)) * 512
    x1r = min(task.out_w, x1a)   # readable part; the rest of the band stays zero (padding)
    width = x1a - x0
    s = task.scale
    err = ""
    try:
        with rasterio.Env(**GDAL_ENV_SCAN):
            src = None
            for attempt in range(retries):
                try:
                    src = rasterio.open(_signed(task.href))
                    break
                except Exception as exc:  # network / SAS / 5xx
                    err = f"open: {type(exc).__name__}: {exc}"
                    time.sleep(min(30.0, 2.0 * 2 ** attempt) * (0.5 + random.random()))
            if src is None:
                raise RuntimeError(err)
            try:
                tf = src.transform
                a, c, e, f = task.transform
                if abs(tf.a - a) > 1e-6 or abs(tf.e - e) > 1e-6 or abs(tf.c - c) > 0.01 or abs(tf.f - f) > 0.01:
                    raise RuntimeError(f"transform mismatch {tuple(tf)[:6]} vs plan {task.transform}")
                buf = np.zeros((0, width, 3), dtype=np.uint8)
                b0 = b1 = -1    # buffer covers output rows [b0, b1)
                resampling = Resampling.nearest if s == 1.0 else Resampling.average

                def read_rows(r0: int, r1: int) -> np.ndarray:
                    """Output rows [r0, r1) x [x0, x0 + width) as (rows, width, 3); zero past the raster."""
                    blk = np.zeros((r1 - r0, width, 3), dtype=np.uint8)
                    rr1 = min(r1, task.out_h)
                    if rr1 <= r0 or x1r <= x0:
                        return blk
                    win = Window(int(round(x0 * s)), int(round(r0 * s)), int(round((x1r - x0) * s)),
                                 int(round((rr1 - r0) * s)))
                    for attempt in range(retries):
                        try:
                            data = src.read([1, 2, 3], window=win, out_shape=(3, rr1 - r0, x1r - x0),
                                            resampling=resampling)
                            break
                        except Exception as exc:
                            if attempt == retries - 1:
                                raise
                            log.debug("read retry %s: %s", task.item_id, exc)
                            time.sleep(min(30.0, 2.0 * 2 ** attempt) * (0.5 + random.random()))
                    blk[: rr1 - r0, : x1r - x0] = np.transpose(data, (1, 2, 0))
                    return blk

                pending_rows: list[int] = []
                pending_cols: list[int] = []
                pending_tiles: list[np.ndarray] = []
                for j in kept_rows:
                    y0 = int(j) * stride
                    y1 = y0 + tile
                    if y0 >= b1:  # gap (skipped tile rows) or first row: restart at the strip holding y0
                        b0 = (y0 // strip_rows) * strip_rows
                        b1 = int(np.ceil(y1 / strip_rows)) * strip_rows
                        buf = read_rows(b0, b1)
                    elif y1 > b1:  # extend the buffer down to y1 in block-aligned strips
                        end = int(np.ceil(y1 / strip_rows)) * strip_rows
                        buf = np.concatenate([buf[y0 - b0:], read_rows(b1, end)])
                        b0, b1 = y0, end
                    if y0 > b0:
                        buf = buf[y0 - b0:]
                        b0 = y0
                    band = buf[:tile]
                    valid_band = band.max(axis=2) > 0
                    for ci in np.flatnonzero(task.mask[j]):
                        cx = int(ci) * stride - x0
                        t = band[:, cx:cx + tile]
                        if t.shape[1] < tile:
                            t = np.pad(t, ((0, 0), (0, tile - t.shape[1]), (0, 0)))
                            vf = valid_band[:, cx:cx + tile].sum() / float(tile * tile)
                        else:
                            vf = valid_band[:, cx:cx + tile].mean()
                        if vf < min_valid:
                            skipped += 1
                            continue
                        pending_rows.append(int(j))
                        pending_cols.append(int(ci))
                        pending_tiles.append(t)
                    if len(pending_tiles) >= chunk_rows:
                        out_q.put(TileChunk(task.idx, np.array(pending_rows), np.array(pending_cols), np.stack(pending_tiles)))
                        n_tiles += len(pending_tiles)
                        pending_rows, pending_cols, pending_tiles = [], [], []
                if pending_tiles:
                    out_q.put(TileChunk(task.idx, np.array(pending_rows), np.array(pending_cols), np.stack(pending_tiles)))
                    n_tiles += len(pending_tiles)
            finally:
                src.close()
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc)[:300]}"
        log.warning("item %s failed: %s", task.item_id, err)
    out_q.put(ItemEnd(task.idx, n_tiles, skipped, time.monotonic() - t0, err))


def merge_boxes(boxes: np.ndarray, conf: np.ndarray, edge: np.ndarray, iou_thr: float = 0.3,
                contain_thr: float = 0.6, edge_penalty: float = 0.05) -> np.ndarray:
    """Greedy NMS with a containment rule (truncated boxes from tile edges); returns kept indices."""
    if not len(boxes):
        return np.zeros(0, dtype=np.int64)
    order = np.argsort(-(conf - edge_penalty * edge))
    x0, y0, x1, y1 = boxes.T
    area = np.maximum(0.0, x1 - x0) * np.maximum(0.0, y1 - y0)
    suppressed = np.zeros(len(boxes), bool)
    keep = []
    for i in order:
        if suppressed[i]:
            continue
        keep.append(i)
        ix0 = np.maximum(x0[i], x0)
        iy0 = np.maximum(y0[i], y0)
        ix1 = np.minimum(x1[i], x1)
        iy1 = np.minimum(y1[i], y1)
        inter = np.maximum(0.0, ix1 - ix0) * np.maximum(0.0, iy1 - iy0)
        union = area[i] + area - inter
        small = np.minimum(area[i], area)
        hit = (inter > 0) & ((inter / np.maximum(union, 1e-9) >= iou_thr) | (inter / np.maximum(small, 1e-9) >= contain_thr))
        hit[i] = False
        suppressed |= hit
    return np.array(keep, dtype=np.int64)


DET_SCHEMA = pa.schema([
    ("conf", pa.float32()), ("lon", pa.float64()), ("lat", pa.float64()),
    ("ax0", pa.float32()), ("ay0", pa.float32()), ("ax1", pa.float32()), ("ay1", pa.float32()),
    ("box_w_m", pa.float32()), ("box_h_m", pa.float32()), ("edge", pa.bool_()), ("verify", pa.float32()),
    ("item_id", pa.string()), ("state", pa.string()), ("date", pa.string()), ("tile_row", pa.int32()),
    ("tile_col", pa.int32()),
])


def finalize_item(st: ItemState, results: Path, tile: int, stride: int, gsd: float, min_box_m: float,
                  max_box_m: float, worker: str) -> dict[str, Any]:
    """Merge an item's detections, keep those centred in its zone, write parquet + done marker."""
    task = st.task
    end = st.ended
    assert end is not None
    d = np.concatenate(st.dets) if st.dets else np.zeros((0, 9), np.float32)
    # columns: row, col, x0, y0, x1, y1 (item output px), conf, verify, tile-touch
    n_raw = len(d)
    out_rows: dict[str, list[Any]] = {k: [] for k in DET_SCHEMA.names}
    if n_raw:
        rows_, cols_ = d[:, 0].astype(int), d[:, 1].astype(int)
        bx = d[:, 2:6].astype(np.float64)
        conf = d[:, 6].astype(np.float64)
        verify = d[:, 7]
        tx0, ty0 = cols_ * stride, rows_ * stride
        edge = (((bx[:, 0] - tx0) <= 2) & (cols_ > 0)) | (((bx[:, 1] - ty0) <= 2) & (rows_ > 0)) \
            | (((tx0 + tile - bx[:, 2]) <= 2) & (cols_ < task.n_cols - 1)) \
            | (((ty0 + tile - bx[:, 3]) <= 2) & (rows_ < task.n_rows - 1))
        wm = (bx[:, 2] - bx[:, 0]) * gsd
        hm = (bx[:, 3] - bx[:, 1]) * gsd
        ok = (np.minimum(wm, hm) >= min_box_m) & (np.maximum(wm, hm) <= max_box_m)
        idx = np.flatnonzero(ok)
        keep = idx[merge_boxes(bx[idx], conf[idx], edge[idx].astype(float))]
        if len(keep):
            a, c, e, f = task.transform
            s = task.scale
            X0 = c + bx[keep, 0] * a * s
            X1 = c + bx[keep, 2] * a * s
            Y0 = f + bx[keep, 1] * e * s
            Y1 = f + bx[keep, 3] * e * s
            to_alb = _tf(f"EPSG:{task.epsg}", ALBERS)
            to_ll = _tf(f"EPSG:{task.epsg}", "EPSG:4326")
            cx, cy = (X0 + X1) / 2, (Y0 + Y1) / 2
            ax, ay = to_alb.transform(np.r_[X0, X1, X1, X0], np.r_[Y0, Y0, Y1, Y1])
            k = len(keep)
            ax = np.asarray(ax).reshape(4, k)
            ay = np.asarray(ay).reshape(4, k)
            acx, acy = to_alb.transform(cx, cy)
            zone = shapely.from_wkb(task.zone_wkb)
            inz = shapely.contains_xy(zone, np.asarray(acx), np.asarray(acy))
            lon, lat = to_ll.transform(cx, cy)
            lon, lat = np.asarray(lon), np.asarray(lat)
            for q in np.flatnonzero(inz):
                i = keep[q]
                for key, val in (("conf", conf[i]), ("lon", lon[q]), ("lat", lat[q]), ("ax0", ax[:, q].min()),
                                 ("ay0", ay[:, q].min()), ("ax1", ax[:, q].max()), ("ay1", ay[:, q].max()),
                                 ("box_w_m", wm[i]), ("box_h_m", hm[i]), ("edge", bool(edge[i])),
                                 ("verify", float(verify[i])), ("item_id", task.item_id), ("state", task.state),
                                 ("date", task.date), ("tile_row", rows_[i]), ("tile_col", cols_[i])):
                    out_rows[key].append(val)
    sdir = results / task.state
    sdir.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pydict(out_rows, schema=DET_SCHEMA)
    tmp = sdir / f".{task.item_id}.parquet.tmp"
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(sdir / f"{task.item_id}.parquet")
    info = {"item_id": task.item_id, "state": task.state, "tiles": end.tiles, "skipped_empty_tiles": end.skipped_empty,
            "planned_tiles": int(task.mask.sum()), "raw_detections": n_raw, "detections": table.num_rows,
            "read_seconds": round(end.read_s, 2), "item_seconds": round(time.monotonic() - st.t_start, 2),
            "error": end.error, "worker": worker, "finished": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    if not end.error:
        write_json(done_marker(results, task.state, task.item_id), info, indent=None)
    return info


def cmd_run(args: argparse.Namespace) -> None:
    import torch

    from .fastdet import FastDetector

    t_start = time.monotonic()
    worker = f"{socket.gethostname()}:{os.getpid()}:{args.manifest.stem if args.manifest else 'all'}"
    results: Path = args.results or args.plan / "results"
    results.mkdir(parents=True, exist_ok=True)
    ids = None
    if args.manifest:
        ids = {ln.strip() for ln in args.manifest.read_text(encoding="utf-8").splitlines() if ln.strip()}
    tasks = tasks_from_plan(load_plan(args.plan, ids))
    todo = [t for t in tasks if not done_marker(results, t.state, t.item_id).exists()]
    n_done = len(tasks) - len(todo)
    if args.max_items:
        todo = todo[: args.max_items]
    if args.manifest:  # keep the manifest's (shuffled) order
        order = {i: k for k, i in enumerate(ln.strip() for ln in args.manifest.read_text().splitlines() if ln.strip())}
        todo.sort(key=lambda t: order.get(t.item_id, 0))
    planned_tiles = int(sum(int(t.mask.sum()) for t in todo))
    log.info("%s: %d items in plan/manifest, %d already done, %d to do (%d tiles)", worker, len(tasks),
             n_done, len(todo), planned_tiles)
    if not todo:
        return
    tile, stride = args.tile, args.tile - args.overlap
    det = None
    if not args.read_only:
        det = FastDetector(args.weights, imgsz=args.imgsz, engine=args.engine, tta=args.tta)
    device = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
    verifier = None
    if args.verifier and det is not None:
        from .cropcls import CropClassifier

        verifier = CropClassifier.load(args.verifier, device=device)

    tile_q: queue.Queue[Any] = queue.Queue(maxsize=args.queue_chunks)
    states: dict[int, ItemState] = {t.idx: ItemState(t) for t in todo}
    task_by_idx = {t.idx: t for t in todo}
    net = NetMeter()
    gpu = GpuMeter()
    rx0 = net.rx()
    counters: Counter[str] = Counter()
    lock = threading.Lock()
    finished: list[dict[str, Any]] = []

    # ---------------------------------------------------------- readers
    reader_pool = ThreadPoolExecutor(max_workers=args.threads, thread_name_prefix="rd")
    for t in todo:
        reader_pool.submit(read_item, t, tile, stride, args.strip_rows, tile_q, args.min_valid, args.chunk_tiles)

    # ---------------------------------------------------------- packer: queue -> pinned batches
    B = args.batch
    n_buf = 3
    pin = torch.cuda.is_available() and not args.read_only
    bufs = [torch.empty((B, tile, tile, 3), dtype=torch.uint8) for _ in range(n_buf)]
    if pin:
        bufs = [b.pin_memory() for b in bufs]
    free_q: queue.Queue[int] = queue.Queue()
    for k in range(n_buf):
        free_q.put(k)
    ready_q: queue.Queue[Any] = queue.Queue(maxsize=n_buf)
    total_items = len(todo)

    def packer() -> None:
        ended = 0
        carry: TileChunk | None = None
        while True:
            k = free_q.get()
            n = 0
            meta_item: list[np.ndarray] = []
            meta_row: list[np.ndarray] = []
            meta_col: list[np.ndarray] = []
            ends: list[ItemEnd] = []
            arr = bufs[k].numpy()
            deadline = None
            while n < B:
                if carry is None:
                    try:
                        timeout = 0.05 if n else 1.0
                        obj = tile_q.get(timeout=timeout)
                    except queue.Empty:
                        if n:
                            break
                        if ended >= total_items:
                            break
                        continue
                    if isinstance(obj, ItemEnd):
                        ends.append(obj)
                        ended += 1
                        if ended >= total_items and n == 0:
                            break
                        continue
                    carry = obj
                take = min(B - n, len(carry.tiles))
                arr[n:n + take] = carry.tiles[:take]
                meta_item.append(np.full(take, carry.item, dtype=np.int64))
                meta_row.append(carry.rows[:take])
                meta_col.append(carry.cols[:take])
                with lock:
                    states[carry.item].queued += take
                n += take
                if take < len(carry.tiles):
                    carry = TileChunk(carry.item, carry.rows[take:], carry.cols[take:], carry.tiles[take:])
                else:
                    carry = None
                if deadline is None:
                    deadline = time.monotonic() + 0.5
                elif time.monotonic() > deadline and tile_q.empty():
                    break
            item_arr = np.concatenate(meta_item) if meta_item else np.zeros(0, np.int64)
            ready_q.put((k, n, item_arr, np.concatenate(meta_row) if meta_row else np.zeros(0, np.int64),
                         np.concatenate(meta_col) if meta_col else np.zeros(0, np.int64), ends))
            if ended >= total_items and carry is None and tile_q.empty():
                ready_q.put(None)
                return

    pk = threading.Thread(target=packer, name="packer", daemon=True)
    pk.start()
    fin_pool = ThreadPoolExecutor(max_workers=args.finalizers, thread_name_prefix="fin")

    def maybe_finalize(item: int) -> None:
        stt = states[item]
        if stt.ended is not None and stt.processed >= stt.queued and stt.processed >= stt.ended.tiles:
            states.pop(item, None)
            fut = fin_pool.submit(finalize_item, stt, results, tile, stride, args.gsd, args.min_box_m, args.max_box_m, worker)
            fut.add_done_callback(lambda f: finished.append(f.result()) if not f.exception() else
                                  log.error("finalize failed: %s", f.exception()))

    # ---------------------------------------------------------- GPU loop
    t_loop = time.monotonic()
    last_report = t_loop
    n_tiles = 0
    gpu_s = 0.0
    window: deque[tuple[float, int, int]] = deque(maxlen=120)
    while True:
        got = ready_q.get()
        if got is None:
            break
        k, n, item_arr, row_arr, col_arr, ends = got
        if n:
            t_g = time.monotonic()
            if det is not None:
                x = bufs[k][:n].to(device, non_blocking=True)
                xp = det.preprocess(x)
                out = det.detect(x, conf=args.conf, iou=args.iou, x_pre=xp)
                if verifier is not None and len(out):
                    vmask = out[:, 5] >= args.verify_min
                    v = torch.full((len(out),), float("nan"), device=out.device)
                    if vmask.any():
                        v[vmask] = verifier.score_boxes(xp, out[vmask], tile_px=tile)
                else:
                    v = torch.full((len(out),), float("nan"), device=out.device)
                o = out.cpu().numpy()
                vv = v.cpu().numpy()
            else:
                o = np.zeros((0, 6), np.float32)
                vv = np.zeros(0, np.float32)
            gpu_s += time.monotonic() - t_g
            free_q.put(k)
            n_tiles += n
            ti = o[:, 0].astype(np.int64) if len(o) else np.zeros(0, np.int64)
            items_here, counts = np.unique(item_arr, return_counts=True)
            with lock:
                for it, cnt in zip(items_here, counts):
                    states[int(it)].processed += int(cnt)
            if len(o):
                its = item_arr[ti]
                rows_ = row_arr[ti]
                cols_ = col_arr[ti]
                ox = o[:, 1:5] + np.column_stack([cols_ * stride, rows_ * stride, cols_ * stride, rows_ * stride])
                packed = np.column_stack([rows_, cols_, ox, o[:, 5], vv, np.zeros(len(o))]).astype(np.float32)
                for it in np.unique(its):
                    states[int(it)].dets.append(packed[its == it])
            for it in items_here:
                maybe_finalize(int(it))
        else:
            free_q.put(k)
        for e_ in ends:
            if e_.item in states:
                states[e_.item].ended = e_
                if e_.error:
                    counters["item_errors"] += 1
                maybe_finalize(e_.item)
        now = time.monotonic()
        rx = net.rx()
        window.append((now, n_tiles, rx))
        if now - last_report > args.report_s:
            last_report = now
            w0 = window[0]
            rate = (n_tiles - w0[1]) / max(1e-6, now - w0[0])
            mbs = (rx - w0[2]) / 1e6 / max(1e-6, now - w0[0])
            log.info("%d/%d items, %d tiles (%.0f/s recent, %.0f/s avg), %.0f MB/s recent, %.0f MB avg/s, GPU %.0f%% "
                     "(busy %.0f%%), queue %d", len(finished), total_items, n_tiles, rate,
                     n_tiles / (now - t_loop), mbs, (rx - rx0) / 1e6 / (now - t_loop), gpu.mean_util(now - 60),
                     100 * gpu_s / (now - t_loop), tile_q.qsize())
    reader_pool.shutdown(wait=True)
    # items whose ItemEnd arrived after the last batch
    for it in list(states):
        if states[it].ended is not None:
            maybe_finalize(it)
    fin_pool.shutdown(wait=True)
    el = time.monotonic() - t_loop
    rx = net.rx() - rx0
    gpu.stop()
    errs = [f for f in finished if f.get("error")]
    summary = {
        "worker": worker, "items_done": len(finished) - len(errs), "item_errors": len(errs), "tiles": n_tiles,
        "planned_tiles": planned_tiles, "seconds": round(el, 1), "tiles_per_s": round(n_tiles / max(el, 1e-6), 1),
        "MB_received": round(rx / 1e6, 1), "MB_per_s": round(rx / 1e6 / max(el, 1e-6), 1),
        "gpu_util_mean": round(gpu.mean_util(t_loop), 1), "gpu_busy_frac": round(gpu_s / max(el, 1e-6), 3),
        "threads": args.threads, "batch": B, "imgsz": det.imgsz if det else None,
        "backend": det.info.backend if det else "read-only", "tta": args.tta, "verifier": str(args.verifier or ""),
        "detections": int(sum(f["detections"] for f in finished)),
        "total_seconds": round(time.monotonic() - t_start, 1),
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "errors": [f"{f['item_id']}: {f['error']}" for f in errs][:20],
    }
    wdir = results / "_workers"
    wdir.mkdir(parents=True, exist_ok=True)
    write_json(wdir / f"{worker.replace(':', '_')}.json", summary)
    log.info("worker done: %s", json.dumps({k: v for k, v in summary.items() if k != "errors"}))
    del task_by_idx


# =========================================================================== merge
def cmd_merge(args: argparse.Namespace) -> None:
    from .geoparquet import read_geoparquet

    t0 = time.monotonic()
    results: Path = args.results or args.plan / "results"
    plan = pq.read_table(args.plan / "plan.parquet", columns=["item_id", "state", "kept_tiles", "zone_km2"])
    plan_ids = plan.column("item_id").to_pylist()
    plan_states = plan.column("state").to_pylist()
    if args.packed:
        # packed per-state files from `pack` (one pair per state and node): detections + the done items
        files = sorted(p for p in args.packed.glob("*.det.parquet") if not p.name.startswith("."))
        done_ids: set[str] = set()
        for p in sorted(args.packed.glob("*.items.parquet")):
            done_ids |= set(pq.read_table(p, columns=["item_id"]).column("item_id").to_pylist())
        done = [i in done_ids for i in plan_ids]
    else:
        # only finished items: a failed item still writes its (partial) parquet, but never a done marker
        files = sorted(p for p in results.glob("*/*.parquet") if not p.name.startswith(".")
                       and (p.parent / (p.name[: -len(".parquet")] + ".done.json")).exists())
        done = [done_marker(results, s, i).exists() for s, i in zip(plan_states, plan_ids)]
    log.info("%d result files; %d of %d planned items done", len(files), sum(done), len(plan_ids))
    tables = [pq.read_table(p) for p in files]
    t = pa.concat_tables(tables) if tables else pa.Table.from_pydict({k: [] for k in DET_SCHEMA.names}, schema=DET_SCHEMA)
    if args.min_conf > 0:
        t = t.filter(pa.compute.greater_equal(t.column("conf"), args.min_conf))
    del tables
    d = {k: t.column(k).to_numpy(zero_copy_only=False) for k in t.column_names}
    n = t.num_rows
    conf = d["conf"].astype(float)
    boxes = np.column_stack([d["ax0"], d["ay0"], d["ax1"], d["ay1"]]).astype(float)
    cx, cy = (boxes[:, 0] + boxes[:, 2]) / 2, (boxes[:, 1] + boxes[:, 3]) / 2
    # cross-item merge: greedy by score, suppress boxes overlapping (IoU/containment) or centred within merge_m.
    # All candidate pairs come from one bulk STRtree query; the greedy pass then only walks each box's neighbours.
    order = np.argsort(-(conf - 0.05 * d["edge"].astype(float)), kind="stable")
    _, item_code = np.unique(d["item_id"].astype(str), return_inverse=True)
    area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    radius = np.maximum(args.merge_m, 0.5 * np.maximum(boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]))
    pts = shapely.points(cx, cy)
    tree = shapely.STRtree(pts)
    qi, qj = tree.query(pts, predicate="dwithin", distance=float(radius.max()) if n else 0.0)
    m = qi != qj
    qi, qj = qi[m], qj[m]
    dist = np.hypot(cx[qi] - cx[qj], cy[qi] - cy[qj])
    ix = np.clip(np.minimum(boxes[qi, 2], boxes[qj, 2]) - np.maximum(boxes[qi, 0], boxes[qj, 0]), 0, None)
    iy = np.clip(np.minimum(boxes[qi, 3], boxes[qj, 3]) - np.maximum(boxes[qi, 1], boxes[qj, 1]), 0, None)
    inter = ix * iy
    # the distance rule is for zone-border duplicates (same court seen by two items); inside an item,
    # tile-level NMS already ran and side-by-side courts ~18 m apart must survive
    close = (item_code[qi] != item_code[qj]) & (dist <= args.merge_m)
    iou = inter / np.maximum(area[qi] + area[qj] - inter, 1e-9)
    over = (iou >= 0.3) | (inter / np.maximum(np.minimum(area[qi], area[qj]), 1e-9) >= 0.6)
    hit = (dist <= radius[qi]) & (close | over)  # i suppresses j
    qi, qj = qi[hit], qj[hit]
    srt = np.argsort(qi, kind="stable")
    qi, qj = qi[srt], qj[srt]
    indptr = np.searchsorted(qi, np.arange(n + 1))
    suppressed = np.zeros(n, bool)
    keep = []
    for i in order.tolist():
        if suppressed[i]:
            continue
        keep.append(i)
        a_, b_ = indptr[i], indptr[i + 1]
        if b_ > a_:
            suppressed[qj[a_:b_]] = True
    keep_a = np.array(keep, dtype=np.int64)
    keep_a = keep_a[np.argsort(-conf[keep_a], kind="stable")]
    log.info("merge: %d detections -> %d after cross-item merge (%.0f m; %d neighbour pairs)", n, len(keep_a),
             args.merge_m, len(qi))

    # known OSM basketball features nearby
    a, g = read_geoparquet(args.courts, columns=["osm_ref", "state", "label_ok", "center_lon", "center_lat"])
    to_alb = _tf("EPSG:4326", ALBERS)
    g_alb = shapely.transform(g, lambda xy: np.column_stack(to_alb.transform(xy[:, 0], xy[:, 1])))
    ktree = shapely.STRtree(g_alb)
    det_geoms = shapely.box(boxes[keep_a, 0], boxes[keep_a, 1], boxes[keep_a, 2], boxes[keep_a, 3])
    centers = shapely.points(cx[keep_a], cy[keep_a])
    near_known = np.full(len(keep_a), "", dtype=object)
    qi, qk = ktree.query(centers, predicate="dwithin", distance=args.known_radius)
    for i_, k_ in zip(qi, qk):
        near_known[i_] = near_known[i_] or str(a["osm_ref"][k_])
    qi2, qk2 = ktree.query(det_geoms, predicate="intersects")
    for i_, k_ in zip(qi2, qk2):
        near_known[i_] = near_known[i_] or str(a["osm_ref"][k_])

    # recall on held-out OSM courts of the scanned states (labelable polygons), inside/outside the v1 mask
    recall: dict[str, Any] = {}
    states_scanned = sorted(set(plan_states))
    sel = np.isin(a["state"].astype(str), states_scanned) & a["label_ok"].astype(bool)
    # only courts inside the zones of finished items count (a partial run must not look like low recall)
    zt = pq.read_table(args.plan / "plan.parquet", columns=["item_id", "zone_wkb"])
    zdone = [shapely.from_wkb(w) for w, ok_ in zip(zt.column("zone_wkb").to_pylist(), done) if ok_]
    del zt
    if zdone:
        ztree = shapely.STRtree(zdone)
        cx_alb, cy_alb = to_alb.transform(a["center_lon"].astype(float), a["center_lat"].astype(float))
        hit_i, _ = ztree.query(shapely.points(cx_alb, cy_alb), predicate="within")
        in_done = np.zeros(len(sel), bool)
        in_done[hit_i] = True
        sel &= in_done
    court_idx = np.flatnonzero(sel)
    v1_mask = None
    if args.v1_mask and args.v1_mask.exists():
        from .common import read_json

        mfc = read_json(args.v1_mask)
        v1_mask = shapely.union_all([shapely.from_geojson(json.dumps(f["geometry"])) for f in mfc["features"]])
        shapely.prepare(v1_mask)
    if len(court_idx):
        court_buf = shapely.buffer(g_alb[court_idx], 5.0)
        in_v1 = (shapely.intersects_xy(v1_mask, a["center_lon"][court_idx], a["center_lat"][court_idx])
                 if v1_mask is not None else np.zeros(len(court_idx), bool))
        dtree = shapely.STRtree(det_geoms)
        ci, di = dtree.query(court_buf, predicate="intersects")
        best = np.zeros(len(court_idx))
        for c_, d_ in zip(ci, di):
            best[c_] = max(best[c_], conf[keep_a[d_]])
        thr = (0.1, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7)
        for name, m in (("all", np.ones(len(court_idx), bool)), ("inside_v1_mask", in_v1), ("outside_v1_mask", ~in_v1)):
            if not m.any() or (name != "all" and v1_mask is None):
                continue
            recall[name] = {"courts": int(m.sum()), **{f"{t:.2f}": round(float((best[m] >= t).mean()), 4) for t in thr}}
        cst = a["state"][court_idx].astype(str)
        recall["by_state"] = {s: {"courts": int((cst == s).sum()),
                                  **{f"{t:.2f}": round(float((best[cst == s] >= t).mean()), 4) for t in thr}}
                              for s in sorted(set(cst.tolist()))}
        recall["court_best_conf"] = {str(a["osm_ref"][court_idx[i]]): round(float(best[i]), 4) for i in range(len(court_idx))}
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    k = keep_a
    ids = []
    seen_ids: Counter[str] = Counter()
    for la, lo in zip(d["lat"][k], d["lon"][k]):
        fid = stable_detection_id(float(la), float(lo))
        seen_ids[fid] += 1
        ids.append(fid if seen_ids[fid] == 1 else f"{fid}-{seen_ids[fid]}")
    cand = {
        "id": ids, "conf": conf[k], "lon": d["lon"][k], "lat": d["lat"][k], "box_w_m": d["box_w_m"][k],
        "box_h_m": d["box_h_m"][k], "ax0": boxes[k, 0], "ay0": boxes[k, 1], "ax1": boxes[k, 2], "ay1": boxes[k, 3],
        "edge": d["edge"][k], "verify": d["verify"][k], "item_id": d["item_id"][k], "state": d["state"][k],
        "date": d["date"][k], "near_known_osm": near_known,
    }
    pq.write_table(pa.table(cand), out / "detections.parquet", compression="zstd")
    fresh = near_known == ""
    wdir = results / "_workers"
    workers = [json.loads(p.read_text()) for p in wdir.glob("*.json")] if wdir.exists() else []
    stats = {
        "states": states_scanned, "items_planned": len(plan_ids), "items_done": int(sum(done)),
        "tiles_planned": int(sum(plan.column("kept_tiles").to_pylist())),
        "raw_item_detections": n, "merged": len(keep_a), "near_known_osm": int((~fresh).sum()),
        "candidates": int(fresh.sum()),
        "candidates_by_threshold": {f"{t:.2f}": int(((conf[k] >= t) & fresh).sum())
                                    for t in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)},
        "known_court_recall": {kk: v for kk, v in recall.items() if kk != "court_best_conf"},
        "workers": workers, "merge_m": args.merge_m, "known_radius_m": args.known_radius,
        "seconds": round(time.monotonic() - t0, 1),
    }
    write_json(out / "merge_stats.json", stats)
    if recall.get("court_best_conf"):
        write_json(out / "court_best_conf.json", recall["court_best_conf"], indent=None)
    log.info("merge: %s", json.dumps({kk: v for kk, v in stats.items() if kk != "workers"}))


# =========================================================================== status
def read_manifest(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def cmd_status(args: argparse.Namespace) -> None:
    results: Path = args.results or args.plan / "results"
    plan = pq.read_table(args.plan / "plan.parquet", columns=["item_id", "state", "kept_tiles"])
    if args.manifest:
        plan = plan.filter(pa.compute.is_in(plan.column("item_id"), value_set=pa.array(read_manifest(args.manifest))))
    ids, sts, tiles = (plan.column(c).to_pylist() for c in ("item_id", "state", "kept_tiles"))
    done = np.array([done_marker(results, s, i).exists() for s, i in zip(sts, ids)], dtype=bool)
    tiles_a = np.array(tiles)
    print(json.dumps({"items": len(ids), "done": int(done.sum()), "pending": int((~done).sum()),
                      "tiles": int(tiles_a.sum()), "tiles_done": int(tiles_a[done].sum())}))


# =========================================================================== plan-merge
def cmd_plan_merge(args: argparse.Namespace) -> None:
    """Concatenate plans (e.g. CONUS without DC + a DC plan that borrows MD/VA flights) into one."""
    tables = [pq.read_table(p / "plan.parquet") for p in args.plans]
    t = pa.concat_tables(tables)
    ids = t.column("item_id").to_pylist()
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate item ids across plans")
    args.out.mkdir(parents=True, exist_ok=True)
    pq.write_table(t, args.out / "plan.parquet", compression="zstd")
    stats: dict[str, Any] = {"states": {}, "merged_from": [str(p) for p in args.plans]}
    for p in args.plans:
        s = json.loads((p / "plan_stats.json").read_text(encoding="utf-8"))
        stats["states"].update(s.get("states", {}))
        for k in ("tile_px", "overlap_px", "gsd", "state_buffer_m"):
            stats.setdefault(k, s.get(k))
    stats.update({"items": t.num_rows, "tiles": int(sum(t.column("kept_tiles").to_pylist())),
                  "zone_km2": round(float(sum(t.column("zone_km2").to_pylist())), 1)})
    write_json(args.out / "plan_stats.json", stats)
    log.info("plan-merge: %d items, %d tiles, %d states -> %s", stats["items"], stats["tiles"], len(stats["states"]),
             args.out)


# =========================================================================== pack
def cmd_pack(args: argparse.Namespace) -> None:
    """Pack one node's finished items per state into a few files that are cheap to pull:

    ``<out>/<ST>.<node>.det.parquet`` (every detection of the node's done items of that state) and
    ``<ST>.<node>.items.parquet`` (one row per done item: tiles, detections, seconds, worker). When all of the
    node's items of a state are done the pair is final (``<ST>.<node>.final`` marker, never rewritten);
    before that it is a partial pack, rewritten only when more items have finished."""
    results: Path = args.results or args.plan / "results"
    order = read_manifest(args.manifest)
    plan = pq.read_table(args.plan / "plan.parquet", columns=["item_id", "state"])
    st_of = dict(zip(plan.column("item_id").to_pylist(), plan.column("state").to_pylist()))
    by_state: dict[str, list[str]] = {}
    for i in order:
        by_state.setdefault(st_of[i], []).append(i)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {}
    for st, items in by_state.items():
        stem = out / f"{st}.{args.node}"
        final = stem.with_suffix(stem.suffix + ".final")
        if final.exists():
            summary[st] = "final"
            continue
        done = [i for i in items if done_marker(results, st, i).exists()]
        if not done:
            continue
        cnt = stem.with_suffix(stem.suffix + ".count")
        prev = int(cnt.read_text()) if cnt.exists() else -1
        if len(done) == prev and len(done) < len(items):
            summary[st] = f"{len(done)}/{len(items)}"
            continue
        dets = [pq.read_table(results / st / f"{i}.parquet") for i in done]
        tab = pa.concat_tables(dets) if dets else pa.Table.from_pylist([], schema=DET_SCHEMA)
        info = [json.loads(done_marker(results, st, i).read_text(encoding="utf-8")) for i in done]
        keys = ["item_id", "state", "tiles", "skipped_empty_tiles", "planned_tiles", "raw_detections", "detections",
                "read_seconds", "item_seconds", "worker", "finished"]
        itab = pa.table({k: [r.get(k) for r in info] for k in keys})
        for name, t in (("det", tab), ("items", itab)):
            tmp = out / f".{st}.{args.node}.{name}.parquet.tmp"
            pq.write_table(t, tmp, compression="zstd")
            tmp.replace(out / f"{st}.{args.node}.{name}.parquet")
        if len(done) == len(items):
            final.write_text(dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds") + "\n")
            cnt.unlink(missing_ok=True)
            summary[st] = "final"
        else:
            cnt.write_text(str(len(done)))
            summary[st] = f"{len(done)}/{len(items)}"
    print(json.dumps(summary))


# =========================================================================== CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--states", nargs="+", default=["TN"], help="USPS codes, or CONUS")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--naip-dir", type=Path, default=config.NAIP_GEOPARQUET_DIR)
    p.add_argument("--min-year", type=int, default=config.NAIP_MIN_YEAR)
    p.add_argument("--states-file", type=Path, default=config.STATES_FILE)
    p.add_argument("--state-buffer-m", type=float, default=100.0)
    p.add_argument("--min-zone-m2", type=float, default=500.0)
    p.add_argument("--max-native-gsd", type=float, default=0.65, help="ignore coarser (pre-2018 1 m) items")
    p.add_argument("--flight-states", nargs="*", help="ST=A,B: plan state ST from flights of A and B (DC=MD,VA)")
    p.add_argument("--tile", type=int, default=config.CHIP_SIZE_PX)
    p.add_argument("--overlap", type=int, default=config.SCAN_OVERLAP_PX)
    p.add_argument("--gsd", type=float, default=config.TARGET_GSD_M)
    add_common_args(p)
    p = sub.add_parser("partition")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--nodes", type=int, default=1)
    p.add_argument("--procs", type=int, default=1, help="worker processes per node")
    p.add_argument("--pending", action="store_true", help="only items without a done marker in --results")
    p.add_argument("--results", type=Path)
    p.add_argument("--done-list", type=Path, nargs="*", help="files listing item ids already done elsewhere")
    p.add_argument("--by-state", action="store_true",
                   help="balance every state across all manifests and order each manifest state by state")
    p.add_argument("--state-order", nargs="*", help="states to put first (the rest follow by planned tiles, largest first)")
    add_common_args(p)
    p = sub.add_parser("run")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--manifest", type=Path, help="item ids to process (default: whole plan)")
    p.add_argument("--results", type=Path, help="default <plan>/results")
    p.add_argument("--weights", type=Path, default=config.DEFAULT_WEIGHTS)
    p.add_argument("--imgsz", type=int, default=None, help="model input size (default: the model's training size)")
    p.add_argument("--engine", type=Path, help="TensorRT engine (static batch = --batch)")
    p.add_argument("--tta", default="none", choices=["none", "lr", "lrud"])
    p.add_argument("--verifier", type=Path, help="crop-classifier weights (cropcls) scored in-stream")
    p.add_argument("--verify-min", type=float, default=0.1)
    p.add_argument("--conf", type=float, default=0.05)
    p.add_argument("--iou", type=float, default=0.5)
    p.add_argument("--threads", type=int, default=160)
    p.add_argument("--batch", type=int, default=512)
    p.add_argument("--strip-rows", type=int, default=512, help="output rows per read (multiple of 512)")
    p.add_argument("--chunk-tiles", type=int, default=64, help="tiles per queue message")
    p.add_argument("--queue-chunks", type=int, default=600, help="max queued tile messages (memory bound)")
    p.add_argument("--min-valid", type=float, default=0.02, help="skip tiles with less valid (non-zero) imagery")
    p.add_argument("--finalizers", type=int, default=4)
    p.add_argument("--min-box-m", type=float, default=6.0)
    p.add_argument("--max-box-m", type=float, default=120.0)
    p.add_argument("--tile", type=int, default=config.CHIP_SIZE_PX)
    p.add_argument("--overlap", type=int, default=config.SCAN_OVERLAP_PX)
    p.add_argument("--gsd", type=float, default=config.TARGET_GSD_M)
    p.add_argument("--max-items", type=int, default=0)
    p.add_argument("--read-only", action="store_true", help="benchmark: read and tile only, no GPU")
    p.add_argument("--report-s", type=float, default=20.0)
    add_common_args(p)
    p = sub.add_parser("merge")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--results", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--courts", type=Path, default=config.OSM_US_DIR / "courts_us.parquet")
    p.add_argument("--known-radius", type=float, default=config.KNOWN_COURT_RADIUS_M)
    p.add_argument("--merge-m", type=float, default=20.0)
    p.add_argument("--v1-mask", type=Path, default=config.SCAN_DIR / "TN" / "mask.geojson")
    p.add_argument("--packed", type=Path, help="read `pack` output (*.det.parquet / *.items.parquet) instead of --results")
    p.add_argument("--min-conf", type=float, default=0.0, help="drop raw detections below this before merging")
    add_common_args(p)
    p = sub.add_parser("status")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--results", type=Path)
    p.add_argument("--manifest", type=Path, help="only the items of this manifest")
    add_common_args(p)
    p = sub.add_parser("plan-merge")
    p.add_argument("--plans", type=Path, nargs="+", required=True)
    p.add_argument("--out", type=Path, required=True)
    add_common_args(p)
    p = sub.add_parser("pack")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--results", type=Path)
    p.add_argument("--out", type=Path, required=True)
    add_common_args(p)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    {"plan": cmd_plan, "partition": cmd_partition, "run": cmd_run, "merge": cmd_merge, "status": cmd_status,
     "plan-merge": cmd_plan_merge, "pack": cmd_pack}[args.cmd](args)


if __name__ == "__main__":
    main()
