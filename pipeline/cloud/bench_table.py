"""Markdown tables from a scan_engine merge (per-node throughput, totals, known-court recall).

    python cloud/bench_table.py out/scan2_tn/merge_stats.json --price 1.99
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("merge_stats", type=Path)
    ap.add_argument("--price", type=float, default=1.99)
    args = ap.parse_args()
    m = json.loads(args.merge_stats.read_text(encoding="utf-8"))
    ws = sorted(m["workers"], key=lambda w: w["worker"])
    print("| node (worker) | items | tiles | seconds | tiles/s | MB/s | GB read | GPU util | GPU busy | backend |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for w in ws:
        node = w["worker"].split(":")[0]
        print(f"| {node} | {w['items_done']} | {w['tiles']:,} | {w['seconds']:.0f} | {w['tiles_per_s']:,.0f} "
              f"| {w['MB_per_s']:.0f} | {w['MB_received'] / 1000:.0f} | {w['gpu_util_mean']:.0f}% "
              f"| {100 * w['gpu_busy_frac']:.0f}% | {w['backend']} |")
    tiles = sum(w["tiles"] for w in ws)
    gb = sum(w["MB_received"] for w in ws) / 1000
    starts = [dt.datetime.fromisoformat(w["finished_at"]) - dt.timedelta(seconds=w["total_seconds"]) for w in ws]
    ends = [dt.datetime.fromisoformat(w["finished_at"]) for w in ws]
    wall = (max(ends) - min(starts)).total_seconds()
    node_s = sum(w["total_seconds"] for w in ws)
    print()
    print(f"* total: {len(ws)} workers, {tiles:,} tiles, {gb:,.0f} GB in {wall / 60:.1f} min wall "
          f"({tiles / wall:,.0f} tiles/s and {gb * 1000 / wall:,.0f} MB/s aggregate)")
    print(f"* scan node-time {node_s / 3600:.2f} h = ${node_s / 3600 * args.price:.2f} at ${args.price}/h (excluding setup)")
    r = m.get("known_court_recall", {})
    if r:
        print()
        ths = [k for k in next(iter(r.values())).keys() if k != "courts"]
        print("| OSM courts | n | " + " | ".join(f">={t}" for t in ths) + " |")
        print("|---|---|" + "---|" * len(ths))
        for name, row in r.items():
            print(f"| {name} | {row['courts']} | " + " | ".join(f"{row[t]:.3f}" for t in ths) + " |")
    print()
    print("candidates by threshold (not near known OSM):", m.get("candidates_by_threshold"))


if __name__ == "__main__":
    main()
