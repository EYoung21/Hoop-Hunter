"""Markdown comparison of evaluate.py outputs (v1 vs v2, optional TTA) for the README.

    python cloud/compare_evals.py --runs "v1=out/eval_tn_v1" "v2=out/eval_tn_v2" "v2+TTA=out/eval_tn_v2_tta" \
        --thresholds 0.3 0.5 0.6 0.7 --recalls 0.6 0.7 0.75 0.8
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def interp_precision_at_recall(rows: list[dict[str, Any]], target: float) -> tuple[float, float] | None:
    """Highest precision among thresholds whose recall reaches ``target`` (and that threshold)."""
    ok = [r for r in rows if r["recall"] >= target]
    if not ok:
        return None
    best = max(ok, key=lambda r: r["precision"])
    return best["precision"], best["conf"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True, help="name=dir (each dir has metrics.json)")
    ap.add_argument("--thresholds", type=float, nargs="+", default=[0.3, 0.5, 0.6, 0.7])
    ap.add_argument("--recalls", type=float, nargs="+", default=[0.6, 0.7, 0.75, 0.8])
    ap.add_argument("--iou", choices=["30", "50"], default="30")
    args = ap.parse_args()
    runs = []
    for spec in args.runs:
        name, _, d = spec.partition("=")
        runs.append((name, json.loads((Path(d) / "metrics.json").read_text(encoding="utf-8"))))
    print("| model | mAP50 | mAP50-95 | " + " | ".join(f"P / R @ {t:.2f}" for t in args.thresholds)
          + " | background chips firing @ 0.5 |")
    print("|---|---|---|" + "---|" * len(args.thresholds) + "---|")
    for name, m in runs:
        u = m.get("ultralytics", {})
        rows = {round(r["conf"], 2): r for r in m[f"pr_iou{args.iou}"]}
        cells = []
        for t in args.thresholds:
            r = rows.get(round(t, 2))
            cells.append(f"{r['precision']:.3f} / {r['recall']:.3f}" if r else "-")
        bg = rows.get(0.5, {}).get("background_chips_firing", float("nan"))
        print(f"| {name} | {u.get('mAP50', float('nan')):.3f} | {u.get('mAP50_95', float('nan')):.3f} | "
              + " | ".join(cells) + f" | {100 * bg:.1f}% |")
    print()
    print(f"Precision at matched recall (IoU 0.{args.iou[0]}, best threshold reaching the recall):")
    print()
    print("| model | " + " | ".join(f"recall >= {r:.2f}" for r in args.recalls) + " |")
    print("|---|" + "---|" * len(args.recalls))
    for name, m in runs:
        cells = []
        for r in args.recalls:
            x = interp_precision_at_recall(m[f"pr_iou{args.iou}"], r)
            cells.append(f"{x[0]:.3f} (t={x[1]:.2f})" if x else "-")
        print(f"| {name} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
