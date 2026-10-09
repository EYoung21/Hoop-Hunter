"""Bookkeeping for cloud runs: pipeline/cloud/last_run.json (instance id, times, cost).

Used by the shell scripts in this folder; never sees the API key.

    python lastrun.py set key=value ...        # 'now' becomes a UTC timestamp
    python lastrun.py get key
    python lastrun.py cost                     # update hours / estimated cost, print summary
    python lastrun.py pick --types a,b --regions r1,r2 < instance-types.json
    python lastrun.py field data.status < instance.json      # dotted lookup on stdin JSON
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

LAST_RUN = Path(__file__).resolve().parent / "last_run.json"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load() -> dict[str, Any]:
    if LAST_RUN.exists():
        return json.loads(LAST_RUN.read_text(encoding="utf-8"))
    return {}


def save(d: dict[str, Any]) -> None:
    tmp = LAST_RUN.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    tmp.replace(LAST_RUN)


def update_cost(d: dict[str, Any]) -> dict[str, Any]:
    if not d.get("launched_at") or not d.get("price_per_hour"):
        return d
    start = dt.datetime.fromisoformat(d["launched_at"])
    end = dt.datetime.fromisoformat(d["terminated_at"]) if d.get("terminated_at") else dt.datetime.now(dt.timezone.utc)
    hours = (end - start).total_seconds() / 3600
    d["hours"] = round(hours, 3)
    d["estimated_cost_usd"] = round(hours * float(d["price_per_hour"]), 2)
    d["cost_updated_at"] = now()
    return d


def dotted(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("pairs", nargs="+")
    g = sub.add_parser("get")
    g.add_argument("key")
    sub.add_parser("cost")
    p = sub.add_parser("pick")
    p.add_argument("--types", required=True)
    p.add_argument("--regions", default="")
    f = sub.add_parser("field")
    f.add_argument("path")
    args = ap.parse_args()

    if args.cmd == "set":
        d = load()
        for pair in args.pairs:
            k, _, v = pair.partition("=")
            if v == "now":
                v = now()
            try:
                d[k] = json.loads(v)
            except ValueError:
                d[k] = v
        save(update_cost(d))
    elif args.cmd == "get":
        v = load().get(args.key)
        print("" if v is None else v)
    elif args.cmd == "cost":
        d = update_cost(load())
        save(d)
        print(json.dumps({k: d.get(k) for k in ("instance_id", "instance_type", "region", "status", "hours",
                                                "estimated_cost_usd")}))
    elif args.cmd == "pick":
        data = json.load(sys.stdin)["data"]
        regions = [r for r in args.regions.split(",") if r]
        for t in args.types.split(","):
            v = data.get(t)
            if not v:
                continue
            avail = [r["name"] for r in v.get("regions_with_capacity_available", [])]
            if not avail:
                continue
            ordered = [r for r in regions if r in avail] + [r for r in avail if r not in regions]
            print(t, ordered[0], v["instance_type"]["price_cents_per_hour"] / 100)
            return
        sys.exit("no capacity for any of: " + args.types)
    elif args.cmd == "field":
        v = dotted(json.load(sys.stdin), args.path)
        print("" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else v))


if __name__ == "__main__":
    main()
