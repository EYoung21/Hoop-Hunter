"""Stratified review sample of scan candidates for the provisional (model-assistant) precision estimate.

Run ON the instance from ~/hh/pipeline after scan_engine merge + privacy classify:
    PYTHONPATH=. ~/hhvenv/bin/python cloud/sample_review.py out/scan2_tn/detections_privacy.parquet \
        --out out/review_v2/sample.geojson --per-band 34 --public-share 0.7

Only candidates not near a known OSM court, and only in *evaluation* cells: the 0.05-degree cells whose
hash put them outside the cells used to mine Tennessee hard negatives for v2 training (see
README "Hard negatives"), so the sample measures v2 on locations it never trained on.

Strata are (confidence band, public or not): ``--public-share`` of each band's sample comes from candidates
classed ``public`` (what the app exports), the rest from the other classes (to check the privacy classifier).
``<out>.strata.json`` records every stratum's population so ``score_review.py`` can weight the estimates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from pathlib import Path

import pyarrow.parquet as pq


def mining_cell(lat: float, lon: float, cell: float = 0.05) -> bool:
    key = f"tnfp:{math.floor(lat / cell)}:{math.floor(lon / cell)}"
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < 0.5


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--bands", type=float, nargs="+", default=[0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.01])
    ap.add_argument("--per-band", type=int, default=34)
    ap.add_argument("--public-share", type=float, default=0.7, help="share of each band's sample from public candidates")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    d = pq.read_table(args.candidates).to_pylist()
    pool = [r for r in d if not r.get("near_known_osm") and not mining_cell(r["lat"], r["lon"])]
    rng = random.Random(args.seed)
    feats = []
    strata: dict[str, dict[str, int]] = {}
    for lo, hi in zip(args.bands[:-1], args.bands[1:]):
        band = f"{lo:.2f}-{hi:.2f}"
        rows = [r for r in pool if lo <= r["conf"] < hi]
        pub = [r for r in rows if r.get("privacy") == "public"]
        oth = [r for r in rows if r.get("privacy") != "public"]
        n_pub = min(len(pub), round(args.per_band * args.public_share))
        n_oth = min(len(oth), args.per_band - n_pub)
        n_pub = min(len(pub), args.per_band - n_oth)  # top up from public when a band has few others
        pick_pub, pick_oth = rng.sample(pub, n_pub), rng.sample(oth, n_oth)
        strata[f"{band}|public"] = {"population": len(pub), "sampled": n_pub}
        strata[f"{band}|other"] = {"population": len(oth), "sampled": n_oth}
        print(f"band {band}: {len(rows)} eligible ({len(pub)} public), sampled {n_pub} public + {n_oth} other")
        for r in sorted(pick_pub + pick_oth, key=lambda r: -r["conf"]):
            props = {k: (v if not isinstance(v, float) or v == v else None) for k, v in r.items()}
            props.update({"confidence": r["conf"], "box_m": [r["box_w_m"], r["box_h_m"]], "band": band,
                          "stratum": f"{band}|{'public' if r.get('privacy') == 'public' else 'other'}"})
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                          "properties": props})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    meta = {"candidates": str(args.candidates), "eligible": len(pool), "bands": args.bands, "per_band": args.per_band,
            "public_share": args.public_share, "seed": args.seed, "strata": strata,
            "note": "eligible = not near a known OSM court and in an evaluation cell (not a TN hard-negative mining cell)"}
    args.out.with_suffix(".strata.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"wrote {len(feats)} to {args.out}")


if __name__ == "__main__":
    main()
