# Hoop Hunter satellite court-detection pipeline

Finds outdoor basketball courts that are **not yet on OpenStreetMap** by scanning
free aerial imagery with a small object detector. Courts already in OSM are
used as free training labels. The results feed the app as `candidate` courts
for people to confirm or reject.

```
fetch_osm ──► build_dataset ──► train ──► scan ──► export_app ──► app/assets/data/courts.geojson
   (OSM)        (NAIP chips)    (YOLO)   (NAIP)     (+ NYC Parks)
```

That is the small-scale path (Overpass, a few metros, runs on a laptop). The
**nationwide path** (Geofabrik extract, every US court, a rented GPU) is in
[Nationwide workflow](#nationwide-workflow-lambda-cloud-gpu) below:

```
osm_extract ──► build_dataset_us ──► train ──► evaluate ──► scan_state ──► export_app
 (Geofabrik)   (NAIP GeoParquet      (YOLO)    (TN holdout)  (masked statewide
               index, grouped reads)                         scan)
```

**v2 (2026-10-09)** adds three pieces. The privacy filter keeps private home courts off the map.
A cleaner, harder training set builds a 416 px model. A scan engine covers every pixel of a state,
fast and resumable, on several nodes. See [v2 workflow](#v2-workflow-2026-10-09) and
[Second run](#second-run-2026-10-09):

```
osm_extract ─► sites (PAD-US, NCES, Overture, OSM) ─────────────────────────────────┐
build_dataset_us ─► label_clean ─► train (416 px) ─► evaluate                         │
scan_engine plan ─► partition ─► run (N nodes) ─► merge ─► privacy classify ─────────┴─► export_app
gold sample/fetch/annotate/export ─► tools/label/index.html (human labels) ─► gold score
```

Everything lives in `pipeline/`. The only file written outside it is
`../app/assets/data/courts.geojson` (by `export_app`).

## What NAIP is

The USDA **National Agriculture Imagery Program** flies the continental US
every two to three years during the agricultural growing season, so trees are
usually in leaf. Imagery is about **0.6 m per pixel** (0.3 m in some recent
state flights) with four bands: red, green, blue and near-infrared. It is orthorectified and delivered as ~6 x 7 km
quarter-quad tiles. Microsoft Planetary Computer hosts it as cloud-optimized
GeoTIFFs (STAC collection `naip`), so the pipeline never downloads whole tiles.
It reads only the window it needs with HTTP range requests (rasterio plus a
SAS-signed URL from the `planetary-computer` package).

At 0.6 m a regulation full court (28.7 x 15.2 m) is about 48 x 25 px, and its
painted lines are visible (see "Verified on 2026-10-08" below). Chattanooga's
latest NAIP is **2023-04-11, 0.6 m** (Tennessee; Georgia's 2023 flight was
2023-10-07).

## Data sources and licenses

| Source | Used for | License / terms |
|---|---|---|
| OpenStreetMap via Overpass API | court labels; known courts to exclude; `source: "osm"` entries in the app | **ODbL 1.0.** The app must show "© OpenStreetMap contributors" with a link to https://www.openstreetmap.org/copyright. `courts.geojson` contains OSM-derived data, so if it is published as a database, ODbL share-alike applies to that file. Follow the Overpass usage policy: small queries, a User-Agent, and backing off on errors (the pipeline does all three). |
| USDA NAIP via Microsoft Planetary Computer | imagery | **Public domain** (US Government work). Attribution "Imagery: USDA Farm Service Agency NAIP" is appreciated but not required. Planetary Computer's terms of use apply to the hosting. Anonymous access works; set `PC_SDK_SUBSCRIPTION_KEY` for higher rate limits. |
| OpenStreetMap via the Geofabrik US extract (`us-latest.osm.pbf`) | nationwide labels, hard negatives, scan mask, known courts, Tennessee entries in the app | **ODbL 1.0**, same obligations as above. Geofabrik asks that big extracts be downloaded once, not repeatedly. |
| US Census cartographic boundary file (`cb_2023_us_state_500k`) | state of every feature, CONUS filter, TN holdout | **Public domain** (US Government work). |
| USDA Cropland Data Layer 2021 via Planetary Computer (`usda-cdl`) | planning estimate of a paved-area scan mask (`cloud/estimate_paved_fraction.py`); urban / suburban / rural strata of the gold set (`gold sample`) | **Public domain** (US Government work). |
| USGS PAD-US 4.1, Fee layer (national File Geodatabase, ScienceBase item 652d4fc5d34e44db0e2ee45e) | public land for the privacy filter (`sites`) | **Public domain** (US Government work). USGS says PAD-US is not meant for mapping finer than 1:24,000, so a court must be ≥ 20 m inside a PAD-US polygon to count. |
| NCES EDGE school geocodes: public 2024-25, private 2023-24 (PSS), postsecondary 2025-26 (IPEDS) | school and college points for the privacy filter | **Public domain** ("all information contained herein is in the public domain", EDGE tech docs). |
| Overture Maps places, release 2026-09-23.1 | places of worship, community/civic centres, youth organisations, parks, schools, colleges, sports complexes, YMCAs and rec centres (name match) for the privacy filter | **CDLA-Permissive-2.0 and Apache-2.0** (verified 2026-10-09 at docs.overturemaps.org/guides/places: "published under the CDLA Permissive 2.0 and Apache 2.0 licenses... contains no OpenStreetMap data"). Foursquare-sourced rows are Apache-2.0 (NOTICE: opensource.foursquare.com/places-notice-txt); some rows CC0 (AllThePlaces). Only used inside the pipeline: the app gets a derived `site` word, not Overture records. |
| Overture Maps buildings, release 2026-09-23.1 | building footprints around candidates (house-sized vs large buildings) for the privacy filter | **ODbL 1.0** (includes OSM). Only used inside the pipeline. |
| NYC Parks basketball courts (`../assets/NYCcourts.json`) | `source: "nyc_parks"` entries | NYC Open Data, free to use under the NYC Open Data terms of use. |
| 29 official city / county / state court inventories plus Overture `basketball_court` places (`truth_sources.json`) | the independent truth set (`truth`); evaluation only, nothing goes to the app | Per source: CC0, PDDL, CC BY 3.0/4.0, ODbL (Cleveland), Public Domain, or open-data terms without a named license. Every license is quoted in `truth_sources.json`; see [Official court truth set](#official-court-truth-set-truth-2026-10-15). |
| Ultralytics YOLO | detector training and inference | **AGPL-3.0.** Using it in this offline pipeline is fine. The app only ships the GeoJSON it produces, not the model or code. If you ever ship the model inside an app or service, check AGPL obligations or get an Ultralytics Enterprise license. |

Detected candidates are machine-generated from public-domain imagery and are
not OSM data. Before importing any of them into OSM, a human must verify each
one, and the import must follow the OSM import guidelines (or each court can
be added by hand after review).

## Setup (Windows, Git Bash or PowerShell)

```bash
cd pipeline
python3 -m venv .venv                       # Python 3.10+; tested with 3.12.6 (rye)
.venv/Scripts/python -m pip install --no-cache-dir -r requirements.txt
```

* `rasterio` 1.5 / `pyproj` / `shapely` install from PyPI wheels on Windows,
  which bundle GDAL and PROJ, so no separate GDAL install is needed.
* `--no-cache-dir` was needed on the dev machine: pip hit a `MemoryError` while
  hashing cached wheels when free RAM was low.
* `ultralytics` pulls in `torch`. On Windows the default PyPI torch wheel is
  **CPU-only** (`torch 2.x+cpu`).
* The nationwide modules add `pyarrow`, `pyshp` and `pyyaml` (in
  `requirements.txt`). `osm_extract` also needs **osmium-tool** on `PATH`
  (Linux: `apt-get install osmium-tool`), so in practice it runs on the cloud
  instance. Reading its GeoParquet outputs locally only needs `pyarrow` and
  `shapely` (`hoop_pipeline.geoparquet.read_geoparquet`).
* All commands below run from `pipeline/` as `python -m hoop_pipeline.<step>`
  (use `.venv/Scripts/python`, or activate the venv first). Every step has `--help`.
* `hoop_pipeline/__init__.py` points `YOLO_CONFIG_DIR` at `pipeline/.ultralytics/`,
  so Ultralytics settings, fonts and analytics opt-out stay inside the repo
  instead of in `%APPDATA%`.

### GPU training

CPU works for everything, but training a real model on CPU takes most of a
day. To use an NVIDIA GPU (this dev machine has an RTX 4070, 12 GB), install a
CUDA build of torch into the same venv:

```bash
.venv/Scripts/python -m pip install --no-cache-dir --force-reinstall torch torchvision --index-url https://download.pytorch.org/whl/cu128
.venv/Scripts/python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

Pick the `cuXXX` index that matches your driver (see pytorch.org/get-started).
`train` and `scan` use GPU 0 automatically when `torch.cuda.is_available()`,
or you can pass `--device 0`.

Free GPU options when no local GPU is available:

* **Kaggle Notebooks**: free T4 or P100, ~30 h/week.
* **Google Colab**: free T4, with session limits.

For either one, zip `datasets/courts/` (images are small PNGs; ~4,000 chips is
about 800 MB), upload it, and run `pip install ultralytics` followed by
`yolo detect train data=.../data.yaml model=yolo11n.pt imgsz=320 epochs=100 flipud=0.5 fliplr=0.5 scale=0.2 degrees=0`.
Then copy `runs/detect/train/weights/best.pt` back to `pipeline/weights/best.pt`.
Edit `path:` in `data.yaml` to the notebook's dataset path first.

## Configuration

* `regions.json` holds named bounding boxes `[south, west, north, east]` plus
  groups. It contains:
  * targets: `chattanooga` (34.98, -85.45, 35.26, -85.05), `hamilton_county`
    (whole county), and two ~1 km² test windows: `downtown_chattanooga` and
    `chattanooga_smoke`
  * training metros: atlanta, nashville, knoxville, birmingham, charlotte,
    houston, phoenix, chicago_suburbs
  * groups: `training`, `southeast`, `target`

  Add a region by adding a line. `app_region` sets the `region` label that a
  sub-window's detections get in the app. Any CLI that takes `--regions` also
  accepts group names. `--bbox S W N E` works wherever a one-off area is needed.
* `hoop_pipeline/config.py` holds paths, the Overpass endpoints (kumi, then
  overpass-api.de, then private.coffee), the User-Agent, timeouts, chip size
  (320 px), target GSD (0.6 m), scan overlap (96 px), the known-court radius
  (40 m) and the base model (`yolo11n.pt`).

## Step by step

Runtimes were measured on the dev machine (Ryzen 7 7800X3D, CPU torch, home
broadband) unless marked as an estimate.

### 1. `fetch_osm`: OSM basketball courts

```bash
python -m hoop_pipeline.fetch_osm --regions chattanooga
python -m hoop_pipeline.fetch_osm --regions training --negatives     # labels + negative-sample spots
python -m hoop_pipeline.fetch_osm --bbox 34.98 -85.48 35.46 -84.94 --name my_area
```

The query is `node/way/relation["sport"~"basketball"]` with `out body geom`.
Bboxes larger than 0.25° are split into tiles, and the pipeline sleeps 2 s
between requests. Each request retries up to 5 rounds over all endpoints with
exponential backoff and honors `Retry-After`. An endpoint that just failed
moves to the back of the list for 5 minutes. If a region still fails, nothing
is written for it, the other regions continue, and the command exits non-zero
listing what to re-run. Results are cached in `data/osm/<region>.geojson`
(use `--force` to refetch). Each feature has the court polygon, or a point for
node-only courts, plus `center_lat/lon` (the bbox center, the same as Overpass
`out center`), `area_m2`, and the raw tags.

`--negatives` also saves centers of parks, parking lots, playgrounds and
non-basketball pitches to `<region>.negatives.geojson`. Those pitches are good
hard negatives because tennis courts look like basketball courts.

Runtime is 10 s to 3 min per region depending on Overpass load. On
2026-10-08 kumi returned HTTP 500 to everything and overpass-api.de returned
intermittent 504s, so most requests needed one to three retry rounds.

Counts on 2026-10-08:

| region | features | with polygon |
|---|---|---|
| chattanooga | 28 | 28 |
| knoxville | 35 | 35 |
| birmingham | 52 | 50 |
| nashville | 79 | 73 |
| atlanta | 119 | 118 |
| charlotte | 199 | 199 |
| houston | 229 | 226 |
| chicago_suburbs | 404 | 391 |
| phoenix | 610 | 605 |
| **total** | **1,755** | **1,725** |

### 2. `naip`: item lookup and chips

```bash
python -m hoop_pipeline.naip items --lat 35.0662 --lon -85.2537
python -m hoop_pipeline.naip chip --lat 35.0003 --lon -85.2823 --out data/chips/eastlake.png
python -m hoop_pipeline.naip chip --osm data/osm/chattanooga.geojson --ids way/133339532 way/906959428 --nir
```

A chip is an N x N PNG (RGB, plus an optional `_nir.png`) centered on a
lat/lon. A sidecar `.json` records the CRS, the affine transform, the
date/year, the item id, the native and output GSD, the bounds, and the
valid-pixel fraction.

The pipeline uses the most recent NAIP year covering the point. Among items
from that year it prefers one that contains the whole chip, and it fills
quarter-quad edge gaps from neighbouring items through a `WarpedVRT`. Chips
are resampled to 0.6 m by default (`--gsd`), so 0.3 m states look the same to
the model.

Runtime is ~0.3 s per 320 px chip and ~2 to 4 s per 1600 px block, plus a
one-time ~2 s STAC search per area (cached in `data/naip_index/`).

### 3. `build_dataset`: YOLO dataset

```bash
python -m hoop_pipeline.build_dataset --regions training chattanooga --max-positives 3000 --neg-ratio 1.0 --out datasets/courts
# smoke version used for verification:
python -m hoop_pipeline.build_dataset --regions atlanta nashville knoxville chattanooga --no-fetch \
    --max-positives 64 --neg-ratio 1.0 --out datasets/courts_smoke --overwrite
```

**Positives:** one chip per usable OSM court polygon. A court is usable if it
is outdoor (not `indoor`, `covered`, `building` or stadium/sports-centre) and
its area is 60 to 6,000 m². Regions are sampled round-robin, and each chip is
randomly offset (`--jitter`) so courts aren't always centered. **Every** known
court inside a chip is labelled. Each polygon is reprojected (lon/lat → chip
CRS via pyproj → pixel via the inverse affine) and becomes an axis-aligned
`0 xc yc w h` box. A chip is dropped if it contains a visible court that can't
be boxed (an OSM node with no polygon, or a large court only partly inside).

**Negatives:** half come from the OSM negative spots (when available) and half
from random points 150 to 1,000 m from known courts (streets, roofs, yards,
parking). A negative is rejected if its chip, padded by 30 m, touches any
known court. Negatives get empty label files.

**Split:** a spatial split on ~1 km cells (`--val-frac 0.2`), so overlapping
chips never straddle train and val. `--val-regions chattanooga` instead holds
out a whole region, which gives an honest "new city" score.

The output contains `images/{train,val}`, `labels/{train,val}`, `meta/*.json`
(chip georeferencing and OSM refs), `manifest.csv`, `stats.json`, `data.yaml`
and `previews/` (label overlays; look at these).

Runtime is ~4 to 5 chips/s with 6 threads (128 chips in 25 to 35 s).
Estimate: 6,000 chips takes ~25 min.

> **Rotated courts:** axis-aligned boxes around diagonal courts include a lot
> of background. Ultralytics' **YOLO-OBB** (`yolo11n-obb.pt`) predicts rotated
> boxes. The label would be the polygon's minimum rotated rectangle
> (`shapely.minimum_rotated_rectangle`) written as 4 normalized corners. It
> would fit courts better and allow rotation augmentation. That is a small
> change to `label_chip` (see Next steps).

### 4. `train`: Ultralytics YOLO

```bash
python -m hoop_pipeline.train --data datasets/courts_smoke/data.yaml --epochs 2 --imgsz 320 --batch 8 --workers 0 --name smoke
python -m hoop_pipeline.train --data datasets/courts/data.yaml --epochs 100 --imgsz 320 --batch 32 --device 0   # real run
```

The base model is `yolo11n.pt` (COCO-pretrained, 2.6 M parameters,
downloaded to `weights/`). `--model yolov8n.pt` and `yolo11s.pt` also work.
Augmentation is set for aerial imagery: up/down and left/right flips, scale
jitter of only ±20% (GSD is fixed), and no rotation (rotation would inflate
axis-aligned boxes). The best checkpoint is copied to `weights/best.pt`, with
`weights/best.json` holding its metrics and settings. Ultralytics run outputs
(curves, confusion matrix, val predictions) go to `runs/<name>/`.

Runtime on CPU is ~25 s per epoch for 109 images at 320 px (~0.2 s per image
per epoch). The estimates below scale from that measurement:

| hardware | 4,000 chips x 100 epochs (estimate) |
|---|---|
| CPU | ~20 h |
| RTX 4070 | ~20 to 60 min |
| Colab or Kaggle T4 | ~1.5 to 2 h |

### 5. `scan`: find candidates

```bash
python -m hoop_pipeline.scan --region chattanooga_smoke --save-crops 20
python -m hoop_pipeline.scan --region chattanooga --workers 8           # whole target bbox
python -m hoop_pipeline.scan --bbox 35.042 -85.312 35.051 -85.301 --conf 0.3
```

The scan works like this:

1. It reads the area in 1,600 px (960 m) blocks with 96 px overlap, using one
   ranged read per block, prefetched by `--workers` threads.
2. It cuts each block into 320 px tiles with 96 px overlap. That is more than
   a full court, so every court appears whole in at least one tile.
3. It runs YOLO on batches of tiles, then maps each box from pixels to block
   CRS to lon/lat.
4. It merges duplicates across tiles and blocks with greedy NMS in metres
   (IoU ≥ 0.3, or ≥ 60% of the smaller box inside the larger). Boxes cut by a
   tile edge lose ties.
5. It drops boxes with a side under 6 m or over 120 m, and any detection
   within **40 m** of a known OSM court. Known courts come from cached
   `data/osm/*.geojson` files that cover the area, or are fetched.

Each candidate gets a stable id `det-<sha1 of lat/lon rounded to 4 dp>`. The
results go to `out/candidates.geojson`, along with confidence, box size,
NAIP item and date, and run metadata. With `--save-crops N`, the top N also
get review PNGs in `out/candidates_crops/`.

`--conf` (default 0.25) only sets what gets recorded. The export applies its
own threshold.

Runtime for 1.2 km² (4 blocks, 196 tiles) is 15 to 40 s on CPU, including
model load. Estimate for the full Chattanooga bbox (~1,130 km², ~1,400
blocks, ~68k tiles): 1 to 2 h on CPU, ~30 min on GPU, where NAIP reads become
the bottleneck (raise `--workers`).

### 6. `export_app`: write the app's GeoJSON

```bash
python -m hoop_pipeline.export_app                       # OSM (chattanooga) + NYC + candidates with confidence >= 0.5
python -m hoop_pipeline.export_app --min-confidence 0.7
python -m hoop_pipeline.export_app --no-candidates       # OSM + NYC only
python -m hoop_pipeline.export_app --dry-run             # validate and report only
```

The export builds the file in the shared schema
(`id, name, source, status, confidence, hoops, surface, lit, address, region, osm_id, updated`):

* **OSM:** `osm-<type>-<id>`, verified, bbox-center point. It copies `hoops`,
  `surface` and `lit` (yes/no) from tags. `address` stays null, matching the
  current file.
* **NYC Parks:** `nyc-<Prop_ID>`. Repeated Prop_IDs get `-2`, `-3`… in file
  order, and the 20 rows without coordinates are skipped (553 features).
* **Detected:** `source: "detected", status: "candidate"`, confidence rounded
  to 3 dp, and only at or above `--min-confidence` (default 0.5). Candidates
  within 40 m of an OSM court are re-checked and dropped.

Any `source: "user"` features already in the file are carried over. Unchanged
features keep their previous `updated` date. Validation (exact keys, types,
enums, coordinates, unique ids) runs before an atomic write. If it fails,
nothing is written.

## Nationwide workflow (Lambda Cloud GPU)

The small-scale steps above use Overpass and per-chip STAC searches, which do
not scale past a few cities (and Overpass was failing on 2026-10-08). The
nationwide path replaces both, and all heavy work runs on one rented GPU
instance. The local machine only launches it, syncs code, polls, and downloads
results.

### New modules

| module | what it does |
|---|---|
| `osm_extract` | `osmium tags-filter` + `osmium export` on the Geofabrik US extract (closed ways and multipolygon relations become polygons), parallel classification of the GeoJSON lines, US state by point-in-polygon on the Census 1:500k boundaries, contiguous US only (48 states + DC). Writes `data/osm_us/courts_us.parquet` (every feature whose `sport` contains `basketball`, with polygon or point, bbox center, area, state, `outdoor` / `court_like` / `label_ok` flags, tags JSON), `indoor_us.parquet`, `context_us.parquet` (hard negatives and scan-mask features, one row per feature and kind), `courts_<ST>.geojson` (fetch_osm schema, for `export_app`) and `summary.json`. |
| `naip_index` | Loads every NAIP footprint from Planetary Computer's `naip` **GeoParquet items asset** (yearly partitions, ~25 MB each, fetched once). `NaipCatalog.select` picks one item per location: the location's own state flight first (unless it is more than 2 years older than the newest covering flight), then the newest `naip:year`, then an item that contains the whole chip, then the latest date. This keeps Georgia's October leaf-on flight off Tennessee locations. `GroupedReader` groups windows by item so each COG opens once per batch of windows, runs a thread pool, retries with exponential backoff, and shares one SAS token per container. |
| `build_dataset_us` | Nationwide YOLO dataset (details below). Labels are computed from the catalog's `proj:transform` before any pixel is read. |
| `evaluate` | Ultralytics mAP plus our own precision / recall / F1 at fixed thresholds (IoU 0.5, and a "loose" IoU 0.3 because OSM boxes around rotated or multi-court polygons are loose), how often background-only chips fire, a recommended threshold, and montages of false positives, misses and hits. |
| `scan_state` | Masked statewide scan. `plan`: the scan mask (parks, schools, colleges, places of worship, community centres, recreation grounds, sports centres, apartment landuse; polygons +40 m, points +100 m, clipped to the state), one NAIP item per location (own-state flight, newest first), 320 px tiles with 96 px overlap in each item's pixel grid, grouped into blocks. `fetch`: one ranged read per block, cached as JPEG q95 like the training chips (network only, so it can run while the GPU trains). `detect`: batched FP16 inference, merge across tiles/blocks/items (greedy NMS in metres), 6–120 m size filter, 40 m known-court filter, end-to-end recall on known courts inside the scanned blocks, review crops and contact sheets; `--retile 640` re-cuts the cached blocks into bigger tiles. `annotate`: `mask_kinds` and an `on_site` flag per candidate (inside the park/school/... polygon, or near a point feature). |
| `osm_tags`, `states`, `geoparquet` | shared tag rules, Census state boundaries, GeoParquet I/O with pyarrow (no geopandas needed) |

`export_app` gained `--sub-region` (a bbox inside a bigger export keeps its own
region label) so the statewide file keeps the Chattanooga ids and labels, plus
`--offsite-min-box-m` / `--require-on-site` privacy filters for candidates.
`train` gained `--set KEY=VALUE` for extra Ultralytics arguments.

### `build_dataset_us` rules

* **Positives:** one chip per labelable court (outdoor, court-like polygon of
  60–6,000 m²), randomly offset but with the court whole inside the chip.
* **Negatives** (`--neg-ratio 1.0` per state): centred on tennis courts (30%),
  other small non-basketball pitches (10%), parking lots (20%), pools (10%),
  and random points inside parks (15%) and schools (15%) at least 60 m from any
  known basketball feature. Hard-negative centres are at least 20 m from one.
* **Labels:** every labelable court whose box intersects the chip, in every
  chip (so a tennis-centred chip next to a basketball court has a box). Boxes
  are clipped at the chip edge; slivers (under 35% visible, or a side under
  6 px) are dropped. A chip that shows a court we cannot box (node-only court,
  polygon over 6,000 m², or a park/school under 20 ha tagged basketball) is
  discarded rather than teaching the model that a court is background.
* **Split:** Tennessee entirely as `val`. Non-TN chips within 250 m of
  Tennessee are dropped. 2% of ~5 km cells elsewhere form `val_us`; the rest
  is `train`.
* Chips are 320 × 320 px at 0.6 m (0.3 m states are read from the 0.6 m
  overview), JPEG quality 95.

### Cloud scripts (`pipeline/cloud/`)

All local scripts run from Git Bash in `pipeline/`. The API key is read from
`~/.config/hoop-hunter/lambda_api_key` and passed to curl on stdin, so it is
never printed and never written to the repo. The one copy that leaves this PC is
the one `install_watchdog.sh` pipes into a root-only file on each instance, so
the instance can terminate itself (see [Safety shutoff](#safety-shutoff-watchdogs)).

| script | runs | purpose |
|---|---|---|
| `launch.sh NAME [types] --max-hours H` | local | Re-reads the API key file and checks it works (`GET /instance-types` → 200; the key may be rotated at any time). Accepts only single-GPU types. Launches **one** instance as node NAME with the registered `hoophunter-claude` SSH key. The deadline (`--max-hours`, default 6) goes into `cloud/last_run.json` and into the instance name (`hh-NAME-xYYMMDDHHMM`, UTC). The record is written the moment the id comes back. Waits for `active`, then installs the on-instance watchdog. If the install fails, or the instance goes unhealthy or never becomes active, it terminates the instance. Refuses while NAME is live. |
| `install_watchdog.sh NAME [--key-only]` | local | Pushes `watchdog_instance.py`, its config (instance id, IP, deadline, idle limit) and the API key (piped into `/etc/hoophunter/lambda_api_key`, root, 0600) and runs `watchdog_install.sh` on the node. `--key-only` refreshes the instance's key copy after a rotation. |
| `watchdog_install.sh`, `watchdog_instance.py` | instance (root) | systemd timer every 2 min plus a cron fallback every 5 min. They terminate the instance at its deadline, or after 30 min with an idle GPU and no pipeline process. See below. |
| `watchdog.py` | local / scheduler | Backstop: terminates any `hh*` / `hoophunter*` instance past its deadline (name suffix, `last_run.json`, or a 24 h hard cap). |
| `sync_code.sh` | local | tars the package, scripts and the Census zip to `~/hh/pipeline` |
| `start_stage.sh STAGE [args]` | local | starts a `run_remote.sh` stage fully detached (`setsid nohup`); `start_stage.sh status` shows every stage's markers and log tail |
| `setup_remote.sh` | instance | (re)installs the watchdog timer first and stops if its config or key is missing; apt `osmium-tool`; venv with `--system-site-packages` (reuses Lambda Stack's CUDA torch); `requirements-cloud.txt`; GPU and numpy/torch interop check |
| `run_remote.sh STAGE` | instance | `setup`, `pbf`, `osm`, `naip`, `dataset`, `train`, `scan_plan`, `scan_fetch`, `eval`, `scan_detect`, `post_train` (eval + scan_detect + a 640 px re-tiled scan + `bench_infer.py`), `package`; logs and `.start/.done/.failed` markers in `~/hh/logs` |
| `fetch_results.sh` | local | packages results on the instance and unpacks them into `weights/`, `out/`, `data/` (chips and imagery cache stay remote) |
| `terminate.sh NAME\|ID\|--all` | local | Terminates, waits until the instance is gone, records the time and cost, and lists everything still on the account. On 401/403 it retries for 10 min, re-reading the key file each time, then prints `TERMINATE MANUALLY: instance <id>`. `--all` also covers instances not in `last_run.json`. |
| `scan_fleet.sh launch\|push\|start\|status\|collect\|terminate` | local | Multi-node `scan_engine`: launch (with `HH_MAX_HOURS`, default 6, and a watchdog per node) and set up nodes in parallel, push plan/manifests/weights/engine, start `run` on node k with `manifests/node<k>_p*.txt`, collect results into one node |
| `post_train_v2.sh engine\|a\|b\|ab`, `post_scan_tn.sh` | instance | TensorRT engine + GPU ceiling; v2 / v1 evaluations, gold pre-annotation, disagreement crops; merge + privacy + review sample after a scan |
| `lastrun.py` | local | Bookkeeping for `last_run.json`: one record per node, totals against the run budget, an atomic lock for parallel launches, previous runs archived. Also picks the instance type. |
| `diag_reads.py`, `bench_reads.py`, `bench_infer.py` | instance | signing / open / read concurrency check; dense NAIP read benchmark; inference benchmark |
| `estimate_paved_fraction.py` | local | samples USDA CDL to estimate how much of CONUS a paved-area mask covers |
| `review_samples.py` | instance | random review crops per confidence band (optionally on-site / off-site only) |
| `bench_fastdet.py`, `estimate_national.py` | instance / local | FastDetector GPU ceiling (PyTorch / TensorRT); national time and cost from the CONUS plan and benchmark workers |
| `review_crops.py`, `sample_review.py`, `score_review.py`, `disagree.py`, `gold_review.py`, `filter_extra_negatives.py` | instance / local | crops for visual review, the stratified v2 review sample, scoring model-assistant labels, v1-vs-v2 disagreements, a blind v1-vs-v2 review of every box on the gold tiles, TN hard negatives away from held-out chips |
| `compare_evals.py`, `bench_table.py` | local | README tables from `evaluate` outputs and from a scan's `merge_stats.json` |
| `national.sh up\|status\|pull\|logs`, `national_node.sh`, `national_scan.sh` | local / instance | national scan fleet: push plan + manifest + weights, build the TensorRT engine, run up to 4 `scan_engine run` passes per node |
| `pull_results.py` | local (scheduled task) | every 10 min copies new per-item results from live `hh-s*` nodes and releases a node's watchdog hold once its manifest is done and copied |
| `national_post.sh rescan\|prep\|merge\|phome\|rescore\|privacy\|review\|package` | instance | national post-processing, one part per `start_stage.sh job` (see [National run](#national-run-2026-10-15)) |

A full session:

```bash
cd pipeline
python cloud/watchdog.py --loop 10 &      # local backstop (or the scheduled task, see Safety shutoff)
bash cloud/launch.sh main --max-hours 6    # gpu_1x_a100_sxm4, else gpu_1x_a10, else gpu_1x_h100_sxm5; installs the watchdog
bash cloud/sync_code.sh
bash cloud/start_stage.sh pbf              # Geofabrik US extract, md5-checked (~10 min at 20 MB/s)
bash cloud/start_stage.sh setup            # parallel with pbf
bash cloud/start_stage.sh naip             # after setup
bash cloud/start_stage.sh osm              # after pbf
bash cloud/start_stage.sh dataset --workers 96
bash cloud/start_stage.sh scan_plan        # CPU only; overlaps the dataset build
bash cloud/start_stage.sh train --workers 24
bash cloud/start_stage.sh scan_fetch       # network only; overlaps training
bash cloud/start_stage.sh eval
bash cloud/start_stage.sh scan_detect --crops 300
bash cloud/start_stage.sh status           # any time
bash cloud/fetch_results.sh
bash cloud/terminate.sh                    # always, also after a failure; check the account listing it prints
python -m hoop_pipeline.export_app --osm data/osm_us/courts_TN.geojson --region-label tennessee \
    --sub-region chattanooga --candidates out/candidates_tn.geojson --min-confidence <recommended>
```

Gotchas found on the first run:

* Lambda Stack's system scipy and matplotlib are built against numpy 1.x and
  are visible through `--system-site-packages`. numpy 2 in the venv breaks
  them (`numpy.dtype size changed`), so `requirements-cloud.txt` pins
  `numpy<2` and `opencv-python<4.12`.
* `ssh host "cmd && nohup job > log &"` keeps the SSH session open, because the
  backgrounded subshell still holds the session's stdout. `start_stage.sh`
  wraps the job in `(setsid nohup ... &)`.
* bash reads a plain script incrementally. `scp` rewrites the remote file in
  place, so copying a new `run_remote.sh` while a stage was running left that
  stage's shell resuming at an old byte offset in new content once its job
  finished, so it would never write `train.done`. A watchdog wrote the marker
  this time. `run_remote.sh` is now one function called on its last line, so
  bash parses the whole file up front and in-place edits are safe.
* `pgrep -f pattern` inside `ssh host "..."` also matches the SSH command's own
  shell, whose command line contains the pattern. Use `pgrep -f "[p]attern"`.
* Planetary Computer's anonymous SAS-token endpoint throttles bursts (a single
  token request took 16 s while others were in flight). When every reader
  thread signed its own URLs, a 1,000-chip pilot ran at **2.4 chips/s**. With
  one lock-protected token per container, refreshed 5 minutes before expiry,
  it ran at **43 chips/s**, and the full build reached **469 chips/s**. The
  NAIP container also answers anonymous range requests. `HH_ANONYMOUS_READS=1`
  skips tokens for it entirely. Tokens stay the default, because Planetary
  Computer documents SAS as the access path.

## Safety shutoff (watchdogs)

**Why.** In run 2 the coordinating agent hit an API usage limit on 2026-10-09 and stopped mid-task. Its
four A100 nodes (`main`, `scan2`–`scan4`) then ran unattended for about 147 h each, roughly **$1,174**,
until they were terminated by hand on 2026-10-15. Nothing on the instances could stop them. Now two
independent watchdogs can, and neither needs the agent or this PC to stay alive.

**1. On the instance** (`cloud/watchdog_instance.py`, standard library only, runs as root):

* **Install.** `launch.sh` installs it right after the instance turns `active`, before code sync or setup,
  through `install_watchdog.sh NAME`. If the install fails, `launch.sh` terminates the instance.
  `setup_remote.sh` reinstalls it and refuses to continue without it.
* **Files.**
  * `/etc/hoophunter/watchdog.json`: instance id and IP from the launch record, the deadline, idle limit
    and API base.
  * `/etc/hoophunter/lambda_api_key`: root, 0600. Copying the key to the instance is the accepted
    trade-off for a node that can kill itself.
  * `/var/lib/hoophunter/watchdog.json`: state.
  * `/var/log/hoophunter-watchdog.log`: every decision and API call, never the key.
* **Schedule.** A systemd timer runs `check` every 2 min, with a cron fallback every 5 min. A file lock
  keeps the two from overlapping.
* **Rules**, in order:
  1. Past the deadline (`launch.sh --max-hours`, default 6): terminate.
  2. Idle for 30 min: terminate. Idle means GPU utilisation < 5%, no process on the GPU, no pipeline
     process (`run_remote.sh`, `hoop_pipeline`, the venv's python, `setup_remote.sh`) and no hold.
     * A hold, `python3 /opt/hoophunter/watchdog_instance.py hold 45`, counts as activity for 45 min. It
       never extends past the deadline.
     * The deadline is never extended on the instance. A longer run needs a bigger `--max-hours` at launch.
* **Terminate.** `POST /instance-operations/terminate` for its own id.
  * It re-reads the key before every attempt and retries 5 times with back-off on network errors,
    401/403 and 5xx.
  * On a 404 it looks the id up again with `GET /instances`, matching the node's IP.
  * If every attempt of a run fails, the next timer run (2 min later) tries again.
* **Commands.**
  * `sudo python3 /opt/hoophunter/watchdog_instance.py status` prints the current decision.
  * `check --dry-run` logs the decision without acting.
  * `journalctl -u hh-watchdog` and the log file show the history.

**2. From this PC or a scheduler** (`cloud/watchdog.py`): it lists the account's instances and terminates
every `hh*` / `hoophunter*` instance past its deadline. Other instances are never touched. The deadline
is the earliest of:

* the name suffix `-xYYMMDDHHMM` (UTC) that `launch.sh` adds
* the `deadline` (or `launched_at` + `max_hours`) in `last_run.json`
* launch, or first sighting, + `--hard-cap-hours` (24)
* first sighting + `--default-max-hours` (6), for an instance with no deadline anywhere

It re-reads the key before each call and retries a failed terminate for 5 min. Then it prints
`TERMINATE MANUALLY: instance <id>` and exits 2. Its log goes to `cloud/logs/watchdog.log`.

```bash
python cloud/watchdog.py --dry-run          # what it would do now
python cloud/watchdog.py --loop 10          # every 10 min while a run is going
```

To run it every 10 min with Windows Task Scheduler:

```bat
schtasks /Create /TN "HoopHunter Lambda watchdog" /SC MINUTE /MO 10 /F /TR "\"C:\Users\Eli Young\Hoop-Hunter\pipeline\.venv\Scripts\python.exe\" \"C:\Users\Eli Young\Hoop-Hunter\pipeline\cloud\watchdog.py\""
```

`schtasks /Delete /TN "HoopHunter Lambda watchdog" /F` removes it.

For GitHub Actions, copy `cloud/watchdog_github_action.yml.example` to `.github/workflows/` and add the
key as the secret `LAMBDA_API_KEY`. Actions has no `last_run.json` and no memory of first sightings,
so there only the name deadline counts.

**Tests** (offline, against a fake Lambda API on localhost; no key, no instance):

```bash
python -m unittest discover -s tests -v     # 14 tests
```

They cover these cases:

* an expired name, record or hard-cap deadline terminates the instance
* a live, foreign or already-terminated instance is left alone
* `--dry-run` never posts
* a key rotated between retries is picked up
* a persistent 500 ends in `TERMINATE MANUALLY` with exit 2
* on the instance, max lifetime, 30 min idle (a GPU or CPU job resets it) and a missing `nvidia-smi`
* holds are capped at the deadline
* a stale instance id is resolved by IP
* the retry log never contains the key

**Limits.**

* A process that keeps a GPU context open, such as a forgotten Python shell, counts as activity. Then
  only the deadline stops the node.
* The instance's key copy goes stale when the key is rotated. Run `install_watchdog.sh NAME --key-only`,
  or rely on `watchdog.py`, which reads the key file fresh.
* The watchdogs terminate instead of shutting down. Lambda has no stopped state, and a powered-off
  instance is most likely still billed (not verified).

## Verified on 2026-10-08 (small scale)

* **NAIP chips:** four Chattanooga OSM courts were chipped: East Lake
  (way/133339532), Soddy Daisy Park (way/906959428), way/178202248, and the
  Tacoa Park half court (way/1316785782). All four used NAIP from
  **2023-04-11**, 0.6 m native, 320 px = 192 m. The court is visible and
  centered in each chip. Painted keys and circles are visible on East Lake.
  The half court shows only as a ~17 px light square.
* **Dataset:** the smoke dataset above has 128 chips with nothing skipped.
  Train is 109 chips (53 positive, 56 negative, 67 boxes) and val is 19 chips
  (11 positive, 8 negative, 17 boxes), all 2023 imagery. The `previews/` boxes
  sit on the courts. Round-tripping label box centers back to lon/lat gives a
  median error of 0.01 m and a maximum of 0.66 m against the OSM centers.
* **Train:** 2 epochs, imgsz 320, CPU, 56 s. Val scores were P 0.0025,
  R 0.82, mAP50 0.0024: the pipeline runs, but the model has learned nothing
  yet.
* **Scan:** `chattanooga_smoke` (1.22 km²: 4 blocks, 196 tiles).
  * At the default `--conf 0.25` there were 0 detections.
  * At `--conf 0.01` there were 1,279 raw boxes. 642 passed the size filter,
    195 remained after merging, and 188 remained after dropping 7 within
    40 m of the window's 3 OSM courts.
  * The best confidence was 0.013, and the crops show yards and roofs.
* **Export:** 581 features (28 OSM + 553 NYC) and 0 candidates (all below
  0.5). The file passes validation, has unique ids, and its JSON content is
  identical to the hand-made file it replaced.

## First nationwide run (2026-10-08)

One `gpu_1x_a100_sxm4` (A100 SXM4 40 GB, 30 vCPU, 216 GB RAM, Ubuntu 22.04,
Lambda Stack torch 2.7.0) in **us-east-1** (Ashburn, VA) at $1.99/h, from
13:24 to 18:54 UTC: **5.50 h, ≈ $10.94**, then terminated (`GET /instances`
afterwards returned none). Times, cost and the instance id are in
`cloud/last_run.json`; logs are in `out/cloud/logs/`.

| stage | wall time | notes |
|---|---|---|
| launch → active | 4.7 min | |
| `pbf` | 9.5 min | 12.19 GB from Geofabrik at ~20 MB/s, md5 OK |
| `setup` | ~4 min | including a re-run after the numpy 2 breakage |
| `osm` | 3.4 min | tags-filter + export + 4.1 M GeoJSON lines parsed on 28 processes |
| `naip` | 0.6 min | 8 yearly GeoParquet partitions, 867,997 items (2016–2023) |
| `dataset` | 7.1 min | 186,968 chip reads at 469 chips/s, no retries or errors |
| `scan_plan` | 0.8 min | |
| `train` (yolo11s) | 1.97 h | 61 epochs, early-stopped (best epoch 46), batch 256, AMP, RAM cache (52 GB), ~116 s/epoch |
| `scan_fetch` | 2.4 min | during training |
| `eval` | 0.6 min | |
| `scan_detect` | 11.4 min | 195,843 tiles at 289 tiles/s (Ultralytics overhead, see below) |
| 640 px re-tiled scan, inference and TensorRT benchmarks | ~40 min | incl. a 7 min TensorRT engine build |
| `train` (yolo11m) | 1.92 h | 30 epochs, batch 128, `close_mosaic=5`, ~225 s/epoch |
| yolo11m eval + TN scan | 13 min | |

### OSM (Geofabrik extract of 2026-10-06), contiguous US

* **101,098** features whose `sport` contains `basketball` (97,677 polygons,
  3,420 points, 1 line; 815 outside CONUS dropped). 99,158 are outdoor, and
  **93,887** are labelable (outdoor court-like polygons of 60–6,000 m²).
* Top states: CA 18,218, FL 6,102, NY 5,600, TX 5,210, AZ 4,452, PA 4,392,
  OH 4,383, IL 3,560, WA 3,329, MI 3,054.
* **Tennessee: 828** (770 polygons, 58 points, 719 labelable). All 28
  Chattanooga courts from the Overpass era match by id and centre.
* **Indoor layer: 1,681.** 907 sports centres, 238 fitness centres, 206
  sports halls and 6 community centres (1,357 of the requested kinds), plus
  324 basketball pitches tagged indoor.
* Context layer: 3.62 M rows (parking 1.44 M, pools 702 k, other pitches
  343 k, places of worship 272 k, tennis 197 k, parks 187 k, playgrounds
  173 k, schools 143 k, recreation grounds 58 k, apartment landuse 55 k,
  sports centres 26 k, community centres 14 k, colleges 8.5 k).

### NAIP year used (most chips per state)

* **2023:** AL AR AZ CO CT DC DE FL GA IA ID IL KS LA MA MD ME MN MS MT ND NH
  NJ OH OK RI SC TN VA VT WA
* **2022:** CA IN KY MI MO NC NE NM NV NY OR PA SD TX WI WV WY
* **2021:** UT

0.3 m states are read from their 0.6 m overview. Tennessee's 2023 flight
(0.6 m) ran from 2023-04-11 to 2023-11-02, so some quads are leaf-on.

### Dataset (`datasets/us`, 186,647 chips, manifest in `data/dataset_us/`)

| split | chips | court-centred | negative-centred | with boxes | background only | boxes |
|---|---|---|---|---|---|---|
| train | 181,165 | 90,478 | 90,687 | 98,247 | 82,918 | 189,287 |
| val (Tennessee) | 1,430 | 711 | 719 | 740 | 690 | 907 |
| val_us (2% of ~5 km cells) | 4,052 | 2,063 | 1,989 | 2,247 | 1,805 | 4,110 |

Train negatives are centred on tennis (27,183), parking (18,152), schools
(13,621), parks (13,613), pools (9,102) and other pitches (9,016). Skipped:
789 chips that showed a court we couldn't box, 321 with partial imagery, 7
without NAIP, and 5 within 250 m of Tennessee.

### Model and holdout metrics (yolo11s, `weights/best.pt`)

Ultralytics on the Tennessee holdout gives **mAP50 0.792, mAP50-95 0.538**
(P 0.807, R 0.717). On `val_us` it gives mAP50 0.787 and mAP50-95 0.520, so
Tennessee transfers like any held-out area. Our own matching on the same
chips (`out/eval_tn/metrics.json`):

| conf | IoU 0.5 P / R / F1 | IoU 0.3 P / R / F1 | background chips firing |
|---|---|---|---|
| 0.25 | 0.770 / 0.754 / 0.762 | 0.813 / 0.796 / 0.804 | 5.1% |
| 0.35 | 0.811 / 0.718 / 0.761 | 0.869 / 0.770 / 0.816 | 3.9% |
| 0.50 | 0.853 / 0.647 / 0.736 | 0.910 / 0.690 / 0.785 | 2.5% |
| 0.60 | 0.900 / 0.538 / 0.674 | 0.939 / 0.561 / 0.703 | 1.3% |
| 0.65 | 0.919 / 0.450 / 0.604 | 0.953 / 0.466 / 0.626 | 1.0% |
| 0.70 | 0.946 / 0.348 / 0.509 | 0.970 / 0.357 / 0.522 | 0.9% |

**yolo11m** (`weights/best_y11m.pt`, 30 epochs) is better on the holdout:
mAP50 0.807 / mAP50-95 0.548 (P 0.826, R 0.717 at 0.35, with 3.0% of
background chips firing). In the Tennessee scan it finds more known courts
with fewer detections at low thresholds (recall 0.853 with 3,109 detections at
0.25, versus 0.843 with 3,618). At the app's high-confidence point the two
models are equivalent (~1,115 vs ~1,135 detections at recall 0.60), and
yolo11m costs ~2.5× the GPU. So `weights/best.pt` stays yolo11s, and the
yolo11m candidates (`out/candidates_tn_y11m.geojson`) are the better choice for
a lower-threshold review queue.

**Recommended threshold: 0.65 for scans and the app.** On balanced holdout
chips, 0.35 already reaches 0.87 precision. In a statewide scan, though,
nearly every tile is background, and 30 random review crops per confidence
band put precision at about:

| band | ~precision |
|---|---|
| ≥ 0.75 | 85–90% |
| 0.65–0.75 | 65–70% |
| 0.55–0.65 | 45% |
| 0.45–0.55 | 20% |
| 0.35–0.45 | 20–25% |

Errors seen in the montages (`out/eval_tn/false_positives_*.jpg`,
`misses_*.jpg`):

* Many top "false positives" are **unmapped courts** at schools, parks and
  apartments, or **label granularity**: OSM has one polygon over two adjacent
  courts and the model boxes each one (or the reverse).
* Real false positives: parking lots, bare concrete or sand slabs, sand
  volleyball pits, construction pads, tree shadows, light roofs.
* Misses: OSM label noise (a pool, a block of pickleball courts or a parking
  lot tagged basketball, or a shifted polygon), courts under leaf-on canopy,
  tiny pads or half courts of a few pixels, and courts cut at the chip edge.

### Tennessee scan

* Mask: **1,501.5 km²** (1.38% of the state): parks 890 km² (largest park
  99.9 km²), places of worship 276, schools 201, apartments 73, colleges 54,
  recreation grounds 23, sports centres 21, community centres 3.5 (each kind
  on its own; they overlap).
* **195,843 tiles** in 23,465 blocks from 2,625 NAIP items (all 2023 except
  24 tiles from 2021).
* Times: plan 46 s; fetch 146 s (74 GB, 506 MB/s, no failures); detect
  11.4 min.
* Detections: 20,817 raw → 13,597 merged → **12,898 candidates** not within
  40 m of any of the 1,084 known OSM basketball features nearby. By
  confidence: 7,101 ≥ 0.10, 3,031 ≥ 0.25, 1,742 ≥ 0.40, 1,263 ≥ 0.50, 845 ≥
  0.60, **642 ≥ 0.65**, 409 ≥ 0.70.
* **End-to-end recall on held-out courts:** 580 of Tennessee's 719 labelable
  OSM courts lie inside the scanned blocks. The POI mask misses the other
  19% by design. Of those 580, the scan finds 87.8% at conf 0.10, 84.3% at
  0.25, 79.5% at 0.40, 75.0% at 0.50, 63.8% at 0.60 and 43.6% at 0.70.
* Same blocks re-tiled at 640 px: recall 0.890 / 0.850 / 0.759 / 0.631 at
  0.10 / 0.25 / 0.50 / 0.60, with 7–14% more detections. The 320 px model
  holds up on larger tiles at the same GSD.
* **Privacy.** Candidates include backyard courts on residential lots next to
  parks and churches. `scan_state annotate` marks each candidate
  `on_site` (inside or within 15 m of a park/school/... polygon, or within
  75 m of a point feature). A strict on-site rule also dropped many real
  school courts, because 2,323 of Tennessee's 3,412 schools are mapped only
  as points. The export therefore drops only **off-site candidates under
  22 m** (`--offsite-min-box-m 22`), the group holding most of the yards.
  That removed 48 of 642. Every candidate still needs human review before
  it is trusted.
* Review material: `out/candidates_tn.geojson` (all 12,898, with
  `confidence`, `box_m`, `mask_kinds`, `on_site`, NAIP item and date);
  `out/candidates_tn_crops/` (256 px crops of the top 300) and
  `candidates_tn_crops_sheet_*.jpg`; `out/candidates_tn_640.geojson`.

### App file

`app/assets/data/courts.geojson` was rebuilt with:

```bash
python -m hoop_pipeline.export_app --osm data/osm_us/courts_TN.geojson --region-label tennessee \
    --sub-region chattanooga --candidates out/candidates_tn.geojson --min-confidence 0.65 --offsite-min-box-m 22
```

It went from 581 to **1,975 features (0.89 MB)**:

| source / region | features |
|---|---|
| osm / tennessee | 800 |
| osm / chattanooga | 28 (unchanged ids, coordinates and dates) |
| nyc_parks / nyc | 553 (unchanged) |
| detected / tennessee | 574 |
| detected / chattanooga | 20 |

Candidate confidence runs from 0.65 to 0.81 (median 0.725). The schema is
validated and ids are unique.

## Scaling to every square mile of CONUS (measured 2026-10-08)

### Where NAIP lives, and throttling

* The NAIP COGs are in storage account `naipeuwest`, which resolves to
  `blob.ams08prdstr12a.store.core.windows.net`: Azure **West Europe**
  (Amsterdam). The GeoParquet index (`pcstacitems`) and CDL
  (`landcoverdata`) are in West Europe too. The old East US account
  `naipblobs` now answers 403.
* From Lambda us-east-1 (Ashburn, VA): TCP connect 90–110 ms; the first byte
  of a 1 KB range read arrives after 0.40–0.47 s on a fresh TLS connection.
* **Blob reads were never throttled.** About 300 GB were read in this run
  with no HTTP 429/503 and no retries. Two limits did appear:
  * Throughput stopped scaling past ~128–192 concurrent requests per host:
    256 threads gave 322 MB/s versus 550 MB/s at 128.
  * `GDAL_NUM_THREADS=4` on top of 128 threads (~512 connections) produced
    connection failures (`response_code=0`).
* **Planetary Computer's anonymous SAS-token endpoint is rate-limited.** The
  SDK README says a subscription key gives "less restricted rate limiting",
  and bursts of token requests stalled reads here (2.4 chips/s). The NAIP
  container itself accepts anonymous range reads (`HH_ANONYMOUS_READS=1`).

### Read throughput (one A100 node)

| read pattern | threads | MB/s | rate |
|---|---|---|---|
| training chips, 320 px windows | 96 | ~680 | 469 chips/s (~1.45 MB fetched per chip) |
| TN masked-scan blocks | 48 | 506 | 1,340 tiles/s; 49 MB per km² of mask |
| dense whole items, 0.6 m state (TN), 2,048 px windows | 128 | 550 | 65 km²/s ≈ 3,600 tiles/s; **8.4 MB/km²** |
| dense whole items, 0.3 m state (OH) read at 0.6 m | 192 | 412 | 38 km²/s; **10.8 MB/km²** |

Dense reads used 2–6 CPU cores for DEFLATE decoding. Latest-year NAIP is 67%
0.6 m and 33% 0.3 m by area, so a dense read averages **9.2 MB/km²**.

### Inference throughput (A100 40 GB, yolo11s, imgsz 320)

| mode | batch | 320 px tiles/s |
|---|---|---|
| raw forward, FP32 | 32–1024 | 3,500–3,880 |
| raw forward, FP16 | 64 / 256 / 512 / 1024 | 5,239 / 5,646 / 5,723 / 5,767 |
| raw FP16, 640 px tiles | 256 | 1,410 tiles = 5,641 320-px equivalents |
| raw FP16, 1,024 px tiles | 64 | 600 tiles = 6,139 equivalents |
| Ultralytics `predict()` on numpy tiles, PyTorch FP16 | 128–512 | 1,080–1,159 |
| Ultralytics `predict()`, TensorRT 10.16 FP16 engine (416 s to build) | 256 | 2,303 |
| TN scan end to end (JPEG decode + `predict()` + Python post-processing) | 256 | 289 |

The GPU is not the bottleneck. Ultralytics' per-image CPU pre/post-processing
is: a production loop should upload uint8 batches, normalise on the GPU, and
run batched GPU NMS. Batch 256–512 saturates the A100. The TensorRT 11 wheel
needs a newer driver than Lambda's 570 / CUDA 12.8, so use `tensorrt-cu12<11`.
H100 was not measured; the estimates below assume 2.2× an A100.

### Paved-area mask

USDA CDL 2021 on Planetary Computer carries NLCD's developed classes (121–124:
NLCD pixels with mapped impervious surface, roads included), 30 m, EPSG:5070.
`cloud/estimate_paved_fraction.py` sampled 996 random 6 × 6 km windows
(area-weighted over 7.80 M km² of CONUS):

| mask | share of CONUS |
|---|---|
| developed pixels | 6.4% (runs: 7.4 ± 0.7, 5.7 ± 0.5) |
| developed + 30 m | 13% (14.5 ± 0.9, 12.4 ± 0.6) |
| developed + 60 m | 18–21% |
| 120 m cells touching developed + 30 m (≈ 320 px tiles to run) | **20.9 ± 0.8%** |
| 300 m cells touching it (≈ 512 px COG blocks to read) | **35.0 ± 1.0%** |
| 330 m cells (≈ 640 px tiles) | 37.1 ± 1.0% |

Thin buffered roads make the mask cheap in pixels but expensive in blocks and
tiles.

### Plans

Assumptions: 8.1 M km²; 9.2 MB/km²; 0.55 GB/s per host (Lambda us-east to
West Europe); 5,000 tiles/s per A100 with GPU-side preprocessing (raw FP16 is
5,723); 55.4 tiles/km² at 320 px with 96 px overlap; Lambda prices $1.99/h
(1× A100), $15.92/h (8× A100 40 GB), $31.92/h (8× H100). One host's ingress
was measured; an 8-GPU host's was not, so those rows show 0.55 / 1.1 / 4.4 GB/s.

**(a) Brute force, every NAIP pixel:** 2.2e13 px, 448 M tiles, **~75 TB**
read (~87 TB if overlapping items are read whole).

| setup | wall time | cost |
|---|---|---|
| 1× A100 | 38 h, read-bound (GPU 25 h with a custom loop; 107 h with Ultralytics `predict()`) | $75 |
| 8 × 1× A100 nodes in parallel | 4.7 h | ~$75 + overhead |
| 8× A100 host | 38 / 19 / 4.7 h (GPUs idle unless the host reads 4.4 GB/s) | $600 / $300 / $75 |
| 8× H100 host | 38 / 19 / 4.7 h | $1,200 / $600 / $150 |

**(b) Paved pixels (NLCD impervious > 0 + 30 m):** 94 M tiles, **26–34 TB**
read (35–45% of COG blocks, including a halo for tile context).

| setup | wall time | cost |
|---|---|---|
| 1× A100 | 13–17 h, read-bound (GPU 5 h) | $26–34 |
| 4 × 1× A100 nodes | 3.3–4.2 h | $26–34 + overhead |
| 8× A100 host | 1.6–17 h | $26–270 |
| 8× H100 host | 1.6–17 h | $53–540 |

Mask preparation is a few GB of Annual NLCD and about one CPU-hour.

**Pick (b).** It runs ~5× fewer tiles and reads ~2.5× fewer bytes than (a).
Courts are pavement, so the mask should keep essentially every court. The
POI mask used for Tennessee left out 19% of the known courts. Further steps:

* Drop road-only impervious using NLCD's impervious descriptor layer. Rural
  roads cause most of the block coverage, so this could cut the bytes
  substantially (not measured).
* Use 640 px tiles inside dense blocks.
* Run on several single-GPU nodes, not one 8-GPU host. The job is
  network-bound, and per-GPU prices are the same.
* Running in Azure West Europe next to the data would remove the
  transatlantic latency entirely. For a 30–75 TB pull, give Planetary
  Computer a heads-up or use a subscription key.
* **Candidate volume:** if Tennessee's rates held (0.33% of tiles ≥ 0.65,
  0.09% ≥ 0.75), (b) would yield ~3×10^5 candidates ≥ 0.65 and ~9×10^4 ≥
  0.75. That calls for the calibration and review loop below before
  anything reaches the app.

### Recommended design for the CONUS run

* **Work unit and sharding:** one NAIP item (COG). Plan once: latest-year,
  own-state items (as `scan_state` does); each item's *zone* (the part of its
  footprint no higher-priority item owns); the 512 px COG blocks in the zone
  that touch the mask, plus a one-block halo. Write one plan row per item
  (blocks, tiles, expected bytes). Group items into shards of ~50–100 items
  (3–8 GB, 2–10 min), ordered by state and then along a Hilbert curve. Nodes
  claim shards from a queue: a status table in SQLite or Postgres, or one
  object per shard with conditional writes.
* **Node loop:** 128–192 concurrent range requests (anonymous NAIP reads or
  one shared token); threaded DEFLATE decode; tiles cut in memory with no
  JPEG round-trip (the model trained on q95 JPEG chips, so check scores on
  raw tiles once); pinned-memory batches of 512 to a TensorRT FP16 engine
  with GPU NMS; one process per GPU.
* **Resumability:** per-item results are idempotent. Each item writes
  `results/<state>/<item_id>.parquet` (detections plus tiles, bytes and
  seconds) atomically, and a shard is done when all its items exist.
  Restarts skip finished items, so a preempted node loses under a minute.
  Results sync to durable storage every few minutes, and a budget guard
  stops the fleet.
* **Dedup:** within an item, NMS across overlapping tiles, preferring boxes
  that don't touch a tile edge (`merge_detections`). Across items, keep a
  detection only if its centre is in the item's zone, so there are no
  cross-item duplicates except on zone borders. A final national pass merges
  boxes within ~20 m (spatial hash, then NMS).
* **Storing and merging:** append-only per-item Parquet merged with DuckDB or
  pyarrow into one GeoParquet partitioned by state. Stable `det-<hash>` ids;
  item, date, conf, box, mask kinds, nearest OSM feature. Drop matches within
  40 m of known courts and flag overlaps with OSM tennis/pickleball pitches.
  **Suppress residential lots** using parcels or building footprints (the
  Tennessee review shows backyard courts sit in park and church buffers).
  Export per-state GeoJSON for the app.
* **Reviewing 10^4–10^5 candidates:**
  1. Label a stratified sample (~50 per confidence bin per region) to
     measure precision per bin and set per-region thresholds.
  2. Auto-publish bins above ~90% precision as app candidates, send the
     middle band to review, and keep the rest as hard negatives.
  3. Review in a keyboard-driven page or in the app (256 px crop plus
     context, y/n/skip). At ~1.5 s per decision, 10^4 candidates take ~4 h
     and 10^5 take ~40 h, shared across volunteers. A MapRoulette challenge
     lets OSM mappers verify and add courts, which follows OSM's import
     guidelines because a human checks each one.
  4. Retrain on confirmed courts and rejected look-alikes, then rescan only
     the uncertain bins.

## v2 workflow (2026-10-09)

v2 makes three changes. It keeps private home courts off the map, it improves the detector, and
it adds a scan engine that can cover every pixel of the contiguous US. Run 2's commands, timings
and results are in [Second run](#second-run-2026-10-09).

```
osm_extract (+ sites, residential)  ──► sites (PAD-US, NCES, Overture, OSM) ──┐
build_dataset_us (v2 negatives) ──► label_clean (score / analyze / select) ──► train (416 px) ──► evaluate
                                                                                      │
scan_engine plan ──► partition ──► run (N nodes) ──► merge ──► privacy classify ──► export_app
gold sample / fetch / annotate / export ──► tools/label/index.html ──► gold score
```

### New modules

| module | what it does |
|---|---|
| `fastdet` | GPU inference without Ultralytics `predict()`. Handles uint8 tiles to FP16, resizes on the GPU, decodes, runs per-tile NMS with `torchvision.ops.batched_nms`, and supports optional flip TTA that only re-runs tiles that have candidates. Backends are PyTorch FP16 or a TensorRT 10 engine. Its output matches `predict()` exactly. |
| `scan_engine` | `plan` / `partition` / `run` / `merge` / `status` (below). |
| `label_clean` | Scores every dataset chip with a detector. `analyze` finds labels the model rejects and confident boxes on "background" chips, and writes montages. `select` applies the chosen rules and writes `train_v2.txt`. |
| `cropcls` | ResNet-18 crop classifiers. The verifier works on detection crops taken from the detector's own input tensor with `roi_align`. The context model works on 160 m NAIP crops. This module covers training, `eval-verifier`, and in-scan scoring. |
| `sites` | Builds the public / institutional site layer and the residential layer, and fetches Overture building footprints per state. `buffer-eval` checks the radius used for point sites. |
| `privacy` | Gives each candidate a privacy class (public / residents_only / private_home / restricted / unknown) and a site type, and writes the export GeoJSON. `context-data` builds OSM-labelled crops for the context CNN, and `eval-osm` scores the rules. |
| `gold` | Builds the stratified CONUS gold set: `sample` / `fetch` / `annotate` / `export` / `score`. |
| `wkb_curves` | Linearises the curve geometries in PAD-US, which shapely cannot read. |

`tools/label/index.html` is the gold-set labelling page. Its data folder `tools/label/data/` is
git-ignored.

### Privacy policy (what the app may show)

**No court on a private home lot is ever shown.** That applies to OSM courts as well as
detections. Every candidate gets one class from `privacy.classify_point`, and the first rule that
matches wins. A *house-sized* building is 40–600 m². Overture buildings classed residential/house
count up to 1,200 m². Buildings with a civic, education, religious, commercial or similar subtype
never count as houses.

1. **restricted**: inside a prison or military area. Never shown.
2. **public**: inside an OSM public-site polygon that isn't marked private, or at least 20 m inside
   PAD-US public land.
   * Public-site polygons are parks, playgrounds, recreation grounds, sports centres, schools /
     `landuse=education`, colleges, places of worship / `landuse=religious`, and community
     centres.
   * The 20 m margin is there because USGS says PAD-US is not for mapping finer than 1:24,000.
   * The context CNN can still veto this when it is very sure (p_home ≥ 0.85).
3. **residents_only**: inside a park or club marked `access=private` (an HOA park, a members'
   club).
4. **residents_only**: an apartment complex. That means `residential=apartments` landuse, or an
   apartment building (OSM or Overture class) within 40 m.
5. **public**: within the radius of a point-only site, which comes from NCES, IPEDS, Overture or
   OSM nodes.
   * The radius is school 150 m, college 250 m, and church / community / rec / park 80 m.
   * The school radius covers 89% of Tennessee courts inside an OSM school polygon (median
     92 m to the NCES point). Parks rely on polygons, not points: a park point would need ~310 m
     to cover 90%, and a radius that large would reach into neighbouring backyards.
   * This is overruled to **private_home** when the lot looks residential: a house-sized building
     within 35 m and no larger or civic building within 100 m, or p_home ≥ 0.5.
6. **private_home**: a house-sized building within 35 m, or `landuse=residential`, or p_home ≥ 0.7.
7. **unknown**: anything else.

`p_home` comes from the context CNN (`weights/context_v2.pt`, a ResNet-18 on a 160 m NAIP crop).
It was trained on OSM courts and pitches outside Tennessee:
* **home**: tagged `access=private|no`, inside `landuse=residential`, and outside any public-site
  polygon.
* **public**: inside a park, school or other public-site polygon.

**Export.** By default `export_app` only exports **public** candidates.
* `--include-residents-only` and `--include-unknown` opt in to those classes.
* `private_home` and `restricted` are never exported.
* Each exported candidate carries `site` (park, school, college, church, rec, community), an
  optional property the app ignores today.
* The current Tennessee file goes further. It only shows public candidates that were also visually
  confirmed, because no score threshold reached 90% precision (see
  [Tennessee results](#tennessee-results-and-app-file-2026-10-15)).

With `--osm-privacy out/osm_privacy_TN.json` (from `privacy classify-osm`), OSM courts follow the
same policy:
* private_home, restricted and residents_only courts are dropped. `--include-residents-only`
  keeps residents_only.
* Every court tagged `access=private|no` is dropped, including the ten in Tennessee that sit
  inside school or other public-site polygons, because the mapper said it is not public.
* public and unknown courts stay, because a person mapped them as courts and there is no sign of
  private use.

### Gold set and labelling page

**The gold set** has 300 random 640 px NAIP tiles (384 m × 384 m), with 25 per Census region ×
development stratum:
* **Strata.** Urban means ≥ 30% medium/high NLCD development within the tile. Suburban means
  ≥ 20% any development. Rural is everything else. Development comes from USDA CDL 2021, sampled
  at each tile.
* **Area shares.** Out of 12,000 random CONUS points, urban, suburban and rural cover **1.55%,
  5.16% and 93.3%** of CONUS. Scores are re-weighted by these shares.
* **Pre-annotation.** v2 pre-annotates every box scoring ≥ 0.15, tiled and merged exactly as the
  scan does. v1's boxes are kept separately in `data/gold/preds_v1.json` so both models can be
  scored against the same labels.

**Quick start for Eli** (about 300 tiles; most are empty, so roughly an hour):
1. Double-click `pipeline/tools/label/index.html`. On another computer, unzip
   `out/gold_label_data.zip` anywhere and double-click `hoop_gold_labels/index.html`.
2. For each yellow box press **1** (public court), **2** (home court), **3** (residents only),
   **4** (court, unsure) or **X** (not a court). Drag to add a court the model missed. Press
   **Space** when the tile is done.
3. When finished, or whenever you stop, type your name and click **Download JSON**. Send the file
   back. The browser also keeps your progress.

**Labelling** (`tools/label/index.html`, one HTML file, no server, no install, no internet except the
**G** map link):

1. Open it.
   * On this PC, double-click `pipeline/tools/label/index.html`. Its git-ignored `data/` folder is
     already filled (v2 pre-annotations, `tasks_id` `gold-v2-300-a667b0ce`).
   * On another machine, unzip `out/gold_label_data.zip` (41 MB) anywhere. It holds
     `hoop_gold_labels/index.html`, `data/tasks.js` and `data/images/`, so double-click that
     `index.html`.
   * It works over `file://` in Chrome, Edge and Firefox. To rebuild the package, run
     `python -m hoop_pipeline.gold export --tool tools/label --zip out/gold_label_data.zip`.
   * Work is kept per `tasks_id`, which is derived from the pre-annotations, so a new export never
     mixes with old work.
2. **Label each tile.** The model's boxes are dashed yellow with their scores. The next box to
   check is already selected (white outline), so press one key per box:
   - **1**: court, public (park, school, church, rec)
   - **2**: court on a home lot
   - **3**: court for residents only (apartments, HOA)
   - **4** or **Y**: court, access unsure
   - **X** or **Del**: not a court

   To add a court the model missed, drag a box, then press 1–4. **Space** marks the tile done and
   moves on, but only once every box has been decided. Every reviewed tile counts, including
   empty ones: they measure false positives and misses.

   Other keys:
   - **Tab / Shift+Tab**: move the selection, to go back and change a box
   - **← / →**: move between tiles
   - **N**: next unreviewed tile
   - **U** or **Ctrl+Z**: undo
   - **H**: hide the boxes
   - **Z**: 2× zoom
   - **G**: open the spot in Google satellite view
3. **Save and send.**
   * Work is saved in the browser as you go.
   * To send results, type a name and click **Download JSON** (`hoop_gold_labels_<name>_<date>.json`)
     or **Copy JSON**, then email or message the file.
   * **Import JSON** resumes from a file, for example on another machine.
4. **Score.** `python -m hoop_pipeline.gold score --labels <file>` scores v2, and adding
   `--preds data/gold/preds_v1.json` scores v1. Matching uses IoU ≥ 0.3. The output gives
   precision and recall per stratum, CONUS-weighted by area share, for all tiles and for tiles that
   don't overlap any training chip, plus the counts of public / home / residents tags.

### Scan engine v2 (`scan_engine`)

**Work unit:** one NAIP item, i.e. one COG file. It does not use Ultralytics `predict()`.

* **`plan`** sorts each state's own flights by priority: newest year, then newest date, then id.
  An item's **zone** is its footprint inside the state (buffered 100 m) minus every
  higher-priority footprint, so each location belongs to exactly one item.
  * Items coarser than 0.65 m are skipped.
  * 0.3 m items are read from their 2× overview.
  * Each item's 0.6 m output grid is cut into 320 px tiles at a 224 px stride. A tile is kept
    when its stride cell, dilated by one cell, touches the zone. This step is rasterised, so
    planning is fast (Tennessee: 2,997 items in 20 s).
* **`partition`** splits the items over N nodes × P processes (greedy by kept tiles) into
  `manifests/node<k>_p<j>.txt`. It is static and lock-free: each node works only on its own
  manifest.
* **`run`** is one worker process:
  * 128–192 reader threads stream items in block-aligned strips, so every COG block is read
    once. They share one Planetary Computer SAS token, cut the kept tiles in memory, and skip
    empty ones.
  * A packer thread fills pinned-memory batches of 512.
  * The GPU thread runs `FastDetector`, with the optional in-scan verifier on the same input
    tensor, and maps boxes to item pixels.
  * When an item's last tile is done, a finaliser merges tile duplicates. Edge-cut boxes lose
    ties. It drops boxes outside 6–120 m, keeps only boxes whose centre lies in the item's zone,
    and atomically writes `results/<ST>/<item>.parquet` and `<item>.done.json`.
  * A restart skips items that already have a done marker, so a crash loses only the items in
    flight.
  * The worker reports tiles/s, MB/s from `/proc/net/dev`, and GPU utilisation from
    `nvidia-smi`.
* **`merge`**:
  * Concatenates the per-item files.
  * Merges duplicates. Across items, two boxes merge when their centres are within 20 m. Within
    an item, only IoU or containment counts, so side-by-side courts survive.
  * Marks detections within 40 m of known OSM courts.
  * Reports recall on held-out OSM courts, both inside and outside the v1 scan mask.
* **`status`** reports progress from the done markers.

`cloud/scan_fleet.sh` runs the fleet: `launch` / `push` / `start` / `status` / `collect` /
`terminate`.

### National scan runbook

This was the plan before the run. What actually ran, and how long it took, is in
[National run](#national-run-2026-10-15).

1. **Data, once.**
   * `osm` stage (Geofabrik extract).
   * `naip` stage.
   * `sites download`, `sites overture-places`, and `sites buildings --states CONUS --workers 8`.
     The buildings step reads only the Overture files whose STAC bbox touches each state.
     Tennessee alone took 398 s, so with 8 workers expect ~40 min.
   * `sites build --states CONUS`.
2. **Plan.** On one node:

   ```bash
   python -m hoop_pipeline.scan_engine plan --states CONUS --out data/scan2/US
   ```

   Then partition for the fleet:

   ```bash
   python -m hoop_pipeline.scan_engine partition --plan data/scan2/US --nodes 8
   ```
3. **Fleet.** Start the local backstop first: `python cloud/watchdog.py --loop 10`, or the
   scheduled task above. Give every node a deadline that covers the run plus margin. With 8 nodes
   the scan takes about 4.7 h (estimate below), so use `HH_MAX_HOURS=8`. Each node's on-instance
   watchdog also kills it after 30 idle minutes.

   ```bash
   HH_MAX_HOURS=8 bash cloud/scan_fleet.sh launch n1 n2 ... n7      # main: bash cloud/launch.sh main --max-hours 8
   bash cloud/scan_fleet.sh push US main n1 ... n7
   bash cloud/scan_fleet.sh start US "--weights weights/best_v2.pt --imgsz 416 --threads 160 --engine weights/v2_416_b512.engine" main n1 ... n7
   ```

   The TensorRT engine only runs on the GPU type and TensorRT version it was built on (A100,
   TensorRT 10.16). On another type, build a new engine (`post_train_v2.sh engine`) or drop `--engine`.
4. **Watch.** Run `bash cloud/scan_fleet.sh status US main n1 ...`. Every 30–60 min, also run
   `collect US main n1 ...`, so results exist in two places.
5. **Recover a dead node.** Run `collect`, then
   `partition --pending --nodes <survivors> --results data/scan2/US/results`, then `push` and
   `start` again.
6. **Finish.**

   ```bash
   python -m hoop_pipeline.scan_engine merge --plan data/scan2/US --out out/scan2_us
   python -m hoop_pipeline.privacy classify --candidates out/scan2_us/detections.parquet --scope us ...
   ```

   Then export per region, and terminate every node.

## Second run (2026-10-09)

All heavy work ran on Lambda Cloud single-GPU A100 nodes (`gpu_1x_a100_sxm4`, us-east-1). Costs,
times and instance ids are in `cloud/last_run.json`. Run 1 is archived under `previous_runs`.

**The run was interrupted.** The agent stopped at about 14:50 UTC on 2026-10-09, right after v2
training. The jobs it had already chained on `main` (TensorRT engine, every evaluation, gold
pre-annotation, disagreement crops) finished by 15:03 UTC. Its last background step pushed the v2 plan
and engine to the three scan nodes around 15:00 UTC.
* Its four nodes stayed up until 2026-10-15 17:04 UTC: about 148 h on `main` and 147 h on each scan
  node, roughly **$1,174** against a $60 budget.
* The coordinator terminated them and copied `main`'s results to `remote_v2/`. They now sit under the
  usual `weights/`, `out/` and `data/` folders.
* Lost: the v2 Tennessee fleet scan, which was pushed and probably ran, but whose results never left
  the scan nodes. Also lost: `data/crops` and `data/osm_us`, which can be rebuilt.
* The Tennessee results below therefore come from the **v1 every-pixel fleet scan**, re-scored on a CPU
  with v2, the verifier and the context CNN (`hoop_pipeline.rescore`).
* [Safety shutoff](#safety-shutoff-watchdogs) describes the watchdogs added because of this.

### Data (Geofabrik extract of 2026-10-08)

* There are **101,217** CONUS basketball features and 94,002 labelable ones. Tennessee has 828,
  the same ids as run 1.
* The context layer now has **5.83 M** rows. New in v2:
  * pickleball and volleyball pitches, landuse=residential, `education` and `religious` landuse
  * apartment buildings, prisons and military land
  * `access` and `name` columns
* **Site layer** `data/sites/sites_us.parquet` has **2,409,217** features:
  * OSM: 961,100 sites, including 30,162 restricted ones (prisons, military).
  * PAD-US 4.1: 244,971 public, not-closed Fee polygons (out of 296,456). Some use curve geometries
    (CurvePolygon / MultiSurface), which `wkb_curves` linearises.
  * NCES: 100,384 public and 22,385 private schools. IPEDS: 6,241 colleges.
  * Overture: 1,074,100 places (557,640 places of worship, 220,791 schools, 183,383 parks,
    54,009 colleges, 41,089 community and 17,188 rec).
* **Residential layer** `residential_us.parquet`: 2.13 M polygons (1.14 M `landuse=residential`,
  52 k apartment landuse, 936 k apartment buildings).
* Tennessee:
  * 54,274 sites
  * 27,698 residential polygons
  * **3,706,569 Overture buildings**. Only 14% of them have a class (390 k `house`); most are
    unclassed Microsoft footprints.
* Downloads and build times:
  * PAD-US national FileGDB: 1.5 GB in 163 s.
  * Overture places, CONUS query with DuckDB: 148 s.
  * Overture buildings for Tennessee: 398 s. Only the 7 files whose STAC bbox touches the state
    are read; a full-footer scan of all 512 files had not finished after 10 min.

### Dataset v2 (`datasets/us2`)

Same positives as v1 (one chip per labelable court) plus **twice as many negatives**, now 12
kinds:
* tennis 40k, parking 22k, park 16k, school 16k, pickleball 14k, pool 12.8k, residential 12.8k
* random land 14.6k, other pitches 9k, volleyball 8.7k, apartments 7.3k, playground 7.3k

| split | chips | court-centred | negative-centred | boxes |
|---|---|---|---|---|
| train | 271,921 | 90,580 | 181,341 | 204,961 |
| val (Tennessee, test only) | 2,143 | 711 | 1,432 | 948 |
| val_us (model selection) | 6,046 | 2,075 | 3,971 | 4,370 |

The build took 11 min (453 chips/s).

### Label cleaning and hard negatives (`label_clean`)

v1 scored all 280,110 chips in 232 s (1,209 chips/s; JPEG decode is the limit). Of 90,671
labels, **7,486 (8.3%)** got no v1 detection ≥ 0.01 in their own chip.

Looking at the montages (`out/clean/montages/`), the lowest-scored labels are mostly not courts:
* construction sites where the imagery predates the court
* polygons shifted next to the real court, which the model does detect
* tennis courts, grass, parking and roofs
* courts hidden under canopy or too small to see

Two other patterns show up:
* Labels scored 0.05–0.15 are often a block of courts drawn as one polygon while the model boxes
  each court, or backyard tennis courts tagged basketball.
* "Background" chips that score ≥ 0.45 at schools, parks and apartments are mostly **real courts
  missing from OSM**. Tennis-centred chips scoring ≥ 0.6 are often a basketball court next to, or
  mis-tagged as, the tennis court.

The `select` rules:

| rule | effect |
|---|---|
| label < 0.01 in every chip and no confident box nearby → box deleted (object is background) | 6,138 labels (9,193 boxes) |
| other label < 0.05, polygon > 1,500 m² scoring < 0.15, or surface grass/sand/clay/dirt/turf/gravel → chips dropped | 5,668 labels (9,112 chips) |
| court chip holding another unlabelled box ≥ 0.5 (unmapped neighbour) → dropped | 1,492 chips |
| negative chip with a confident box (school/park/apartments ≥ 0.45, pitches ≥ 0.6 or ≥ 0.45 off-centre, residential/random ≥ 0.6, parking/pool ≥ 0.7) → dropped | 6,317 chips (2,162 at schools) |
| hard negatives (scored 0.1 up to the drop threshold) listed twice | 14,720 chips |
| reviewed run-1 Tennessee false positives (mining cells only, ≥ 300 m from held-out chips) listed three times | 51 chips |

The final train list (`datasets/us2/train_v2.txt`) has **269,873** lines.

**Tennessee hard negatives.** To keep Tennessee an honest test set, the run-1 false positives
come only from "mining" cells: a hash of 0.05° cells picks half of Tennessee. Every Tennessee
evaluation of v2 below (holdout chips aside) uses only the other half. Two samples of run-1
candidates were labelled visually (`out/review_v1fp/`, model-assistant labels, not gold):
* top 180 by confidence (≥ 0.75): 132 courts, 29 not, 19 unsure, so **≈ 82%** precision
* 100 random candidates scoring 0.45–0.72: 10 courts, 44 not, 46 unsure, so **≈ 19%**

False positives were mostly sand volleyball courts, roofs, parking lots, tree canopy, concrete
pads, a helipad, splash pads and construction pads.

### Model v2 vs v1

v2 (`weights/best_v2.pt`) is yolo11s fine-tuned from v1:
* trained on the cleaned list at 416 px, batch 192, for 18 epochs, in 1.49 h on one A100
* the best epoch on val_us was the last one, with mAP50 0.758 and mAP50-95 0.521
* `cloud/post_train_v2.sh` ran every evaluation below through FastDetector, the scan engine's own GPU
  path; mAP comes from Ultralytics `val`.

**Tennessee holdout** (2,143 chips, 948 OSM boxes; precision and recall at IoU 0.3):

| model | mAP50 | mAP50-95 | P / R @ 0.30 | @ 0.50 | @ 0.60 | @ 0.70 | background chips firing @ 0.5 |
|---|---|---|---|---|---|---|---|
| v1 (320 px) | 0.765 | 0.517 | 0.795 / 0.779 | 0.869 / 0.692 | 0.897 / 0.562 | 0.938 / 0.364 | 3.5% |
| v2 (416 px) | 0.762 | 0.519 | 0.813 / 0.759 | 0.888 / 0.651 | 0.897 / 0.536 | 0.930 / 0.420 | 2.7% |
| v2 + flip TTA | – | – | 0.811 / 0.760 | 0.883 / 0.658 | 0.900 / 0.533 | 0.931 / 0.413 | 2.9% |
| v2 TensorRT FP16 | – | – | 0.813 / 0.759 | 0.887 / 0.652 | 0.898 / 0.537 | 0.930 / 0.421 | 2.7% |

Precision at matched recall (best threshold reaching it): v1 0.885 / 0.857 / 0.819 / 0.720 and v2
0.888 / 0.875 / 0.813 / 0.707 at recall 0.60 / 0.70 / 0.75 / 0.80.

**val_us** (6,046 chips outside Tennessee, 4,370 boxes). This is also v2's model-selection split, so it
favours v2 slightly. v1 scored mAP50 0.772 and mAP50-95 0.510; v2 scored 0.758 and 0.518.

| threshold | v1 P / R | v2 P / R |
|---|---|---|
| 0.5 | 0.868 / 0.647 | 0.867 / 0.656 |
| 0.7 | 0.936 / 0.291 | 0.901 / 0.440 |

What this shows:
* **Against OSM labels the two models tie.** Those labels cannot reward the cleaning: v2 learned to skip
  tennis courts tagged as basketball and to box courts that OSM lacks, and both count against it here.
* **v2 fires on fewer background chips** and is more confident on real courts. At 0.7 it keeps 42–44%
  recall where v1 keeps 29–36%.
* **Flip TTA gains nothing** (within 0.005), so it is off.
* **The TensorRT engine** (`weights/v2_416_b512.engine`, A100, TensorRT 10.16) matches PyTorch to 0.001.
  Its GPU ceiling on an A100 is **8,050 tiles/s** against 3,232 for PyTorch FP16 at 416 px, about 2.5×.
  That is faster than v1 at 320 px (5,547).
* **The verifier** (`weights/verifier_v2.pt`, ResNet-18 on box crops, val AUC 0.90) doesn't help
  reliably on the Tennessee holdout:

  | precision at recall | 0.50 | 0.60 | 0.70 | 0.75 |
  |---|---|---|---|---|
  | detector | 0.905 | 0.885 | 0.875 | 0.829 |
  | detector × verifier^0.5 | 0.910 | 0.899 | 0.870 | 0.807 |
* **Blind visual head-to-heads** (model-assistant labels, not gold):
  * **Tennessee holdout disagreements** at 0.5, where the other model's boxes down to 0.3 count as
    agreement (`out/disagree_tn/`). There are only 21. v1 fired alone on 18 (2 courts, 3 not, 13
    unsure) and v2 on 3 (1 court, 2 not). That is too few, and the 320 px chips too ambiguous, to call.
  * **The 300 random CONUS gold tiles** (`out/review_gold/h2h/`). Every box ≥ 0.15 of either model was
    grouped into 38 clusters and labelled blind. Both models found the same 5 courts, and v2 made half
    the false positives at low thresholds:

    | conf ≥ | v2: boxes, court / not / unsure | v1: boxes, court / not / unsure | false positives per 1,000 km² (CONUS-weighted), v2 / v1 |
    |---|---|---|---|
    | 0.15 | 26, 5 / 11 / 10 | 35, 5 / 17 / 13 | 24.5 / 50.4 |
    | 0.50 | 14, 3 / 7 / 4 | 15, 3 / 8 / 4 | 6.0 / 10.6 |

    On random land, precision at 0.5 is only about 0.3 for either model. Away from court-rich sites,
    look-alikes outnumber courts, which is why the export keeps only public sites and needs a second
    opinion (below).

### Scan engine benchmark (Tennessee, every pixel)

The plan covers all of Tennessee: **2,997 NAIP items, 6,491,744 tiles, 109,322 km²** (zones inside
the state plus 100 m), planned in 22 s. `partition --nodes 3` gives each node 999 items and 2.16 M
tiles. Three `gpu_1x_a100_sxm4` nodes in us-east-1 scanned it with v1 (PyTorch FP16, 320 px, 160
reader threads) while v2 was still training. The v2 rerun with TensorRT was pushed to the same nodes but
its results were lost (see the interruption note above). Per node:

| node | tiles | seconds | tiles/s | MB/s | GB read | GPU util | GPU busy | limited by |
|---|---|---|---|---|---|---|---|---|
| scan2 | 2,163,912 | 498 | 4,343 | 830 | 413 | 83% | 95% | GPU |
| scan3 (after the restart) | 1,883,638 | 523 | 3,603 | 587 | 307 | 69% | 84% | reads |
| scan4 | 2,163,913 | 643 | 3,366 | 548 | 352 | 64% | 79% | reads |

* All 2,997 items finished in **11.1 min wall** on 3 nodes, with **0 item errors**. That is
  9,363 tiles/s and 1.6 GB/s aggregate, about 1.1 TB read in all, or 0.17 MB per tile.
* Each node's network differs: scan2 sustained 830–940 MB/s, while scan3 and scan4 sustained
  550–650 MB/s.
* More reader threads did not help. In a read-only re-test on 250 items, scan4 managed
  534 MB/s with 256 threads, and scan3 managed 483 MB/s with 160. The per-node network path is the
  limit, not the thread count.
* **Restart test.** After 2 minutes the scan3 worker was killed with `kill -9`, with 142 items done.
  * No partial files were left. Results are written to a dot-file, then renamed.
  * Re-running the same command logged `999 items in plan/manifest, 142 already done, 857 to do`
    and finished the rest. Only the items in flight were redone.
* **v1, every pixel** (merge in 13 s): 109,426 boxes → 109,350 after the cross-item merge. 873 of
  them are near known OSM courts, which leaves **108,477 candidates** (≥ 0.05).

  | conf ≥ | 0.10 | 0.20 | 0.30 | 0.40 | 0.50 | 0.60 | 0.70 | 0.80 |
  |---|---|---|---|---|---|---|---|---|
  | candidates | 54,111 | 24,007 | 13,430 | 8,012 | 4,789 | 2,545 | 1,008 | 29 |

  Recall on Tennessee's 719 labelable OSM courts:

  | courts | n | ≥ 0.10 | ≥ 0.25 | ≥ 0.40 | ≥ 0.50 | ≥ 0.60 | ≥ 0.70 |
  |---|---|---|---|---|---|---|---|
  | all | 719 | 0.889 | 0.844 | 0.795 | 0.757 | 0.649 | 0.427 |
  | inside the v1 POI mask polygons | 504 | 0.909 | 0.873 | 0.825 | 0.782 | 0.673 | 0.450 |
  | outside them | 215 | 0.842 | 0.777 | 0.726 | 0.698 | 0.595 | 0.372 |

  Run 1's masked scan found 87.8 / 84.3 / 79.5 / 75.0 / 63.8 / 43.6% of the 580 courts inside
  its scanned blocks. Those blocks were 512 px squares touching the mask, which is why there are 580
  courts there but only 504 inside the polygons. The every-pixel scan reaches the same recall over
  all 719 courts, including the 139 (19%) that lay outside run 1's blocks. Above 0.5 it has 3.8 times
  as many candidates (4,789 against 1,263).

**What v2 would do on the same nodes.** With TensorRT the A100's ceiling at 416 px is 7,356 tiles/s from
pinned memory, twice what any node could read. So v2 + TensorRT is read-bound, at the same ~3,400–4,300
tiles/s per node that v1 reached. In PyTorch FP16 (3,123 tiles/s ceiling, about 2,600 in the scan) v2
would be GPU-bound and about 30% slower.

### National estimate

From the CONUS plan (`data/scan2/US`, built on the cloud in 29 min) and the measured per-node rates:
* **219,187 NAIP items, 463.9 M tiles, 7.80 M km²**
* **~80 TB to read** at the measured 0.17 MB per tile (the plan's own estimate was 71 TB)

`cloud/estimate_national.py` assumes 15 min of setup per node and A100 nodes at $1.99/h. Reading the
NAIP COGs is the limit, so the per-node rate is the median of the three nodes, 3,603 tiles/s, which is
what v2 + TensorRT would reach:

| nodes | wall time | node-hours | cost | aggregate read |
|---|---|---|---|---|
| 1 | 36.0 h | 36.0 | $72 | 0.6 GB/s |
| 4 | 9.2 h | 36.8 | $73 | 2.5 GB/s |
| 8 | 4.7 h | 37.8 | $75 | 5.0 GB/s |
| 16 | 2.5 h | 39.8 | $79 | 10 GB/s |

At about 2,600 tiles/s per node (v2 without TensorRT, GPU-bound) every row is 38% longer:
1 / 4 / 8 / 16 nodes take 49.8 / 12.6 / 6.4 / 3.3 h and cost $99 / $101 / $103 / $107.

Not covered by the table:
* **Storage-account limits.** All NAIP blobs come from one Azure storage account in West Europe.
  16 nodes would pull about 80 Gbit/s from it, which may exceed that account's egress limit. Nothing
  above 1.6 GB/s (3 nodes) was measured. Ramp up 4 → 8 → 16 and watch MB/s per node.
* **The rest of the pipeline.** Privacy classification, context-CNN crops and review come on top.
  Building Overture buildings for every state takes roughly 1 node × 40–60 min.
* **Deadlines.** A single node's 36 h needs `--max-hours 40`, and the scan restarts per item anyway.
  More nodes with short deadlines are safer.


### Privacy classifier results

* **Point-site radii checked on Tennessee** (`sites buffer-eval`). For OSM courts inside an OSM
  school polygon, the school's NCES point is a median 92 m away. The p90 is 150 m, and the 150 m
  radius covers 89%.
  * Colleges: 11 pairs, median 130 m. The 250 m radius covers 82%.
  * Churches: only 4 pairs, median 63 m, so 80 m stays a cautious radius.
  * Parks: a park point would need ~310 m for 90%, which would reach into neighbouring yards.
    Parks therefore rely on OSM / PAD-US polygons, and point parks get 80 m.
* **Context CNN** (`weights/context_v2.pt`; ResNet-18; 160 m crop at 224 px; 10 epochs in 2 min).
  It was trained on OSM-labelled crops outside Tennessee: 4,636 home and 17,887 public.

  | split | AUC | home P / R at 0.5 | at 0.7 | at 0.9 |
  |---|---|---|---|---|
  | validation cells (2,508; 621 home) | 0.966 | 0.83 / 0.90 | 0.88 / 0.89 | 0.93 / 0.77 |
  | Tennessee (250; 24 home) | 0.992 | 0.69 / 1.00 | 0.76 / 0.92 | 0.85 / 0.71 |

* **Whole classifier on Tennessee's OSM-labelled courts and pitches** (`privacy eval-osm`; 2,884,
  of which 24 are home):

  | | home flagged | flag precision | home → public (privacy leak) | public courts lost |
  |---|---|---|---|---|
  | rules only | 23/24 | 1.00 | 1 | 0 |
  | rules + context CNN (used) | **24/24** | 0.30 | **0** | 55 of 2,860 (1.9%) |

  These labels come from the same OSM polygons the rules use, so they flatter rules 2–4. The
  review of scan candidates in [Tennessee results](#tennessee-results-and-app-file-2026-10-15) is an
  independent check.
* **Tennessee's 828 OSM courts** (`privacy classify-osm`, `out/osm_privacy_TN.json`): 565 public,
  134 private_home, 68 unknown, 38 residents_only, and **23 restricted** (prison yards and courts on
  military bases, all in the app before).
  * A visual check of 40 of the rules-only private_home courts found about 14 clear backyard
    courts and about 12 HOA or apartment amenities.
  * About 6 looked public: a town court tagged `access=yes`, a school, a church lot. One was a
    prison yard.
  * The export drops private_home, restricted and residents_only OSM courts, plus every court tagged
    `access=private|no` (31 in Tennessee). That costs a few public courts and keeps every known home court off the
    map.

### Tennessee results and app file (2026-10-15)

The v2 fleet scan was lost, so these results come from the **v1 every-pixel scan** of Tennessee:
109,350 detections, of which 108,477 candidates are not near a known OSM court. Everything below ran on
this PC, without a GPU.

1. **Privacy classes for all candidates** (rules only, 2 min):
   `privacy classify --candidates out/scan2_tn_v1/detections.parquet --scope TN --buildings data/sites/buildings_TN.parquet`.
2. **Second opinion for the 8,012 candidates at v1 ≥ 0.4**: `python -m hoop_pipeline.rescore` (15.5 min
   on CPU). It fetched a fresh 192 m NAIP chip for each candidate (`data/rescore/tn_v1/`) and scored it
   three ways:
   * v2 at 416 px (`conf2`, the best v2 box over the candidate)
   * the verifier
   * the context CNN (`p_home`)
3. **Classes again, now with `p_home`**: `privacy classify ... --rescore out/scan2_tn_v1/rescore.parquet`
   writes `out/scan2_tn_v1/detections_privacy.{parquet,geojson,json}`.

**Candidates by privacy class.** These exclude candidates near a known OSM court. Classes at v1 ≥ 0.4
include the context CNN, and those below 0.4 use the rules alone:

| v1 conf ≥ | public | residents_only | private_home | unknown | restricted | all |
|---|---|---|---|---|---|---|
| 0.3 | 1,924 | 130 | 7,283 | 3,937 | 156 | 13,430 |
| 0.4 | 1,416 | 90 | 4,357 | 2,039 | 110 | 8,012 |
| 0.5 | 1,050 | 56 | 2,376 | 1,215 | 92 | 4,789 |
| 0.6 | 741 | 44 | 1,083 | 606 | 71 | 2,545 |
| 0.7 | 406 | 21 | 340 | 205 | 36 | 1,008 |
| 0.8 | 17 | 0 | 5 | 5 | 2 | 29 |

The context CNN changed 1,171 of the 8,012 classes:
* 174 public → private_home (veto)
* 928 unknown → private_home
* 69 private_home → public (the house-lot rule near a point site needs `p_home` ≥ 0.25 once the CNN is
  available)

v2 agrees (`conf2` ≥ 0.5) with 52% of the public candidates but only 26% of all candidates.

Among the 1,416 public candidates at v1 ≥ 0.4, the count left at each `conf2` threshold is:

| `conf2` ≥ | 0.3 | 0.5 | 0.6 | 0.7 |
|---|---|---|---|---|
| public candidates | 1,012 | 740 | 585 | 414 |

**How precise is a score threshold?** I sampled 180 public candidates at v1 ≥ 0.4, 36 per `conf2`
band, and reviewed them blind (2× crops of 90 m, no scores shown; `out/review_tn_rescore/`,
model-assistant labels, not gold):
* **C**: court markings, hoop poles or court lines are visible. 41.
* **N**: clearly something else (sand volleyball, tennis, parking, roof, dirt, playground). 57.
* **U**: an unmarked paved slab, or too unclear. 82.

With population weighting and a bootstrap 95% CI, precision is court / (court + not):

| rule | candidates | precision (95% CI) | with unsure counted as not |
|---|---|---|---|
| v1 ≥ 0.4 | 1,416 | 0.38 (0.28–0.49) | 0.19 |
| v1 ≥ 0.4, v2 ≥ 0.5 | 740 | 0.55 (0.39–0.70) | 0.30 |
| v1 ≥ 0.6, v2 ≥ 0.5 | 618 | 0.57 (0.39–0.73) | 0.29 |
| v1 ≥ 0.7 | 391 | 0.65 (0.36–0.91) | 0.25 |
| verifier ≥ 0.9 and v2 ≥ 0.3 (unweighted) | 416 | 0.72 (21 C / 8 N / 15 U) | 0.48 |
| verifier ≥ 0.98 (unweighted) | 70 | 0.80 (8 / 2 / 4) | 0.57 |

**No score threshold reaches 90%.**
* Most errors are unmarked asphalt or concrete slabs at schools, parks and churches. At 0.6 m they look
  like courts, and many of them may be (faded courts, blacktops). They are what OSM-labelled holdout
  numbers never show.
* **The verifier is the strongest filter** on scan candidates, even though it added nothing on the
  holdout chips.
* One of the 41 courts was a backyard court that the rules called public: within 62 m of an Overture
  church point, houses nearby, `p_home` 0.45. Point-site rules can leak a home court.

**What the app shows instead: visually confirmed candidates.**
* The pool is every public candidate with v1 ≥ 0.4, v2 ≥ 0.3 and verifier ≥ 0.9: 416 candidates.
* All 416 were reviewed the same way (`out/review_tn_confirm/`): **132 courts, 48 not, 236 unsure**.
  One court looked like an HOA amenity (residents only).
* The other **131 are exported**, written by `cloud/review_rescored.py confirmed`.
* **Consistency check.** 44 pool members were also in the blind sample. 38 got the same label, and
  19 of the 21 earlier "court" calls were confirmed again (1 became unsure, 1 not). No "not" or
  "unsure" ever became "court". A second look at 20 random exported courts
  (`out/review_tn_confirm/recheck/`) confirmed all 20, although 4 of them rest only on hoop-pole dots
  at both ends of a slab.
* So precision on what is shown should be about 90% or better. That estimate rests on the reviewer's
  "court" calls being right, which is not verified against ground truth.
* **What it costs.** Recall on public courts is low: the 236 unsure slabs and everything outside the
  pool stay off the map.

**App file** (`app/assets/data/courts.geojson`, 0.59 MB, schema-valid, 1,306 unique ids;
`npx tsc --noEmit` in `app/` passes):

```bash
python cloud/review_rescored.py confirmed --dir out/review_tn_confirm --labels out/review_tn_confirm/labels.json \
    --geojson out/scan2_tn_v1/detections_privacy.geojson --out out/scan2_tn_v1/candidates_confirmed.geojson
python -m hoop_pipeline.export_app --osm data/osm_us/courts_TN.geojson --region-label tennessee \
    --sub-region chattanooga --candidates out/scan2_tn_v1/candidates_confirmed.geojson --min-confidence 0 \
    --osm-privacy out/osm_privacy_TN.json
```

| source / region | features | before (run 1) |
|---|---|---|
| osm / tennessee | 600 | 800 |
| osm / chattanooga | 22 | 28 |
| nyc_parks / nyc | 553 | 553 |
| detected / tennessee | 129 | 574 |
| detected / chattanooga | 2 | 20 |
| **total** | **1,306** | 1,975 |

* **OSM privacy policy.** It drops 206 of Tennessee's 828 OSM courts: 134 private_home, 23 restricted
  and 38 residents_only, plus 11 public or unknown courts tagged `access=private|no`. Six of the dropped
  courts are in Chattanooga (five classed private_home or residents_only, one access-tagged). Without
  `--osm-privacy` those 206 come back.
* **Detected courts by `site`.** School 71, park 41, church 9, rec 4, college 3, community 3.
* **IDs.** 79 of the 131 detected ids are run 1's ids, reused because they are within 15 m, so app
  review decisions carry over. The `confidence` shown is v1's, from 0.42 to 0.81.

## Official court truth set (`truth`, 2026-10-15)

OSM misses many courts, so scoring the detector against OSM counts real unmapped finds as errors.
Google Maps can't be used: its terms forbid copying its data. `hoop_pipeline/truth.py` builds an
independent reference set from official, openly licensed park-department, county and state inventories.
The registry `truth_sources.json` holds, per source: the endpoint, the basketball filter, the field
mapping, the quoted license, the coverage layer and the location precision. Everything runs on CPU;
downloads are small (about 20 MB in total).

```bash
python -m hoop_pipeline.truth refresh                 # download all enabled sources + coverage polygons, then build
python -m hoop_pipeline.truth build                   # re-normalise data/truth/raw/ without network
python -m hoop_pipeline.truth list                    # registry + manifest summary
python -m hoop_pipeline.truth osm-compare             # share of official courts OSM already has (40 m)
python -m hoop_pipeline.truth evaluate --candidates out/scan2_tn_v1/detections.parquet
python -m hoop_pipeline.truth evaluate --candidates all.parquet --scanned-area scanned.geojson --min-area 150
python -m hoop_pipeline.truth evaluate --candidates all.parquet --only-not-in-osm   # courts never used as positives
python -m hoop_pipeline.truth export-labels [--min-area 150]   # official courts OSM lacks -> candidate positives
python -m hoop_pipeline.truth chips --sources denver_dpr --only-unmatched --per-source 8   # NAIP spot checks
python -m unittest tests.test_truth -v
```

### Outputs (`data/truth/`, git-ignored)

* **`courts_truth.parquet`** (GeoParquet, point geometry, EPSG:4326). One row per official record:
  * ids and place: `truth_id`, `source`, `source_id`, `city`, `state`
  * names: `name`, `park_name` (filled from the coverage polygon when the source has none), `park_ref`
  * counts: `courts` (only when the source gives a count), `full_half`
  * flags:
    * `indoor`: `outdoor` / `indoor` / `covered` (roofed or fully shaded) / `unknown`
    * `status`: `active` / `closed` / `inactive` / `unknown`; removed courts are dropped
    * `feature`: `court`, or `hoop` for Chicago's backboards
    * `public`: null for planimetric layers, which include private pads
    * `tier`: `official` / `weak`
  * location: `precision`, `coverage_id`, `same_xy` (records sharing one coordinate), `lon`, `lat`
  * geometry: `area_m2` and `footprint_wkb` (the court polygon for `court_polygon` sources)
  * `attrs`: selected raw attributes as JSON
* **`coverage.parquet`**: the polygons each source is complete for (`coverage_id`, `source`, `name`, `kind`).
  `kind` is `park` (the agency's park polygons), `city` or `county`.
* **`sources.json`** (manifest): per source, the URL, quoted license, attribution, fetch time and source edit
  date, raw/kept/removed counts, court sum, coverage polygons and km², rows inside coverage, and the OSM
  comparison.
* **`osm_match.parquet` / `osm_compare.json`**: the nearest OSM court for every row, and the per-source summary.
* **`new_positive_candidates.geojson`**: official court-level records with no OSM basketball feature within
  40 m. These are candidate extra training positives (980; 493 with `--min-area 150`). They are not snapped
  yet: each must be snapped to a visible court box on NAIP before it is used as a label.

How each command works:

* **Matching.** A court-level row matches a detection within 40 m of its point, or within 15 m of its court
  polygon. A `park_point` row matches any detection inside its park polygon, or within 250 m when there is
  no polygon. Park-point rows are reported separately as "park-level" recall.
* **`evaluate`:**
  * It restricts the truth to the candidates' states, or to `--scanned-area`.
  * It drops `indoor` and `covered` rows.
  * It reports recall at each confidence threshold.
  * For every source with park coverage, it also reports the share of detections inside the covered parks
    that match an official court. Unmatched in-park detections are false positives or courts the agency
    doesn't list, so the share is a lower bound on in-park precision.
* **`--only-not-in-osm`** keeps only courts OSM lacks. Outside Tennessee, every OSM court was a training
  positive, so this is the subset the model never saw labelled.

### Sources (contiguous US, fetched 2026-10-15)

29 official sources are enabled. They hold **6,201 official records**, of which **5,216 are live courts
visible from above** (about 5,730 courts). Of those, 4,232 records (about 4,330 courts) are located at the
court, and 984 park records (about 1,400 courts) are located at the park. Chicago's 771 backboards, 198
inactive courts (196 NYC, 2 San Jose) and 16 roofed courts are kept but not scored.

`tier=weak` adds Overture `basketball_court` places (1,281 CONUS rows). Under "License" below:

* **open** means an explicit open license or public-domain dedication.
* **terms** means the portal's open-data terms allow reuse but name no standard license.

| key | publisher | records (courts) | precision | license | coverage | updated |
|---|---|---|---|---|---|---|
| `nyc_parks` | NYC Parks, Athletic Facilities (qnem-b8re) | 1,954 polygons (1,758 active/closed) | court_polygon | open: NYC Admin. Code 23-502(d), "without any ... restrictions on their use" | 2,061 NYC Parks properties; likely complete for Parks property | 2026-07-23 |
| `chicago_parks` | Chicago Park District, CPD_Facilities | 323 courts + 771 backboards | court_point | open: Public Domain | 617 CPD parks; "as of Nov 2016", **stale** | 2022-05 |
| `seattle_spr` | Seattle Parks & Rec, Basketball Court Polygons | 83 | court_polygon | terms: disclaimer only; city portal labels its own data Public Domain | 492 SPR parks; likely | 2026-10-12 |
| `boston_bprd` | Boston Parks & Rec, Park Features | 135 | court_point | open: PDDL | BPRD open space | 2026-07 |
| `dc_planimetrics` | DC OCTO planimetrics, FEATURECODE 3110 | 411 (224 at least 150 m²) | court_polygon, **every owner** | open: CC BY 4.0 | whole District | 2025 capture |
| `pittsburgh_courts` | Pittsburgh DPW via WPRDC | 105 | court_polygon | open: CC BY | city parks; **stale 2021** | 2022-12 |
| `cambridge_sports_areas` | Cambridge GIS planimetrics | 28 (some multi-court slabs) | court_polygon, every owner | open: PDDL | whole city; April 2018 flyover | 2023-11 |
| `syracuse_courts` | Syracuse Parks | 32 (47 courts) | feature_point | terms (attribution; don't redistribute modified copies) | 27 city parks | 2024-08 |
| `ma_dcr_courts` | Massachusetts DCR, Hard Courts | 51 (66) | feature_point, centre of a court area | terms: "Permission is granted to copy and use" | DCR properties | 2026-10 |
| `ri_scorp` | RI DEM / RIGIS, SCORP Inventory 2024 | 195 facilities (260) | park_point | terms: RIGIS agreement | statewide public facilities incl. schools | 2024-12 |
| `columbus_crpd` | Columbus Rec & Parks | 108 | court_point | open: CC0 | 546 CRPD properties | 2024-01 |
| `cleveland_parks` | Cleveland Park Amenities | 92 | court_point | open: **ODbL** (share-alike) | 160 parks | 2026-06 |
| `sioux_falls_parks` | Sioux Falls Park Amenities | 32 | court_point | open: CC BY 4.0 | 109 public parks | 2025-04 |
| `ramsey_county_planimetrics` | Ramsey County MN, Recreation Structures | 353 (190 at least 150 m²) | court_polygon, **every owner** | terms: use constraints "None" | whole county (St Paul + suburbs) | 2024 aerials |
| `sf_rpd` | SF Rec & Park, Functional Areas | 103 | court_polygon | open: PDDL | 255 RPD properties | 2026-10-15 |
| `san_jose_prns` | San Jose PRNS, Park Amenities | 85 (about 97; slabs carry QUANTITY) | court_polygon | open: CC BY | 613 parks | 2026-02 |
| `la_county_parks` | LA County DPR, Countywide Parks and Open Space | 618 parks (940 courts) | park_point | terms: eGIS license to "copy, publish, distribute ... adapt ... exploit" | 1,563 inventoried parks, all agencies incl. City of LA; 2016 counts | 2026-02 |
| `santa_ana_parks` | Santa Ana Open Space | 20 parks (48) | park_point | open: PDDL | city parks; **2017** | 2017-04 |
| `denver_dpr` | Denver Parks & Rec, Court Surfaces | 107 | court_polygon | open: CC BY 3.0 | 376 parks | 2026-10-09 |
| `austin_apr` | Austin Parks & Rec, facility points | 72 (66 visible) | court_point | terms: portal says public domain (GeoHub only disclaims) | 370 parks | 2026-10-05 |
| `phoenix_pard` | Phoenix Parks & Rec, ParksOpenData/0 | 155 | court_point | terms: revocable licence to use, reproduce and redistribute | 254 parks | 2026-10-15 |
| `college_station_parks` | College Station Parks, Sport Court | 29 (22 visible) | court_polygon | open: CC BY 4.0 | 84 parks | 2026-04 |
| `tempe_courts` | Tempe, Courts (Park App) | 33 (39) | court_polygon | open: CC BY 4.0 | 81 parks; partial; **2022** | 2022-06 |
| `okc_parks` | Oklahoma City Parks, Recreation_Parks/0 | 51 (52) | court_point | terms: open-data policy, "no restrictions on use or reuse" | 169 parks; partial | 2026-10 |
| `boulder_parks` | Boulder Parks, Park Sites | 11 parks (about 11) | park_point | open: CC0 | 104 park sites | 2023-11 |
| `nashville_metro_parks` | Metro Nashville Parks, Park Features | 43 (76, from hoop counts) | court_point | terms: Executive Order FO018 ("informational purposes") | 130 Metro parks | 2026-10-15 |
| `raleigh_prcr` | Raleigh PRCR, Athletic Courts | 61 | court_polygon (see precision) | terms: revocable | 205 parks | 2026-09-16 |
| `brec_parks` | BREC (Baton Rouge), Park Amenities | 86 parks | park_point | open: Public Domain | 174 BREC parks | 2026-09-07 |
| `lexington_parks` | Lexington-Fayette, Park Locations | 54 parks | park_point | terms: LFUCG terms of use | none | 2026-05 |
| `overture_places` (weak) | Overture places 2026-09-23.1 | 1,281 | feature_point | open: CDLA-Permissive-2.0 / Apache-2.0 | none (named places only) | 2026-09 |

**Found but not used.** The license is unclear, restrictive, or only a disclaimer. Ask the agency before
using these. The full list and reasons are in the survey notes.

* Montgomery Parks MD: 224 court polygons, disclaimer only.
* Baltimore BCRP: 129 points.
* Arlington VA: 79 points, "not considered in the Public Domain".
* Fairfax County planimetrics: 1,629 polygons, internal or personal use only.
* Madison WI: 148 points.
* Portland PP&R: 82 points.
* Sacramento: 93 points.
* Mecklenburg County: 127 points.
* BREC court points: 182.
* Tucson: 69 polygons.
* Boise: 28 polygons.
* Tacoma: 30 polygons.
* Eugene: 33 polygons.
* Oakland: 43 polygons.
* Florida FORI: 2,697 sites / 4,958 courts, statewide. It is in the registry with `"enabled": false`.
* Tennessee Statewide Recreation Areas: 445 parks / 587 courts. Also in the registry with `"enabled": false`.

These cities publish **no** usable official court layer: City of Los Angeles (only LA County's park counts),
Philadelphia (tennis only), Houston, Dallas, San Antonio, Minneapolis, Indianapolis, Kansas City, St Louis,
Milwaukee, Knoxville (KGIS needs credentials), Memphis, Chattanooga (only internal hoop points for a few
parks) and Atlanta (park-level counts, no license). Maryland "Sport Venues - Basketball" is 94 tourism
venues, mostly indoor arenas.

Two other sources were checked and not used:

* **Foursquare OS Places** is Apache-2.0, but since 2025 it is a gated Hugging Face dataset that needs an
  account and accepting its terms. The public S3 bucket is empty. Overture already contains the
  Foursquare-sourced rows.
* **Recreation.gov RIDB and NPS** have essentially no basketball courts.

### Location precision (NAIP 0.6 m spot checks, `truth chips`)

* **On the court:**
  * Polygons drawn on the court: NYC, Seattle, SF, San Jose, Denver, Pittsburgh, College Station, Tempe,
    DC and Ramsey.
  * Points on the court: Chicago (92% fall inside an OSM court polygon), Boston, Columbus, Cleveland,
    Phoenix, Sioux Falls and OKC.
  * In these sources a matched court's median distance to OSM is 0–2 m, and courts in the same park are
    about 15–20 m apart.
* **One record, several courts:** San Jose, Tempe and Cambridge polygons can cover multi-court slabs.
  Nashville, OKC and Syracuse points can be multi-court pads. Use `courts`.
* **Austin:** points are on the court. Six records sit under full shade roofs or full tree canopy, and are
  marked `indoor=covered` from `DESCRIPTION`.
* **Raleigh:** polygons are court-sized, but in the NC 2022 NAIP several unmatched ones sit in forest or
  on empty lots, with no court visible. Treat Raleigh as unverified.
* **Nashville:** some unmatched points are unmarked slabs, and one is in woods.
* **MA DCR:** points are the approximate centre of a hard-court area, about 30–50 m off.
* **Syracuse:** one point per park and court size.
* **Park-level points (`park_point`):** LA County, Santa Ana, Boulder, RI SCORP, BREC and Lexington.
  * The point is a park centroid or a geocoded address, 50–300 m from the court. Only park-level recall
    is meaningful.
  * BREC's points are often street addresses outside the park polygon: 79 of 86 fall inside.
* **Planimetric layers (DC, Ramsey, Cambridge):** every visible court is traced, whoever owns it. That
  includes backyard pads, rooftop courts and half-court keys of 16–100 m².
  * Use `--min-area 150` to compare with park-scale courts.
  * `public` is null for these rows.

### OSM coverage of official courts (`osm-compare`, 40 m)

These are live, visible courts. "Same park" means an OSM basketball feature in the same park polygon.

| source | records | in OSM | share | same park |
|---|---|---|---|---|
| phoenix_pard | 155 | 152 | 0.98 | 0.99 |
| san_jose_prns | 83 | 81 | 0.98 | 0.98 |
| sf_rpd | 103 | 100 | 0.97 | 0.97 |
| cleveland_parks | 92 | 87 | 0.95 | 0.95 |
| chicago_parks | 323 | 302 | 0.94 | 0.94 |
| seattle_spr | 83 | 77 | 0.93 | 0.93 |
| college_station_parks | 22 | 19 | 0.86 | 0.86 |
| tempe_courts | 33 | 28 | 0.85 | 0.85 |
| boston_bprd | 135 | 113 | 0.84 | 0.85 |
| pittsburgh_courts | 105 | 88 | 0.84 | 0.86 |
| sioux_falls_parks | 32 | 27 | 0.84 | 0.88 |
| syracuse_courts | 31 | 26 | 0.84 | n/a |
| nyc_parks | 1,758 | 1,459 | 0.83 | 0.86 |
| columbus_crpd | 108 | 89 | 0.82 | 0.87 |
| denver_dpr | 107 | 88 | 0.82 | 0.82 |
| okc_parks | 51 | 42 | 0.82 | 0.86 |
| austin_apr | 66 | 50 | 0.76 | 0.80 |
| cambridge_sports_areas | 28 (25 at least 150 m²) | 20 | 0.71 (0.80) | 0.79 |
| nashville_metro_parks | 43 | 29 | 0.67 | 0.67 |
| ma_dcr_courts | 51 | 33 | 0.65 | n/a |
| raleigh_prcr | 59 | 30 | 0.51 | 0.51 |
| dc_planimetrics | 411 (224 at least 150 m²) | 158 | 0.38 (0.68) | n/a |
| ramsey_county_planimetrics | 353 (190 at least 150 m²) | 128 | 0.36 (0.60) | n/a |
| la_county_parks (park-level) | 618 parks | | | 0.75 |
| santa_ana_parks (park-level) | 20 | | | 0.80 |
| ri_scorp (park-level, 250 m) | 195 | | | 0.64 |
| lexington_parks (park-level, 250 m) | 54 | | | 0.63 |
| brec_parks (park-level) | 86 | | | 0.34 |
| boulder_parks (park-level) | 11 | | | 0.91 |
| **all official, court-level** | **4,232** | **3,226** | **0.76** | 0.80 |
| overture_places (weak) | 1,281 | 335 | 0.26 | n/a |

What the comparison shows:

* **Park courts are well mapped.** In big-city parks OSM already has 82–98% of the official courts.
* **The gap is outside parks.** OSM is missing most of the courts that only all-owner planimetrics see:
  schools, apartments and homes. That is 32% of DC's and 40% of Ramsey's courts of at least 150 m².
  Smaller park systems (Baton Rouge, Nashville, Raleigh) are also patchy.
* **Many unmatched park records aren't courts you can see.** On NAIP they are roofs, canopy, empty lots
  or stale records (Austin, Raleigh, Nashville). So "official minus OSM" is not automatically a list of
  missing courts.

### National scan evaluation (snapshot 2026-10-15 18:00 EDT)

**Input.** The input is the per-item results in `data/scan2/US/results/<ST>/*.parquet` (read-only).

* Only items with a `.done.json` were used: 280,217 detections at `conf` 0.10 or higher.
* The scanned area is the union of the plan zones of those done items, passed as `--scanned-area`.
* Complete states: **CA, MT, NM, NV, TN, TX**.
* Partial states, by share of items done: AZ 90%, CO 91%, WY 83%, OR 55%, MN 43%, UT 25%. Only truth
  inside the done zones is scored.
* Not yet scored: ID, KS, ND, NE and SD had started but were not used. No truth source there anyway,
  except Sioux Falls in SD.

Recall (court match within 40 m of a point, or 15 m of a polygon):

| source | records | 0.10 | 0.25 | 0.40 | 0.50 | 0.60 | 0.70 |
|---|---|---|---|---|---|---|---|
| sf_rpd (polygons at least 150 m²) | 92 | 0.96 | 0.96 | 0.95 | 0.94 | 0.87 | 0.74 |
| san_jose_prns (at least 150 m²) | 78 | 0.97 | 0.96 | 0.94 | 0.94 | 0.89 | 0.67 |
| phoenix_pard (AZ partial) | 129 | 0.94 | 0.91 | 0.90 | 0.87 | 0.80 | 0.69 |
| tempe_courts | 20 | 0.95 | 0.95 | 0.95 | 0.90 | 0.90 | 0.85 |
| college_station_parks | 22 | 0.86 | 0.86 | 0.86 | 0.86 | 0.86 | 0.77 |
| nashville_metro_parks (TN, held out) | 43 | 0.88 | 0.88 | 0.86 | 0.77 | 0.72 | 0.58 |
| denver_dpr (CO partial, at least 150 m²) | 97 | 0.82 | 0.78 | 0.72 | 0.70 | 0.62 | 0.43 |
| austin_apr | 66 | 0.80 | 0.77 | 0.71 | 0.65 | 0.58 | 0.50 |
| ramsey_county_planimetrics (MN partial, at least 150 m²) | 62 | 0.79 | 0.71 | 0.66 | 0.63 | 0.56 | 0.31 |
| **all court-level (at least 150 m²)** | **609** | **0.89** | **0.86** | **0.84** | **0.81** | **0.74** | **0.59** |
| la_county_parks (park-level) | 618 parks | 0.87 | 0.85 | 0.83 | 0.78 | 0.73 | 0.61 |
| santa_ana_parks (park-level) | 20 | 1.00 | 0.95 | 0.90 | 0.90 | 0.85 | 0.60 |
| boulder_parks (park-level) | 11 | 0.82 | 0.82 | 0.82 | 0.82 | 0.73 | 0.55 |
| overture_places (weak; mostly indoor gyms) | 350 | 0.22 | 0.22 | 0.21 | 0.20 | 0.19 | 0.15 |

Results:

* **Courts OSM lacks.** On these 86 court-level records (`--only-not-in-osm`), recall falls to 0.62 at 0.10
  and 0.49 at 0.5. They were never positives in training, so this is the honest number, but it is pulled
  down by records that aren't visible courts (see precision above).
* **Detections inside covered parks** that match an official court, at conf 0.5: Phoenix 110/110, Tempe
  20/20, College Station 19/19, SF 87/91, Denver 67/70, San Jose 98/108, Austin 42/47, Nashville 32/37.
  At conf 0.10 the shares fall to 54/102 (Austin) and 109/138 (San Jose).
* **Tennessee v1 scan.** The v1 every-pixel TN scan (`out/scan2_tn_v1/detections.parquet`) scores 0.79 on
  Nashville at 0.5, with 32/36 in-park detections matching.

Results are in `data/truth/eval_national_20261015/`: `eval_national_{official,minarea150,notinosm,weak}.json`,
`eval_tn_v1.json`, `scanned_area.geojson`, and the done-item lists. To redo the evaluation, run its two
scripts: `scripts/concat_national.py` concatenates the done items' parquets, and `scripts/scanned_area.py`
unions the same items' `plan.parquet` `zone_wkb` (EPSG:5070).

### Which cities make an honest test set

**Recommended:**

* **Tennessee.** It was held out of training entirely. The only open court-level TN source is Nashville,
  with 43 records.
* **Courts OSM lacks** (`--only-not-in-osm`), in sources whose geometry is on the court:
  * Denver (19 such records, 17 of at least 150 m²)
  * NYC (296; 154 of at least 150 m²)
  * DC planimetrics (72 of at least 150 m²)
  * Ramsey County (76 of at least 150 m²)
  * Boston (22)
  * Columbus (19)
  * Chicago (21)
  * Pittsburgh (14)

  Review them with `truth chips --only-unmatched` first, and drop records with no visible court.
* **For all-owner recall:** DC and Ramsey County planimetrics with `--min-area 150`. They are the only
  sets that include school, apartment and home courts, which is what the national scan actually finds.

**Optimistic:** courts outside Tennessee that OSM already has (Phoenix, SF, San Jose, Chicago, Seattle,
Cleveland). They were training positives. Use them to check that the pipeline runs and that positions
match, not to measure generalisation.

**Not recommended** as the main test: Raleigh and Austin (many records not visible), stale sources
(Chicago 2016, Pittsburgh 2021, Tempe 2022, Santa Ana 2017), and park-level sources.

### Caveats

* **Licenses.**
  * "terms" sources have no standard license. Phoenix and Raleigh terms are revocable, and Nashville's
    say "informational purposes". The registry keeps each quote.
  * Cleveland is ODbL: a published derivative must stay ODbL.
  * Syracuse asks that modified copies not be redistributed.
  * The truth files are for internal evaluation. Don't ship them in the app.
* **Stale data.** Official inventories can be out of date. Chicago is from Nov 2016 and Pittsburgh from
  2021. NAIP dates from 2021–2023, so built, removed or resurfaced courts disagree in both directions.
* **What park sources cover.** Park-department layers cover only that agency's parks, not schools, housing
  authorities or private courts. So the "in covered parks" match share is a lower bound on precision, and
  recall over a whole city can't come from them.
* **Duplicates.** Nothing is deduplicated across sources. The overlaps are:
  * Boston BPRD vs MA DCR (different owners)
  * Ramsey County, which already contains Saint Paul's courts
  * LA County vs Santa Ana (no overlap)
* **Precision classes** were spot-checked on a few NAIP chips per source, not exhaustively.

## National run (2026-10-15)

The whole contiguous US (48 states + DC) was scanned with v2 at every NAIP pixel. The results were then
merged, privacy-classified, re-scored, checked against the truth set and exported per state. Outputs are
git-ignored under `out/national/` (and `data/national/`, `data/sites/`).

### Scan: what ran

* **Plan** (`data/scan2/US`): 219,198 NAIP files (one COG per item), 463.9 M planned 320 px tiles, 7.80 M km².
  `partition --nodes 8 --by-state` gave each node about 27,400 files and 58.0 M tiles.
* **Detector**: `best_v2.pt` at imgsz 416, conf ≥ 0.1, TensorRT FP16. The in-stream verifier (`verifier_v2.pt`)
  scored every box ≥ 0.1 (`--verify-min 0.1`), so every detection carries `verify`.
* **Fleet**: 8 × `gpu_1x_a100_sxm4` in us-east-1 (s1–s8 = manifests node0–node7), launched 18:32–18:39 UTC
  with `--max-hours 8`.
  * Each node ran `cloud/national_node.sh`: setup, TensorRT engine, then up to 4 `scan_engine run` passes.
  * `cloud/pull_results.py` (Windows scheduled task, every 10 min) copied every finished file to this PC.
* **Imagery read**: about 138 TB came in on the node interfaces, 0.30 MB per tile. The Tennessee benchmark
  measured 0.17 MB per tile.

| node | first pass: tiles/s | MB/s | GPU util | read errors per pass | last finished file (UTC) | terminated (UTC, ±5 min) | hours | cost |
|---|---|---|---|---|---|---|---|---|
| s1 | 1,571 | 475 | 42% | 1,031 / 387 / 231 / **145** | 10-16 09:22 | 09:58 | 15.4 | $30.70 |
| s2 | 1,787 | 452 | 47% | 561 / 0 | 04:25 | 04:58 | 10.4 | $20.75 |
| s3 | 1,786 | 458 | 47% | 566 / 0 | 04:26 | 04:58 | 10.4 | $20.75 |
| s4 | 1,857 | 469 | 48% | 581 / 0 | 04:11 | 04:38 | 10.1 | $20.08 |
| s5 | 2,875 | 854 | 56% | 108 / 0 | 00:40 | 01:28 | 6.8 | $13.58 |
| s6 | 2,214 | 590 | 51% | 290 / 0 | 02:31 | 03:18 | 8.7 | $17.23 |
| s7 | 2,224 | 585 | 51% | 325 / 0 | 02:32 | 03:18 | 8.7 | $17.23 |
| s8 | 2,945 | 866 | 57% | 191 / 0 | 00:37 | 01:08 | 6.5 | $12.91 |
| post (this step) | 1,519 (rescan) | 512 | 53% | 0 | 10-16 15:41 | 16:12 | 1.4 | $2.83 |

**Throughput.**
* The 7 healthy nodes finished their manifests in 5.6–9.5 h (first file 18:53 UTC, last 04:26 UTC). That is
  about 13,500 tiles/s aggregate, or 1,690–2,860 tiles/s per node over each node's span.
* s1 finished its first pass at 04:52 UTC with 1,031 errors and spent 4.5 more hours on retries.
* The plan had estimated 3,600 tiles/s per node and 4.7 h. Reads were the limit: 450–870 MB/s per node, with GPU
  utilisation of 42–57%.
* Every node had read errors on its first pass (108–1,031 RasterioIOError "Read failed"), and each was retried.
  s1's network path stayed bad: its later passes ran at 96–210 tiles/s, and 145 files still failed after pass 4.

**Cost.**
* Scan fleet: **$154.19** (77.5 node-hours, including the $0.96 watchdog test node). That figure is a log
  estimate, because the nodes terminated themselves: each `terminated_at` in `cloud/last_run.json` is the last
  sighting in `cloud/logs/watchdog.log` + 5 min. The owner's account figure was about $157.
* Post-processing (this step): **$2.83** (1.42 h on one A100).
* Total about **$157** by the log estimate, about $160 with the account figure.

**Watchdog behaviour observed.**
* **s2–s8.** They self-terminated through the 30-min idle rule, 22–53 min after their last finished file.
  `pull_results.py` had already logged "scan finished and fully copied" for each. Nothing was lost: every node's
  done markers are on this PC.
  * **s5 and s8** finished before their 02:32 UTC launch deadline. The puller's hold kept them alive until it
    released the hold (01:00 and 00:40 UTC), and they ended about 30 min later.
  * **s2–s4, s6 and s7** finished after 02:32. A hold is capped at the launch deadline, so theirs had already
    lapsed, and they ended about 30 min after their last pipeline activity (the final pass, then pack).
  * After 02:32, only the 10-min puller stood between a finished node and its deletion. Uncopied results would
    have been safe for only about 30 minutes.
* **s1.**
  * It stopped at 27,255 / 27,400 files. Its 4th pass ended at 09:22:36 UTC with the 145 failures, and
    `national_scan.sh` exited (`NODE_SCAN_INCOMPLETE`).
  * The node was still listed at 09:53 and gone by 10:03, so it ended **31–41 min after its last finished
    file**.
  * That matches the 30-min idle rule. The scan process had exited, and the puller's hold had expired at the
    node's launch deadline (02:32), because holds are capped at it.
  * It does not match the 60-min stall rule, which would have fired at about 10:23 or later. The local
    `watchdog.py` had moved its own deadline out to 10:29 from the puller's progress record.
  * The on-instance log was lost with the node, so which rule fired is inferred from these times, not read
    from the log.
* **The post node.**
  * It was launched with `--max-hours 4` and no `progress_glob`, so its deadline was a hard 18:46 UTC.
  * The watchdog was installed and its self-test passed before any work: timer active, cron fallback, and
    `GET /instances` resolved its own id.
  * Holds of 45–60 min covered the uploads. It was terminated by hand at 16:12, and `GET /instances` then
    returned 0.

### Missing files and rescan

* Comparing the 8 manifests with the done markers found 219,053 of 219,198 files done.
* The **145 missing were all node0 (s1)**: AZ 55, CO 32, OK 21, AR 15, KS 13, IA 5, MN 3, IL 1.
  * Each had a partial parquet but no done marker. A failed item writes its parquet and skips the marker.
  * Nodes 1–7 were complete.
* On the post node, `national_post.sh rescan` ran all 145 in **one pass with 0 errors**: 340,423 tiles in
  224 s, PyTorch FP16, 300 detections, the same model and thresholds as the scan. So the failures belonged to
  s1's link, not to the files.
* The new files are copied back, so this PC now has all **219,198 / 219,198** done markers.
* `scan_engine merge` now reads only files that have a done marker.

### Merge, dedupe, known courts

`scan_engine merge --plan data/scan2/US --out out/national` (4 min on the node):

* **Raw detections.** 986,765 at conf ≥ 0.1. The cross-file merge (boxes from different files centred within
  20 m; IoU / containment within a file) leaves **983,856**.
* **Near known courts.** **94,039** are within 40 m of an OSM basketball feature (or their box touches one),
  marked `near_known_osm`. That leaves **889,817 candidates**:

  | conf ≥ | 0.1 | 0.2 | 0.3 | 0.4 | 0.5 | 0.6 | 0.7 | 0.8 |
  |---|---|---|---|---|---|---|---|---|
  | candidates | 889,817 | 481,655 | 320,008 | 226,735 | 159,140 | 107,235 | 60,699 | 9,937 |

* **Recall on OSM courts.** Over the 93,838 labelable OSM courts inside the scan: 0.88 / 0.86 / 0.83 / 0.80 /
  0.74 / 0.66 / 0.52 at 0.1 / 0.2 / 0.3 / 0.4 / 0.5 / 0.6 / 0.7.
  * Outside Tennessee these courts were training positives, so this is optimistic.
  * By state at 0.5 it ranges from 0.54 (ND) to 0.89 (DE).

### Privacy

* **New national inputs, built on the post node.**
  * Overture buildings 2026-09-23.1 for all 49 states: `buildings_<ST>.parquet`, 6.6 GB, 30 min with 8
    workers. They are now also in `data/sites/`; this replaced the old `buildings_TN.parquet`, which came from
    the same release.
  * Per-state slices of `sites_us` / `residential_us` (`national split-sites`, 93 s).
  * Context-CNN `p_home` for all 101,098 OSM basketball features (`national osm-phome`, 270 s, about 375 crops/s).
* **Screen for the context CNN.** Candidates with conf ≥ 0.3 **and** in-scan verifier ≥ 0.5: **214,197**.
  * `rescore` fetched a fresh 192 m NAIP chip for each (17.2 min, about 208 chips/s) and recorded three scores:
    v2 again (`conf2`), the verifier on the centred chip (`verify`) and `p_home`.
  * This screen contains every export candidate and every stratum of the review below. Candidates outside it
    are classed by the rules alone.
* **Classification.** `national privacy --min-conf 0.1` classed every one of the 889,817 candidates, plus every
  OSM feature, in 1.4 min with 16 processes. Candidates by class:

  | conf ≥ | public | residents_only | private_home | unknown | restricted | all |
  |---|---|---|---|---|---|---|
  | 0.1 | 150,408 | 7,450 | 453,613 | 268,910 | 9,436 | 889,817 |
  | 0.3 | 85,265 | 3,434 | 153,502 | 72,590 | 5,217 | 320,008 |
  | 0.5 | 57,401 | 2,001 | 66,784 | 29,336 | 3,618 | 159,140 |
  | 0.7 | 30,625 | 843 | 18,921 | 8,301 | 2,009 | 60,699 |

  * **What the context CNN changed.** It produced 21,989 of the private_home calls inside the screen:
    * 16,443 unknown → private_home
    * 4,228 near a point site
    * 1,318 vetoes inside a public polygon
  * **Screened public candidates by site.** School 46,058, park 13,514, church 4,698, rec 1,556, college 990,
    community 383.
  * **OSM courts.** 101,098 features: 76,729 public, 4,050 unknown, 2,972 residents_only, 14,859 private_home,
    2,488 restricted.
    * The export keeps **78,893**. It drops private_home, restricted and residents_only courts, and every court
      tagged `access=private|no`.
    * As in Tennessee, non-outdoor basketball features (sports centres) are kept.

### Verifier: node-side scores confirmed

On the 214,197 rescored candidates, the in-scan verifier (`verify_scan`) and the fresh-chip verifier (`verify`)
agree closely:
* Pearson 0.86, Spearman 0.88.
* The difference has a median of 0.023 and a p90 of 0.116.
* Of the 50,144 candidates with conf ≥ 0.5 and `verify_scan` ≥ 0.9, 93.6% also score ≥ 0.9 on the fresh chip
  and 100% score ≥ 0.5. The second v2 pass (`conf2`) is ≥ 0.5 for 90% of them.

The fresh chip scores slightly higher: 70,823 candidates reach ≥ 0.9 against 61,765 in-scan. So the export uses
the in-scan score, which every detection has.

The verifier separates known courts from new candidates. At conf ≥ 0.5, it scores 67% of the detections on OSM
courts ≥ 0.9, against 32% of the new candidates.

### Recall versus the truth set

`national truth-eval` writes `out/national/truth/`. It covers all 983,856 detections, including those near OSM
courts. A court-level record matches a detection within 40 m of its point or 15 m of its polygon.

| truth subset | records | 0.10 | 0.25 | 0.40 | 0.50 | 0.60 | 0.70 |
|---|---|---|---|---|---|---|---|
| all official court-level | 4,232 | 0.77 | 0.73 | 0.70 | 0.66 | 0.60 | 0.50 |
| court-level, ≥ 150 m² | 3,334 | **0.87** | 0.84 | 0.81 | **0.77** | 0.71 | 0.59 |
| courts OSM lacks (never training positives) | 1,006 | 0.43 | 0.37 | 0.32 | 0.27 | 0.24 | 0.17 |
| courts OSM lacks, ≥ 150 m² | 516 | **0.63** | 0.57 | 0.52 | **0.47** | 0.42 | 0.32 |
| Overture `basketball_court` places (weak, mostly gyms) | 1,281 | 0.24 | 0.23 | 0.22 | 0.22 | 0.20 | 0.15 |

* **Same area as the 2026-10-15 snapshot.** Restricted to the snapshot's 12 states and scanned area, every
  source reproduces the snapshot's recall exactly, all 609 court-level records ≥ 150 m²: 0.89 / 0.86 / 0.84 /
  0.81 / 0.74 / 0.59.
  * Merging changed nothing there.
  * Recall over the whole country is a little lower, 0.87 / 0.84 / 0.81 / 0.77 / 0.71 / 0.59, because it adds
    the eastern sources.
  * The snapshot's courts OSM lacks (86 records) gave 0.62 at 0.10 and 0.49 at 0.50. The national 516 give
    0.63 and 0.47.
* **Per source** (≥ 150 m² where a source is planimetric):

  | source | rows | 0.10 | 0.25 | 0.40 | 0.50 | 0.60 | 0.70 |
  |---|---|---|---|---|---|---|---|
  | nyc_parks | 1,272 | 0.84 | 0.81 | 0.80 | 0.77 | 0.73 | 0.68 |
  | chicago_parks | 323 | 0.89 | 0.85 | 0.80 | 0.75 | 0.70 | 0.50 |
  | dc_planimetrics (all owners) | 224 | 0.83 | 0.80 | 0.78 | 0.73 | 0.66 | 0.45 |
  | ramsey_county_planimetrics (all owners) | 190 | 0.83 | 0.74 | 0.66 | 0.62 | 0.55 | 0.32 |
  | phoenix_pard | 155 | 0.95 | 0.92 | 0.92 | 0.89 | 0.83 | 0.73 |
  | boston_bprd | 135 | 0.96 | 0.94 | 0.93 | 0.93 | 0.91 | 0.81 |
  | columbus_crpd | 108 | 0.96 | 0.93 | 0.87 | 0.83 | 0.73 | 0.50 |
  | denver_dpr | 102 | 0.83 | 0.78 | 0.72 | 0.71 | 0.61 | 0.43 |
  | pittsburgh_courts | 99 | 0.95 | 0.92 | 0.90 | 0.87 | 0.79 | 0.64 |
  | sf_rpd | 92 | 0.96 | 0.96 | 0.95 | 0.94 | 0.87 | 0.74 |
  | cleveland_parks | 92 | 0.96 | 0.96 | 0.91 | 0.85 | 0.79 | 0.58 |
  | san_jose_prns | 78 | 0.97 | 0.96 | 0.94 | 0.94 | 0.89 | 0.67 |
  | austin_apr | 66 | 0.80 | 0.77 | 0.71 | 0.65 | 0.58 | 0.50 |
  | raleigh_prcr | 58 | 0.67 | 0.67 | 0.57 | 0.40 | 0.24 | 0.22 |
  | seattle_spr | 52 | 0.89 | 0.81 | 0.71 | 0.71 | 0.50 | 0.42 |
  | okc_parks | 51 | 0.88 | 0.82 | 0.77 | 0.72 | 0.65 | 0.61 |
  | ma_dcr_courts | 51 | 0.67 | 0.67 | 0.61 | 0.59 | 0.47 | 0.37 |
  | nashville_metro_parks (TN, held out) | 43 | 0.88 | 0.88 | 0.86 | 0.77 | 0.72 | 0.58 |
  | la_county_parks (park-level) | 618 | 0.87 | 0.85 | 0.83 | 0.78 | 0.73 | 0.61 |
  | ri_scorp (park-level) | 195 | 0.94 | 0.90 | 0.88 | 0.83 | 0.77 | 0.61 |
  | brec_parks (park-level) | 86 | 0.73 | 0.63 | 0.52 | 0.49 | 0.43 | 0.27 |

* **Detections inside covered parks** that match an official court, all detections: 2,195 / 2,324 (0.94) at conf
  0.5, and 2,659 / 3,248 (0.82) at 0.1. Most of these are courts OSM already has.
* **Candidates, the new finds.** The honest number is for public candidates not near an OSM court. In a covered
  park such a candidate can only match an official court OSM lacks. The share that do, at conf ≥ 0.5, is:

  | `verify_scan` ≥ | 0 | 0.5 | 0.8 | 0.9 | 0.95 | 0.98 |
  |---|---|---|---|---|---|---|
  | public candidates (CONUS) | 57,401 | 50,070 | 36,645 | 25,200 | 14,879 | 5,431 |
  | in covered parks: matched / all | 166 / 238 | 156 / 200 | 134 / 153 | 109 / 120 | 73 / 78 | 29 / 30 |
  | share (lower bound on in-park precision) | 0.70 | 0.78 | 0.88 | 0.91 | 0.94 | **0.97** |
  | recall on the 1,006 courts OSM lacks | 0.225 | 0.215 | 0.189 | 0.157 | 0.112 | 0.047 |

### Review sheets and precision

* **The sample.** `national review --name public_strata` drew public candidates screened by the CNN, 10 per
  stratum of conf (0.3 / 0.5 / 0.6 / 0.7 / 1) × `verify_scan` (0.5 / 0.8 / 0.9 / 0.98 / 1). That is 160 crops,
  each 96 m at 2×, with only a number on each.
* **How they were labelled.** Blind, model-assistant labels, not gold:
  * **C**: court markings or hoops visible
  * **N**: clearly something else
  * **U**: an unmarked slab or unclear
* **Result.** 47 C, 38 N, 75 U. `national score` weights each stratum by its population and bootstraps a 95% CI.
  Precision is C / (C + N):

  | conf ≥ | `verify_scan` ≥ | candidates | precision (95% CI) | unsure counted as not |
  |---|---|---|---|---|
  | 0.3 | 0.5 | 67,199 | 0.50 (0.39–0.62) | 0.30 |
  | 0.5 | 0.5 | 50,070 | 0.61 (0.46–0.72) | 0.37 |
  | 0.5 | 0.8 | 36,645 | 0.70 (0.54–0.85) | 0.43 |
  | 0.5 | 0.9 | 25,200 | 0.80 (0.58–0.98) | 0.50 |
  | 0.3 | 0.98 | 5,982 | 0.90 (0.82–0.97) | 0.61 |
  | **0.5** | **0.98** | **5,431** | **0.92 (0.85–0.98)** | 0.64 |
  | 0.7 | 0.98 | 4,104 | 1.00 (7 C / 0 N / 3 U) | 0.70 |

**Operating point for the app: public, conf ≥ 0.5, `verify_scan` ≥ 0.98, CNN-scored.**
* **Why.** It is the loosest point where both pieces of evidence reach about 0.9 or better: the in-park truth
  share is 0.97 (29 / 30), and the blind review gives 0.92 (0.85–0.98). At `verify_scan` ≥ 0.9 the review falls
  to 0.80 with a wide CI.
* **What it costs.** Low recall: it keeps 5,431 of the 57,401 public candidates at conf ≥ 0.5, and finds 47 of
  the 1,006 official courts OSM lacks.
* **Exported sample.** A second blind sample of **100 random exported candidates**
  (`review/exported_sample`, no overlap with the first) gave **57 C, 2 N, 41 U**.
  * That is precision **0.97 (Wilson 95% CI 0.89–0.99)**.
  * Counting every unsure slab as not a court gives **0.57 (0.47–0.66)**.
  * The two N were sand rectangles (volleyball courts or sand pits).
  * The 41 U are mostly unmarked school blacktops and park slabs. Many show hoop-pole dots but no visible lines
    at 0.6 m.
  * No exported crop was on a home lot. One (AZ, inside an OSM `leisure=park` polygon in a subdivision,
    `p_home` 0.60) looks like an HOA neighbourhood park, which may be residents-only.
* **Combined.** With the 30 crops of the first sample in that stratum: 73 C, 6 N, 51 U, so about 0.92 excluding
  unsure. Treat the exported candidates as "a court or a court-sized public blacktop, rarely anything else".

### Outputs (`out/national/`, git-ignored)

* **`detections.parquet`**: 983,856 merged detections, including near-OSM ones. Columns: id, conf, verify, lon,
  lat, box (Albers `ax0..ay1`, `box_w_m`, `box_h_m`), edge, item_id, state, date, near_known_osm.
* **`merge_stats.json`, `court_best_conf.json`**: merge counts, and OSM-court recall per state.
* **`candidates_us.parquet`**: the 889,817 candidates.
  * Scores: conf, `verify_scan` (in-scan), and from the rescore `verify`, `conf2`, `p_home`.
  * Privacy: privacy, site, privacy_reason, and the `f_*` features.
  * Box: `box_w_m`, `box_h_m`, edge, the Albers box `box_ax0..box_ay1` and its lon/lat bounds
    `box_west/south/east/north`.
  * NAIP: `naip_item`, `naip_date`.
* **`rescore.parquet`**: the 214,197 rescored candidates. Their chips are on the node only. The 67,199 chips of
  public candidates are in `data/national/rescore_chips/`.
* **`privacy/<ST>.parquet`, `osm_privacy/<ST>.json` (+ `_detail`), `privacy_states.json`**: per-state classes
  for candidates and OSM features.
* **`state_summary.{json,csv}`, `scan_throughput.json`**: per-state counts (below), and per-node throughput from
  the done markers.
* **`review/public_strata/`, `review/exported_sample/`**: sheets, crops, `sample.json`, `labels.json` and
  `scores.json`.
* **`truth/`**: `truth_summary.json` (all of the above, snapshot comparison, grids) and one `eval_*.json` per
  configuration.
* **`app_ready/<ST>.geojson`**: 49 files, 84,324 features (78,893 OSM + 5,431 detected). They follow the app
  schema, and detected features add `site`.
  * They validate with `export_app.validate`, and ids are unique across files.
  * `index.json` holds the operating point and the per-state counts. `exported_candidates.parquet` holds the
    exported rows.
  * `app/assets/data/courts.geojson` is unchanged. The national data is meant for a database.
* **`node_logs/`**: the post node's job logs and its watchdog log.
* **Also written**:
  * `data/national/osm_phome.parquet`
  * `data/national/osm_geojson/courts_<ST>.geojson` (the OSM input of `app-ready`)
  * `data/sites/buildings_<ST>.parquet` for all 49 states

### Per-state counts

Candidates are not near an OSM court. Exported means public at the operating point above. OSM recall is on
labelable OSM courts at conf ≥ 0.5. The table is sorted by candidates ≥ 0.5.

| state | NAIP files | km² | candidates ≥ 0.1 | ≥ 0.5 | public ≥ 0.5 | private_home ≥ 0.5 | unknown ≥ 0.5 | exported candidates | OSM courts exported / all | OSM recall @ 0.5 |
|---|---|---|---|---|---|---|---|---|---|---|
| CA | 11,074 | 410,046 | 50,572 | 23,091 | 17,628 | 3,983 | 744 | 2,954 | 14,990 / 18,218 | 0.85 |
| TX | 17,224 | 688,231 | 119,762 | 16,928 | 4,005 | 7,773 | 4,457 | 74 | 3,892 / 5,210 | 0.69 |
| FL | 4,016 | 151,327 | 31,618 | 7,799 | 3,464 | 3,333 | 683 | 394 | 4,707 / 6,102 | 0.82 |
| PA | 3,354 | 117,498 | 33,451 | 6,708 | 1,772 | 3,181 | 1,627 | 100 | 3,760 / 4,392 | 0.82 |
| OH | 3,049 | 107,033 | 41,315 | 6,047 | 1,234 | 3,327 | 1,354 | 39 | 3,433 / 4,383 | 0.67 |
| NY | 3,923 | 127,230 | 26,427 | 5,543 | 1,629 | 2,647 | 1,049 | 112 | 4,607 / 5,600 | 0.77 |
| NC | 3,575 | 128,947 | 33,920 | 5,206 | 1,591 | 2,202 | 1,121 | 80 | 1,311 / 1,821 | 0.72 |
| MI | 4,840 | 151,439 | 31,080 | 4,794 | 1,283 | 2,380 | 955 | 45 | 2,492 / 3,054 | 0.65 |
| GA | 3,909 | 152,784 | 30,064 | 4,748 | 1,114 | 2,200 | 1,188 | 64 | 913 / 1,437 | 0.75 |
| IN | 2,687 | 93,896 | 32,017 | 4,658 | 1,025 | 2,503 | 1,027 | 24 | 1,627 / 2,100 | 0.75 |
| MO | 4,970 | 180,782 | 41,934 | 4,117 | 991 | 1,843 | 1,195 | 11 | 1,017 / 1,245 | 0.68 |
| WI | 4,473 | 145,592 | 30,808 | 3,654 | 1,034 | 1,768 | 755 | 21 | 1,549 / 1,735 | 0.68 |
| NJ | 634 | 20,199 | 11,679 | 3,499 | 1,079 | 1,968 | 364 | 110 | 2,134 / 2,559 | 0.81 |
| IL | 4,126 | 146,126 | 19,411 | 3,463 | 1,194 | 1,669 | 496 | 66 | 2,889 / 3,560 | 0.70 |
| KY | 2,925 | 104,866 | 22,160 | 3,398 | 769 | 1,693 | 848 | 31 | 798 / 980 | 0.75 |
| TN | 2,997 | 109,322 | 21,319 | 3,358 | 993 | 1,438 | 803 | 33 | 622 / 828 | 0.74 |
| VA | 3,032 | 105,120 | 19,030 | 3,281 | 950 | 1,370 | 690 | 49 | 1,984 / 2,720 | 0.72 |
| WA | 5,660 | 175,769 | 22,692 | 3,115 | 556 | 2,063 | 344 | 17 | 2,076 / 3,329 | 0.57 |
| AZ | 7,592 | 295,472 | 10,684 | 3,061 | 1,431 | 1,288 | 174 | 263 | 3,073 / 4,452 | 0.72 |
| AL | 3,482 | 134,087 | 20,902 | 2,801 | 746 | 1,126 | 816 | 22 | 531 / 652 | 0.68 |
| MD | 906 | 26,885 | 10,853 | 2,602 | 786 | 1,324 | 358 | 41 | 1,746 / 2,098 | 0.76 |
| SD | 5,963 | 199,937 | 26,951 | 2,518 | 235 | 458 | 1,817 | 7 | 305 / 387 | 0.69 |
| SC | 2,146 | 80,731 | 13,202 | 2,514 | 859 | 956 | 612 | 37 | 788 / 1,011 | 0.77 |
| MS | 3,234 | 123,722 | 16,051 | 2,270 | 524 | 858 | 787 | 18 | 221 / 301 | 0.71 |
| LA | 3,249 | 122,397 | 12,882 | 2,241 | 787 | 768 | 558 | 52 | 392 / 475 | 0.67 |
| MA | 737 | 21,378 | 7,679 | 2,137 | 518 | 1,328 | 249 | 65 | 1,972 / 2,467 | 0.80 |
| MN | 6,761 | 218,859 | 13,031 | 1,999 | 573 | 1,091 | 303 | 43 | 1,788 / 2,313 | 0.64 |
| UT | 6,021 | 220,086 | 11,990 | 1,972 | 671 | 1,167 | 92 | 33 | 1,014 / 1,530 | 0.56 |
| NE | 5,618 | 200,552 | 16,169 | 1,867 | 445 | 722 | 671 | 1 | 626 / 807 | 0.61 |
| OK | 4,727 | 181,303 | 8,900 | 1,827 | 803 | 592 | 339 | 51 | 590 / 784 | 0.61 |
| CO | 7,390 | 269,810 | 6,881 | 1,621 | 782 | 677 | 111 | 47 | 1,943 / 2,616 | 0.63 |
| NV | 7,486 | 271,878 | 3,932 | 1,435 | 1,037 | 285 | 55 | 304 | 697 / 832 | 0.83 |
| OR | 7,446 | 251,570 | 8,870 | 1,432 | 429 | 785 | 164 | 8 | 1,128 / 1,705 | 0.57 |
| WV | 1,827 | 62,953 | 7,512 | 1,427 | 384 | 703 | 309 | 23 | 315 / 393 | 0.76 |
| AR | 3,595 | 137,999 | 8,154 | 1,410 | 560 | 502 | 299 | 35 | 542 / 639 | 0.62 |
| NM | 8,007 | 315,157 | 8,913 | 1,385 | 516 | 480 | 296 | 11 | 972 / 1,230 | 0.67 |
| KS | 5,856 | 213,296 | 7,194 | 1,376 | 534 | 606 | 170 | 18 | 562 / 794 | 0.61 |
| IA | 4,235 | 145,928 | 8,002 | 1,275 | 446 | 640 | 177 | 21 | 935 / 1,363 | 0.56 |
| ID | 6,544 | 216,733 | 6,753 | 1,099 | 397 | 512 | 151 | 14 | 579 / 706 | 0.65 |
| CT | 411 | 12,988 | 4,476 | 1,059 | 258 | 613 | 144 | 18 | 857 / 1,023 | 0.80 |
| ME | 2,862 | 85,253 | 5,251 | 806 | 224 | 336 | 231 | 13 | 392 / 476 | 0.72 |
| MT | 11,764 | 381,155 | 5,575 | 762 | 310 | 319 | 123 | 5 | 256 / 318 | 0.62 |
| WY | 7,311 | 253,527 | 6,122 | 605 | 248 | 204 | 142 | 4 | 246 / 290 | 0.63 |
| ND | 5,684 | 183,317 | 6,024 | 569 | 120 | 228 | 210 | 4 | 441 / 599 | 0.54 |
| NH | 764 | 24,131 | 2,474 | 544 | 126 | 308 | 98 | 10 | 325 / 486 | 0.73 |
| DE | 176 | 5,251 | 1,959 | 366 | 83 | 209 | 64 | 3 | 208 / 251 | 0.89 |
| RI | 114 | 2,901 | 1,167 | 355 | 112 | 197 | 36 | 24 | 266 / 358 | 0.80 |
| VT | 811 | 24,993 | 1,810 | 338 | 112 | 147 | 75 | 10 | 208 / 255 | 0.86 |
| DC | 11 | 183 | 195 | 60 | 34 | 14 | 5 | 2 | 174 / 214 | 0.84 |
| **all** | **219,198** | **7,800,639** | **889,817** | **159,140** | **57,401** | **66,784** | **29,336** | **5,431** | **78,893 / 101,098** | **0.74** |

**The export is regionally uneven.** CA has 54% of the exported candidates (2,954), while TX has 74 and MO 11.
* The cause is not NAIP resolution. Even on known OSM courts at conf ≥ 0.5, the share with `verify_scan` ≥ 0.98
  is 0.27 in CA, 0.35 in NV and 0.28 in FL, but 0.08 in TX, 0.05 in MO and 0.10 in OH, for 0.3 m and 0.6 m
  imagery alike.
* The verifier is less confident on some states' flights.
* A per-state calibration, such as a threshold set from each state's known courts, would even out recall. It has
  not been checked for precision.

### Commands as run

```bash
# local: missing files (manifests vs done markers), bookkeeping, launch (watchdog installed and self-tested)
bash cloud/launch.sh post gpu_1x_a100_sxm4,gpu_1x_a10 --max-hours 4
HH_NODE=post bash cloud/sync_code.sh && HH_NODE=post bash cloud/start_stage.sh setup
# inputs as tar streams over ssh: weights, courts_us, NAIP catalog, plan; data/scan2/US/results (one gzip -1 stream,
# 45 min: Windows reads 438k small files at ~190/s); sites_us + residential_us (54 s)
HH_NODE=post bash cloud/start_stage.sh job buildings python -m hoop_pipeline.sites buildings --states CONUS --workers 8
HH_NODE=post bash cloud/start_stage.sh job phome bash cloud/national_post.sh phome
HH_NODE=post bash cloud/start_stage.sh job rescan bash cloud/national_post.sh rescan data/scan2/US/rescan_missing.txt
HH_NODE=post bash cloud/start_stage.sh job split python -m hoop_pipeline.national split-sites
HH_NODE=post bash cloud/start_stage.sh job merge bash cloud/national_post.sh merge
HH_NODE=post bash cloud/start_stage.sh job rescore env HH_RESCORE_MIN=0.3 HH_RESCORE_VERIFY=0.5 bash cloud/national_post.sh rescore
HH_NODE=post bash cloud/start_stage.sh job privacy env HH_PRIVACY_MIN=0.1 bash cloud/national_post.sh privacy   # + summarize
python -m hoop_pipeline.national review --name public_strata --classes public --verify-col verify_scan --require-phome \
    --conf-bands 0.3 0.5 0.6 0.7 1.01 --verify-bands 0.5 0.8 0.9 0.98 1.001 --per-stratum 10 --crop-px 160 --thumb-px 320
bash cloud/national_post.sh package data/scan2/US/rescan_missing.txt     # lists; then pulled home with tar -T
bash cloud/terminate.sh post                                              # GET /instances -> 0
# local
python -m hoop_pipeline.national osm-geojson --out data/national/osm_geojson
python -m hoop_pipeline.national score --name public_strata --labels out/national/review/public_strata/labels.json
python -m hoop_pipeline.national app-ready --osm-dir data/national/osm_geojson --min-conf 0.5 --min-verify 0.98 \
    --verify-col verify_scan --require-phome
python -m hoop_pipeline.national review --name exported_sample --classes public --verify-col verify_scan --require-phome \
    --conf-bands 0.5 1.01 --verify-bands 0.98 1.001 --per-stratum 100 --crop-px 160 --thumb-px 320 --seed 7
python -m hoop_pipeline.national truth-eval
```

### Caveats

* **Labels.** Every precision number above comes from model-assistant labels (blind, but not gold) or from a
  truth share over only 30 in-park candidates.
  * The C / N / U split leaves 41% of exported candidates unsure, so "precision" depends on how unmarked slabs
    are counted.
  * Eli's gold set (`tools/label/`) is still the way to settle it.
* **Screen.** Only candidates in the CNN screen (conf ≥ 0.3, `verify_scan` ≥ 0.5) have `p_home`. Below it,
  classes come from the rules alone, so low-confidence home-lot candidates are under-counted as private_home.
* **Point-site leak.** The leak pattern from Tennessee still applies: a home court near an Overture church or
  school point with a larger building nearby. None appeared among the 100 exported crops.
  * HOA parks mapped as plain `leisure=park` count as public.
* **OSM recall is optimistic.** Outside Tennessee, OSM courts were training positives.
* **OSM data is not the latest.** `data/osm_us/courts_us.parquet` is the 2026-10-06 Geofabrik extract from run 1
  (101,098 courts). Run 2 described a 2026-10-08 extract with 101,217.

## Limits (read before trusting a candidate)

* **Painted courts and blacktops are detectable at 0.6 m; single hoops mostly
  aren't.** A hoop and backboard is under 2 m from above and often casts the
  only visible clue, a shadow. Half-courts (~15 x 14 m, about 25 px) work when
  painted, and are hard when they are just a slab of asphalt.
* **Look-alikes:** tennis and pickleball courts (the main confusion), parking
  lots, flat roofs, swimming-pool decks and patios. Hard negatives from OSM
  pitches help, but review is still required.
* **Trees and shadows:** NAIP is flown in leaf-on season, so courts under or
  beside canopy are partly hidden. Tennessee's 2023 flight (2023-04-11) already
  shows trees in leaf in the Chattanooga chips, and building and tree shadows
  darken some courts.
* **Staleness:** NAIP is 1 to 3 years old. New courts are missing, and removed
  courts may still appear.
* **Indoor courts** are invisible from above. That's fine: the app is for
  outdoor courts.
* **Private property:** driveway and backyard hoops are private, and **must not be mapped or
  shown**.
  * Since v2, every candidate and every OSM court goes through `privacy`, and only public sites
    are exported (see [Privacy policy](#privacy-policy-what-the-app-may-show)).
  * The filter is conservative but not perfect. Its inputs are maps (OSM, PAD-US, NCES, Overture),
    building footprints and a CNN, not parcel records.
  * Reviewers should still reject anything on a residential lot, and the app should keep its
    "Not a court" button.
  * One known leak pattern: a court next to a point-only site (an Overture church or school point)
    with houses nearby but a larger building within 100 m is classed public unless the CNN is fairly
    sure (p_home ≥ 0.5). One of the 41 reviewed public courts in Tennessee was such a backyard court.
  * The context CNN learned "home" from 4,636 OSM-tagged private pitches. It has not been checked
    against parcel data.
* **OSM label noise:** some polygons are offset, cover several courts, or mark
  courts that have since been removed. Axis-aligned boxes around diagonal
  courts are loose. Expect a model trained on this data to plateau below what
  clean labels would give.
* **Model quality.** `weights/best.pt` (= `best_v1.pt`) is the nationwide yolo11s from run 1, and
  `weights/best_v2.pt` is v2. They tie on the Tennessee holdout at mAP50 0.76.
  * On every-pixel scan candidates at public sites, a blind visual review found no score threshold
    above about 0.65–0.8 precision. Unmarked paved slabs are the main confusion.
  * The app therefore shows only visually confirmed candidates. Treat every candidate as unverified
    until someone on the ground or a clearer image confirms it.

## Next steps

**GPU steps still pending.** None ran after 2026-10-09; each needs the owner's approval of new spend.
Costs assume A100 nodes at $1.99/h, each with a watchdog:

| step | what it gives | nodes × time | cost |
|---|---|---|---|
| v2 Tennessee fleet scan (the lost run), then `post_scan_tn.sh` | v2's own every-pixel candidates and recall, the benchmark with TensorRT | 3 × ~25 min incl. setup, plus `main` ~40 min (privacy, review crops) | ~$4 |
| rebuild `data/osm_us` (Geofabrik `pbf` + `osm` stages) | the context layer for new datasets or other states' site layers | 1 × ~40 min | ~$1.5 |
| rebuild `data/crops` (`cropcls verifier-data` / `privacy context-data`) | retraining the verifier or the context CNN | 1 × ~20 min, after `data/osm_us` | ~$0.7 |
| ~~Overture buildings for CONUS~~ | done 2026-10-16 (30 min, see [National run](#national-run-2026-10-15)) | | |
| ~~national scan (runbook)~~ | done 2026-10-15/16: 8 nodes, 5.6–15.4 h each | | ~$154 + $2.83 post-processing |

1. **Label the gold set.** Eli labels `tools/label/` (300 tiles). Then run `gold score` for v2,
   and with `--preds data/gold/preds_v1.json` for v1. This gives the first honest CONUS precision
   and recall per stratum. Set the national export threshold from it: per stratum, the lowest
   score with ≥ 90% precision.
2. **National scan.** Done; see [National run](#national-run-2026-10-15). What follows was the plan. Follow the [runbook](#national-scan-runbook) once the gold
   numbers are in. The CONUS plan (`data/scan2/US/plan.parquet`, 31 MB) is now local. Copy it
   to `main` (`hh_scp_up`) before `scan_fleet.sh push`, which reads from there. Expect ~10^5 candidates at 0.5. Review the uncertain bands in the
   labelling page or the app.
3. **Close the loop.**
   * Turn gold labels and app reviews (confirmed / rejected) into positives and hard negatives.
   * Split multi-court OSM polygons and fix shifted ones.
   * Retrain, then rescan only the uncertain items.
4. **Privacy labels.** The context CNN learned "home" only from OSM `access=private` pitches. Eli's
   gold tags (`2` = home lot) and the app's rejected candidates are better labels; retrain it with
   them. Add county parcel land-use codes, where they are open, as a stronger home-lot signal.
5. **Rotated boxes (YOLO-OBB).** Label with the polygon's minimum rotated rectangle, train
   `yolo11s-obb.pt`, and enable `degrees=180` augmentation.
6. **Sharper imagery.** Many counties and cities, possibly Hamilton County and Chattanooga too,
   fly their own leaf-off orthoimagery at 3 to 6 inch (7.5 to 15 cm). Check what the county GIS
   office offers and under what license. At that resolution hoops and backboards become visible.
   Add a reader for the WMS/WMTS or downloaded tiles, and keep `--gsd` consistent with training
   (or retrain at the finer GSD).
7. **Street-level confirmation.** Use Mapillary (CC-BY-SA, free API) images within ~50 m of a
   candidate, plus a hoop/backboard detector, to confirm courts and count hoops. This is also the
   only practical way to find standalone hoops.
8. **NIR / NDVI channel and multi-year voting.** Courts have near-zero NDVI. Scanning 2021 and 2023
   and keeping candidates seen in both removes transient look-alikes such as tarps and parked
   trailers.
9. **Indoor courts and social features** (planned separately). The indoor layer
   (`data/osm_us/indoor_us.parquet`, 1,681 features) is ready to use.

## Files

```
pipeline/
  README.md, requirements.txt, pyproject.toml (ruff config), .gitignore, regions.json
  hoop_pipeline/
    config.py        paths, endpoints, defaults, region loading
    common.py        logging, geodesy helpers, stable ids, atomic JSON writes
    fetch_osm.py     Overpass client + OSM -> GeoJSON
    naip.py          STAC lookup, windowed COG reads, chips + sidecars
    build_dataset.py YOLO dataset builder
    train.py         Ultralytics training wrapper (--copy-to, --set)
    scan.py          tiled inference, merging, known-court filter
    export_app.py    app GeoJSON export + schema validation; privacy-class filter, optional `site`
    osm_tags.py      shared OSM tag rules (basketball, outdoor, court-like, indoor kind, context / site / residential kinds)
    states.py        Census state boundaries, point-in-state
    geoparquet.py    GeoParquet read/write with pyarrow + shapely
    osm_extract.py   nationwide OSM layers from the Geofabrik extract (osmium)
    naip_index.py    NAIP GeoParquet catalog, item choice, grouped/retried COG reads
    build_dataset_us.py  nationwide YOLO dataset, TN held out; v2 negatives, --exclude-labels, --extra-negatives
    evaluate.py      holdout metrics, P/R at thresholds, error montages (--fast: FastDetector, --tta)
    scan_state.py    v1 masked statewide scan: plan / fetch / detect
    fastdet.py       v2 GPU inference: uint8 -> FP16 on GPU, raw FP16 or TensorRT 10, GPU NMS, flip TTA
    scan_engine.py   v2 scan engine: plan / partition / run / merge / status (every pixel, resumable, multi-node)
    label_clean.py   score dataset chips, find label/model disagreements, select the v2 train list
    cropcls.py       ResNet-18 crop classifiers: detection verifier, public-vs-home context model
    sites.py         public-site layer (OSM, PAD-US, NCES, Overture places), residential layer, Overture buildings
    privacy.py       privacy class + site type per candidate / OSM court; OSM-labelled context crops
    gold.py          stratified CONUS gold set: sample / fetch / annotate / export / score
    rescore.py       second opinion on scan candidates from fresh NAIP chips: v2, verifier, context CNN (CPU ok)
    national.py      national post-processing: site slices, OSM p_home, per-state privacy, summary, review, app-ready, truth-eval
    truth.py         official court truth set: refresh / build / osm-compare / evaluate / export-labels / chips
    wkb_curves.py    linearise curve geometries (PAD-US) for shapely
  truth_sources.json registry of official court sources (endpoint, filter, fields, license quote, coverage, precision)
  tools/label/
    index.html       keyboard-driven gold-set labelling page (data/ is git-ignored)
  cloud/
    lambda_api.sh launch.sh sync_code.sh start_stage.sh fetch_results.sh terminate.sh scan_fleet.sh   (local)
    install_watchdog.sh watchdog.py watchdog_github_action.yml.example                                  (local)
    setup_remote.sh run_remote.sh post_train_v2.sh post_scan_tn.sh requirements-cloud.txt             (instance)
    watchdog_install.sh watchdog_instance.py                                                          (instance, root)
    national.sh national_node.sh national_scan.sh national_post.sh pull_results.py                    (national run)
    lastrun.py diag_reads.py bench_reads.py bench_infer.py bench_fastdet.py estimate_paved_fraction.py
    estimate_national.py bench_table.py compare_evals.py
    review_samples.py review_crops.py sample_review.py score_review.py disagree.py gold_review.py
    filter_extra_negatives.py
    last_run.json  (git-ignored: every instance of the current run, times, cost; previous runs archived inside)
  tests/
    test_watchdog.py both watchdogs against a fake Lambda API (python -m unittest discover -s tests)
    test_truth.py    truth filters, Esri geometry conversion, build + evaluate on synthetic data (offline)
  data/ datasets/ runs/ weights/ out/ .ultralytics/ tools/label/data/ remote_v2/   (git-ignored, regenerable)
```
