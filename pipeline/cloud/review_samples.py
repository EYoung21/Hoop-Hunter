"""Random review crops per confidence band from a candidates GeoJSON (run ON the instance).

    PYTHONPATH=. ~/hhvenv/bin/python cloud/review_samples.py out/candidates_tn.geojson --per-band 30

Writes out/review_bands/<band>/ crops and out/review_bands/<band>_sheet_00.jpg contact sheets, so
precision per confidence band can be judged by eye (the top-N crops only show the top band).
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from hoop_pipeline import config
from hoop_pipeline.common import setup_logging
from hoop_pipeline.scan_state import save_crops


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates", type=Path)
    ap.add_argument("--bands", type=float, nargs="+", default=[0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 1.01])
    ap.add_argument("--per-band", type=int, default=30)
    ap.add_argument("--out", type=Path, default=config.OUT_DIR / "review_bands")
    ap.add_argument("--state", default="TN")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--on-site", choices=["any", "yes", "no"], default="any", help="filter on the on_site flag")
    args = ap.parse_args()
    setup_logging(0)
    feats = json.loads(args.candidates.read_text(encoding="utf-8"))["features"]
    if args.on_site != "any":
        want = args.on_site == "yes"
        feats = [f for f in feats if f["properties"].get("on_site") is want]
    rng = random.Random(args.seed)
    ns = argparse.Namespace(naip_dir=config.NAIP_GEOPARQUET_DIR, min_year=config.NAIP_MIN_YEAR, state=args.state,
                            crop_px=256, gsd=config.TARGET_GSD_M)
    for lo, hi in zip(args.bands[:-1], args.bands[1:]):
        band = [f for f in feats if lo <= f["properties"]["confidence"] < hi]
        pick = rng.sample(band, min(args.per_band, len(band)))
        pick.sort(key=lambda f: -f["properties"]["confidence"])
        print(f"band {lo:.2f}-{hi:.2f}: {len(band)} candidates, {len(pick)} sampled", flush=True)
        if pick:
            save_crops(pick, args.out / f"band_{lo:.2f}_{min(hi, 1.0):.2f}", ns)


if __name__ == "__main__":
    main()
