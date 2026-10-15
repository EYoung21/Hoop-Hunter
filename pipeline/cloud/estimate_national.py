"""National scan time and cost from the CONUS plan and a measured benchmark (local or on the instance).

    python cloud/estimate_national.py --plan-stats data/scan2/US/plan_stats.json \
        --workers out/scan2_tn/workers.json --price 1.99 --setup-min 15

``--workers`` is merge_stats.json's ``workers`` list (one summary per scan_engine run process) or a JSON list of
them. Per-node throughput is the median steady rate of the benchmark nodes (tiles / wall seconds). The tile count
comes from the plan; bytes scale with the measured MB per tile for 0.6 m states and with the plan's estimate for
0.3 m states (read from 2x overviews, ~1.3x the bytes per km^2).
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-stats", type=Path, required=True)
    ap.add_argument("--workers", type=Path, required=True)
    ap.add_argument("--price", type=float, default=1.99, help="$/h per single-GPU node")
    ap.add_argument("--setup-min", type=float, default=15.0, help="per-node launch + setup + push overhead")
    ap.add_argument("--nodes", type=int, nargs="+", default=[1, 4, 8])
    ap.add_argument("--rate", type=float, nargs="*", default=[],
                    help="extra per-node tiles/s scenarios (e.g. a TensorRT or read-bound rate)")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    plan = json.loads(args.plan_stats.read_text(encoding="utf-8"))
    w = json.loads(args.workers.read_text(encoding="utf-8"))
    workers = w["workers"] if isinstance(w, dict) else w
    rates = [x["tiles"] / x["seconds"] for x in workers if x.get("seconds")]
    mbps = [x["MB_per_s"] for x in workers if x.get("MB_per_s")]
    mb_per_tile = sum(x["MB_received"] for x in workers) / max(1, sum(x["tiles"] for x in workers))
    tiles = plan["tiles"]
    est_gb = plan.get("est_read_gb_at_measured_MB_per_km2")
    scenarios = {"measured": statistics.median(rates)} if rates else {}
    for r in args.rate:
        scenarios[f"{r:.0f} tiles/s"] = r
    out: dict[str, object] = {"conus_tiles": tiles, "conus_items": plan["items"], "conus_zone_km2": plan["zone_km2"],
                              "est_read_tb": round(est_gb / 1000, 1) if est_gb else None,
                              "est_read_tb_at_measured_MB_per_tile": round(tiles * mb_per_tile / 1e6, 1),
                              "benchmark_nodes": len(rates), "per_node_tiles_per_s": [round(r, 1) for r in rates],
                              "per_node_MB_per_s": mbps, "MB_per_tile_measured": round(mb_per_tile, 4),
                              "price_per_node_hour": args.price, "setup_min_per_node": args.setup_min, "scenarios": {}}
    for name, rate in scenarios.items():
        rows = {}
        for n in args.nodes:
            hours = tiles / (rate * n) / 3600
            node_hours = n * (hours + args.setup_min / 60)
            rows[f"{n} node(s)"] = {"wall_h": round(hours + args.setup_min / 60, 1), "node_hours": round(node_hours, 1),
                                    "cost_usd": round(node_hours * args.price, 0),
                                    "aggregate_MB_per_s": round(rate * n * mb_per_tile, 0)}
        out["scenarios"][name] = {"per_node_tiles_per_s": round(rate, 1), **rows}  # type: ignore[index]
    text = json.dumps(out, indent=1)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
