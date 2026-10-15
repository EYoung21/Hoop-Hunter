"""Drop extra-negative points that fall near held-out chips (TN val / val_us) of a dataset manifest.

    PYTHONPATH=. ~/hhvenv/bin/python cloud/filter_extra_negatives.py out/review_v1fp/tn_fp_negatives.csv \
        --manifest datasets/us2/manifest.csv.gz --out out/review_v1fp/tn_fp_negatives_ok.csv --min-dist-m 300
"""

from __future__ import annotations

import argparse
import csv
import gzip
from pathlib import Path

import numpy as np

M_PER_DEG = 111_320.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", type=Path)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--min-dist-m", type=float, default=300.0)
    args = ap.parse_args()
    held = []
    with gzip.open(args.manifest, "rt", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["split"] in ("val", "val_us"):
                held.append((float(r["lat"]), float(r["lon"])))
    h = np.array(held)
    rows = list(csv.DictReader(open(args.csv, encoding="utf-8", newline="")))
    keep = []
    for r in rows:
        la, lo = float(r["lat"]), float(r["lon"])
        d = np.hypot((h[:, 0] - la) * M_PER_DEG, (h[:, 1] - lo) * M_PER_DEG * np.cos(np.radians(la)))
        if d.min() >= args.min_dist_m:
            keep.append(r)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(keep)
    print(f"kept {len(keep)} of {len(rows)} extra negatives (>= {args.min_dist_m} m from {len(h)} held-out chips)")


if __name__ == "__main__":
    main()
