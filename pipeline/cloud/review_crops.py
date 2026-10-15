"""Review crops (256 px, box drawn, read straight from NAIP) for every feature of a candidates GeoJSON.

Run ON the instance from ~/hh/pipeline:
    PYTHONPATH=. ~/hhvenv/bin/python cloud/review_crops.py out/review_v1fp/mid_sample.geojson --out out/review_v1fp/mid_crops
Features need ``confidence`` and ``box_m`` properties (scan_state / scan_engine outputs have them).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hoop_pipeline import config
from hoop_pipeline.common import setup_logging
from hoop_pipeline.scan_state import save_crops


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--state", default="TN")
    ap.add_argument("--crop-px", type=int, default=256)
    args = ap.parse_args()
    setup_logging(0)
    feats = json.loads(args.candidates.read_text(encoding="utf-8"))["features"]
    ns = argparse.Namespace(naip_dir=config.NAIP_GEOPARQUET_DIR, min_year=config.NAIP_MIN_YEAR, state=args.state,
                            crop_px=args.crop_px, gsd=config.TARGET_GSD_M)
    for f in feats:
        f["properties"].setdefault("mask_kinds", [])
    save_crops(feats, args.out, ns)


if __name__ == "__main__":
    main()
