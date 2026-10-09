"""Shared configuration: paths, regions, network endpoints and model defaults.

Regions live in ``pipeline/regions.json`` so new areas can be added without
touching code. Anything here can be overridden from the CLI of each step.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- paths
PIPELINE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PIPELINE_ROOT.parent

DATA_DIR = PIPELINE_ROOT / "data"            # caches (OSM GeoJSON, NAIP index, chips)
OSM_DIR = DATA_DIR / "osm"
NAIP_INDEX_DIR = DATA_DIR / "naip_index"
CHIPS_DIR = DATA_DIR / "chips"
DATASETS_DIR = PIPELINE_ROOT / "datasets"    # YOLO datasets
RUNS_DIR = PIPELINE_ROOT / "runs"            # Ultralytics training runs
WEIGHTS_DIR = PIPELINE_ROOT / "weights"      # exported/best model weights
OUT_DIR = PIPELINE_ROOT / "out"              # scan results

REGIONS_FILE = Path(os.environ.get("HOOP_REGIONS_FILE", PIPELINE_ROOT / "regions.json"))
NYC_COURTS_FILE = REPO_ROOT / "assets" / "NYCcourts.json"
APP_COURTS_FILE = REPO_ROOT / "app" / "assets" / "data" / "courts.geojson"
CANDIDATES_FILE = OUT_DIR / "candidates.geojson"
DEFAULT_WEIGHTS = WEIGHTS_DIR / "best.pt"

# Nationwide (cloud) workflow: see osm_extract, naip_index, build_dataset_us, scan_state
OSM_US_DIR = DATA_DIR / "osm_us"                 # Geofabrik extract + derived GeoParquet layers
CENSUS_DIR = DATA_DIR / "census"
STATES_FILE = CENSUS_DIR / "cb_2023_us_state_500k.zip"
NAIP_GEOPARQUET_DIR = NAIP_INDEX_DIR / "geoparquet"
SCAN_DIR = DATA_DIR / "scan"                     # statewide scan plans + cached imagery blocks

# --------------------------------------------------------------------------- network
USER_AGENT = "HoopHunter-pipeline/0.1 (+https://github.com/EYoung21/Hoop-Hunter)"

# Tried in order; on failure we back off and move to the next one.
OVERPASS_ENDPOINTS: list[str] = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_TIMEOUT_S = 90          # server-side [timeout:] and client read timeout base
OVERPASS_SLEEP_S = 2.0           # politeness pause between consecutive requests
OVERPASS_MAX_TILE_DEG = 0.25     # large bboxes are split into tiles at most this big
OVERPASS_MAX_RETRIES = 5

STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
NAIP_COLLECTION = "naip"
NAIP_MIN_YEAR = 2016             # ignore older acquisitions when picking "most recent"
PC_SAS_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{account}/{container}"

GEOFABRIK_US_URL = "https://download.geofabrik.de/north-america/us-latest.osm.pbf"
CENSUS_STATES_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_500k.zip"

# --------------------------------------------------------------------------- imagery / model
TARGET_GSD_M = 0.6               # every chip is resampled to this ground sample distance
CHIP_SIZE_PX = 320               # 320 px * 0.6 m = 192 m on a side
SCAN_OVERLAP_PX = 96             # > a full court (~48 px) so every court is whole in some tile
KNOWN_COURT_RADIUS_M = 40.0      # detections this close to a known OSM court are dropped
DEFAULT_MODEL = "yolo11n.pt"     # small COCO-pretrained detector; yolov8n.pt also works
CLASS_NAMES = ["court"]


@dataclass(frozen=True)
class Region:
    name: str
    south: float
    west: float
    north: float
    east: float
    role: str = "target"
    label: str = ""
    app_region: str = ""  # region label written to the app file; defaults to name

    @property
    def app_label(self) -> str:
        return self.app_region or self.name

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """(south, west, north, east)"""
        return (self.south, self.west, self.north, self.east)


def _load_regions_file(path: Path = REGIONS_FILE) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def all_regions(path: Path = REGIONS_FILE) -> dict[str, Region]:
    raw = _load_regions_file(path)
    out: dict[str, Region] = {}
    for name, spec in raw["regions"].items():
        s, w, n, e = (float(v) for v in spec["bbox"])
        if not (s < n and w < e):
            raise ValueError(f"region {name}: bbox must be [south, west, north, east], got {spec['bbox']}")
        out[name] = Region(name, s, w, n, e, spec.get("role", "target"), spec.get("label", ""), spec.get("app_region", ""))
    return out


def resolve_regions(names: list[str], path: Path = REGIONS_FILE) -> list[Region]:
    """Expand region names and group names (e.g. ``training``) into Region objects."""
    raw = _load_regions_file(path)
    regions = all_regions(path)
    groups: dict[str, list[str]] = raw.get("groups", {})
    seen: dict[str, Region] = {}
    for name in names:
        for part in name.split(","):
            part = part.strip()
            if not part:
                continue
            if part in groups:
                for member in groups[part]:
                    seen.setdefault(member, regions[member])
            elif part in regions:
                seen.setdefault(part, regions[part])
            else:
                known = sorted(regions) + sorted(f"{g} (group)" for g in groups)
                raise SystemExit(f"Unknown region or group '{part}'. Known: {', '.join(known)}")
    return list(seen.values())


def bbox_region(bbox: list[float] | tuple[float, ...], name: str | None = None) -> Region:
    s, w, n, e = (float(v) for v in bbox)
    if not (s < n and w < e):
        raise SystemExit("--bbox must be SOUTH WEST NORTH EAST")
    if name is None:
        name = f"bbox_{s:.4f}_{w:.4f}_{n:.4f}_{e:.4f}".replace("-", "m")
    return Region(name, s, w, n, e, "custom", "custom bbox")


def init_ultralytics() -> None:
    """Point Ultralytics at pipeline-local dirs and turn off its analytics sync."""
    from ultralytics import settings

    settings.update({
        "sync": False,
        "datasets_dir": str(DATASETS_DIR),
        "runs_dir": str(RUNS_DIR),
        "weights_dir": str(WEIGHTS_DIR),
    })


def osm_path(region_name: str) -> Path:
    return OSM_DIR / f"{region_name}.geojson"


def osm_negatives_path(region_name: str) -> Path:
    return OSM_DIR / f"{region_name}.negatives.geojson"
