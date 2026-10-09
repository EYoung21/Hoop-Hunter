"""Hoop Hunter satellite court-detection pipeline.

Steps (each is ``python -m hoop_pipeline.<step> --help``):
fetch_osm -> naip -> build_dataset -> train -> scan -> export_app             (regional, Overpass)
osm_extract -> build_dataset_us -> train -> evaluate -> scan_state -> export_app  (nationwide, cloud/)
"""

import os
from pathlib import Path

# Keep Ultralytics' settings file, fonts and downloads inside pipeline/ instead of
# the user's AppData/~/.config. Must be set before ultralytics is imported, and the
# directory must already exist or Ultralytics falls back to /tmp.
_yolo_dir = Path(os.environ.setdefault("YOLO_CONFIG_DIR", str(Path(__file__).resolve().parent.parent / ".ultralytics")))
try:
    _yolo_dir.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

__version__ = "0.1.0"
