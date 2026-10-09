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
| USDA Cropland Data Layer 2021 via Planetary Computer (`usda-cdl`) | only for the planning estimate of a paved-area scan mask (`cloud/estimate_paved_fraction.py`) | **Public domain** (US Government work). |
| NYC Parks basketball courts (`../assets/NYCcourts.json`) | `source: "nyc_parks"` entries | NYC Open Data, free to use under the NYC Open Data terms of use. |
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
never printed, never written to the repo and never copied to the instance.

| script | runs | purpose |
|---|---|---|
| `launch.sh [types]` | local | checks capacity (`GET /instance-types`), launches **one** instance with the registered `hoophunter-claude` SSH key, waits for `active`, writes `cloud/last_run.json` (id, type, region, price, times, running cost). Refuses to launch while `last_run.json` records a live instance. |
| `sync_code.sh` | local | tars the package, scripts and the Census zip to `~/hh/pipeline` |
| `start_stage.sh STAGE [args]` | local | starts a `run_remote.sh` stage fully detached (`setsid nohup`); `start_stage.sh status` shows every stage's markers and log tail |
| `setup_remote.sh` | instance | apt `osmium-tool`; venv with `--system-site-packages` (reuses Lambda Stack's CUDA torch); `requirements-cloud.txt`; GPU and numpy/torch interop check |
| `run_remote.sh STAGE` | instance | `setup`, `pbf`, `osm`, `naip`, `dataset`, `train`, `scan_plan`, `scan_fetch`, `eval`, `scan_detect`, `post_train` (eval + scan_detect + a 640 px re-tiled scan + `bench_infer.py`), `package`; logs and `.start/.done/.failed` markers in `~/hh/logs` |
| `fetch_results.sh` | local | packages results on the instance and unpacks them into `weights/`, `out/`, `data/` (chips and imagery cache stay remote) |
| `terminate.sh [id]` | local | terminates, waits until it is gone, records the cost, lists anything still on the account |
| `lastrun.py` | local | bookkeeping for `last_run.json` (also picks the instance type) |
| `diag_reads.py`, `bench_reads.py`, `bench_infer.py` | instance | signing / open / read concurrency check; dense NAIP read benchmark; inference benchmark |
| `estimate_paved_fraction.py` | local | samples USDA CDL to estimate how much of CONUS a paved-area mask covers |
| `review_samples.py` | instance | random review crops per confidence band (optionally on-site / off-site only) |

A full session:

```bash
cd pipeline
bash cloud/launch.sh                       # gpu_1x_a100_sxm4, else gpu_1x_a10, else gpu_1x_h100_sxm5
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
* **Private property:** driveway and backyard hoops are private, and **must
  not be mapped or shown**. The detector is trained on public `leisure=pitch`
  courts. The scan doesn't target hoops, but it can still flag backyard
  slabs. Reviewers should reject anything on a residential lot.
* **OSM label noise:** some polygons are offset, cover several courts, or mark
  courts that have since been removed. Axis-aligned boxes around diagonal
  courts are loose. Expect a model trained on this data to plateau below what
  clean labels would give.
* **Model quality.** `weights/best.pt` is now the nationwide yolo11s (TN
  holdout mAP50 0.79). The small-scale smoke model it replaced scored mAP50
  ≈ 0.002. Even at 0.65, roughly 1 in 4 statewide candidates is not a court,
  and some are private courts. Treat every candidate as unverified.

## Next steps

1. **Paved-mask CONUS scan** with the design above (NLCD impervious mask at
   COG-block granularity, several single-GPU nodes, GPU-side preprocessing,
   per-item resumable outputs). Calibrate thresholds per region on a labelled
   sample first.
2. **Cleaner labels.** OSM noise (pools, pickleball blocks or parking lots
   tagged basketball; one polygon over several courts) caps the metrics.
   Drop labels whose chips the current model strongly disagrees with, split
   multi-court polygons, and add confirmed app reviews.
3. **Rotated boxes (YOLO-OBB).** Label with the polygon's minimum rotated
   rectangle, train `yolo11n-obb.pt`, and enable `degrees=180` augmentation.
4. **Sharper imagery.** Many counties and cities, possibly Hamilton County
   and Chattanooga too, fly their own leaf-off orthoimagery at 3 to 6 inch
   (7.5 to 15 cm). Check what the county GIS office offers and under what
   license. At that resolution hoops and backboards become visible. Add a
   reader for the WMS/WMTS or downloaded tiles, and keep `--gsd` consistent
   with training (or retrain at the finer GSD).
5. **Street-level confirmation.** Use Mapillary (CC-BY-SA, free API) images
   within ~50 m of a candidate, plus a hoop/backboard detector, to confirm
   courts and count hoops. This is also the only practical way to find
   standalone hoops.
6. **Active learning from app reviews.** The app already records
   `confirmed`/`rejected` decisions locally. Upload them (a small backend or
   export) to:
   * turn confirmed candidates into new positives and rejected ones into
     hard negatives
   * retrain
   * rescan

   Prioritise candidates near the decision boundary (confidence 0.3 to 0.6)
   for review.
7. **NIR / NDVI channel.** Courts have near-zero NDVI, while grass and trees
   are high. Feeding RGB plus NDVI (or swapping one channel) can cut
   false positives in parks.
8. **Multi-year voting.** Scan 2021 and 2023 (`--year`) and keep candidates
   seen in both. That removes transient look-alikes such as tarps and
   parked trailers.

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
    train.py         Ultralytics training wrapper
    scan.py          tiled inference, merging, known-court filter
    export_app.py    app GeoJSON export + schema validation
    osm_tags.py      shared OSM tag rules (basketball, outdoor, court-like, indoor kind, context kinds)
    states.py        Census state boundaries, point-in-state
    geoparquet.py    GeoParquet read/write with pyarrow + shapely
    osm_extract.py   nationwide OSM layers from the Geofabrik extract (osmium)
    naip_index.py    NAIP GeoParquet catalog, item choice, grouped/retried COG reads
    build_dataset_us.py  nationwide YOLO dataset, TN held out
    evaluate.py      holdout metrics, P/R at thresholds, error montages
    scan_state.py    masked statewide scan: plan / fetch / detect
  cloud/
    lambda_api.sh launch.sh sync_code.sh start_stage.sh fetch_results.sh terminate.sh   (local)
    setup_remote.sh run_remote.sh requirements-cloud.txt                                (instance)
    lastrun.py diag_reads.py bench_reads.py bench_infer.py estimate_paved_fraction.py review_samples.py
    last_run.json  (git-ignored: instance id, times, cost of the last run)
  data/ datasets/ runs/ weights/ out/ .ultralytics/   (git-ignored, regenerable)
```
