"""Bookkeeping for cloud runs: pipeline/cloud/last_run.json (every instance of the current run).

Used by the shell scripts in this folder; never sees the API key.

The file holds one record per instance, keyed by a short node name ("main", "scan2", ...):

    {"run": "v2", "budget": {"usd": 60, "instance_hours": 16},
     "instances": [{"name": "main", "instance_id": "...", "instance_type": "gpu_1x_a100_sxm4",
                    "region": "us-east-1", "price_per_hour": 1.99, "launched_at": "...",
                    "active_at": "...", "terminated_at": "...", "status": "terminated",
                    "ip": "...", "hours": 1.234, "estimated_cost_usd": 2.46}, ...],
     "totals": {"hours": ..., "estimated_cost_usd": ...},
     "previous_runs": [...]}

Commands:

    python lastrun.py new-run NAME [--usd 60 --hours 16]   # archive the current run, start a new one
    python lastrun.py add NAME key=value ...                # new instance record ('now' -> UTC timestamp)
    python lastrun.py set NAME key=value ...
    python lastrun.py get NAME key                          # NAME may also be an instance id
    python lastrun.py live                                  # "name id ip" of every instance not terminated
    python lastrun.py cost                                  # update hours / cost, print totals and budget
    python lastrun.py pick --types a,b --regions r1,r2 < instance-types.json
    python lastrun.py field data.status < instance.json     # dotted lookup on stdin JSON
    python lastrun.py deadline --max-hours 6                # "<UTC ISO deadline> <name suffix xYYMMDDHHMM>"
    python lastrun.py watchdog-config NAME [--idle-min 30]  # JSON config for the on-instance watchdog
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

LAST_RUN = Path(__file__).resolve().parent / "last_run.json"
LOCK_DIR = LAST_RUN.with_suffix(".lock")


@contextlib.contextmanager
def locked(timeout_s: float = 60.0) -> Iterator[None]:
    """Exclusive lock around read-modify-write of last_run.json (several nodes launch in parallel).
    mkdir is atomic on Windows and Linux; a lock older than 5 minutes is considered stale."""
    t0 = time.monotonic()
    while True:
        try:
            os.mkdir(LOCK_DIR)
            break
        except FileExistsError:
            try:
                if time.time() - LOCK_DIR.stat().st_mtime > 300:
                    os.rmdir(LOCK_DIR)
                    continue
            except OSError:
                pass
            if time.monotonic() - t0 > timeout_s:
                raise SystemExit(f"could not lock {LOCK_DIR}") from None
            time.sleep(0.1)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(LOCK_DIR)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load() -> dict[str, Any]:
    if not LAST_RUN.exists():
        return {"run": "", "instances": []}
    d = json.loads(LAST_RUN.read_text(encoding="utf-8"))
    if "instances" not in d:  # v1 file: a single instance at the top level
        d = {"run": "v1", "instances": [{"name": d.get("name") or "main", **d}] if d.get("instance_id") else []}
    return d


def save(d: dict[str, Any]) -> None:
    tmp = LAST_RUN.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    tmp.replace(LAST_RUN)


def find(d: dict[str, Any], key: str) -> dict[str, Any] | None:
    for inst in d["instances"]:
        if inst.get("name") == key or inst.get("instance_id") == key:
            return inst
    return None


def update_cost(d: dict[str, Any]) -> dict[str, Any]:
    tot_h = tot_c = 0.0
    for inst in d["instances"]:
        if not inst.get("launched_at") or not inst.get("price_per_hour"):
            continue
        start = dt.datetime.fromisoformat(inst["launched_at"])
        end = dt.datetime.fromisoformat(inst["terminated_at"]) if inst.get("terminated_at") \
            else dt.datetime.now(dt.timezone.utc)
        hours = max(0.0, (end - start).total_seconds() / 3600)
        inst["hours"] = round(hours, 3)
        inst["estimated_cost_usd"] = round(hours * float(inst["price_per_hour"]), 2)
        tot_h += hours
        tot_c += hours * float(inst["price_per_hour"])
    d["totals"] = {"hours": round(tot_h, 3), "estimated_cost_usd": round(tot_c, 2), "updated_at": now()}
    return d


def dotted(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)] if part.isdigit() and int(part) < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def deadline_after(hours: float, start: dt.datetime | None = None) -> tuple[str, str]:
    """(ISO UTC deadline, instance-name suffix "xYYMMDDHHMM") ``hours`` after ``start`` (default now), rounded
    down to the minute. cloud/watchdog.py parses the suffix back, so the deadline travels with the instance."""
    t = (start or dt.datetime.now(dt.timezone.utc)) + dt.timedelta(hours=hours)
    t = t.replace(second=0, microsecond=0)
    return t.isoformat(timespec="seconds"), "x" + t.strftime("%y%m%d%H%M")


def parse_pairs(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        k, _, v = pair.partition("=")
        if v == "now":
            out[k] = now()
            continue
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def main() -> None:
    # The shell scripts read this output with $(...) / read, which keep a '\r': on Windows Python would write
    # CRLF and put a raw CR into instance names (Lambda: "Invalid JSON request data"), ids and IPs.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(newline="\n")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new-run")
    n.add_argument("name")
    n.add_argument("--usd", type=float, default=60.0)
    n.add_argument("--hours", type=float, default=16.0)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("pairs", nargs="*")
    s = sub.add_parser("set")
    s.add_argument("name")
    s.add_argument("pairs", nargs="+")
    g = sub.add_parser("get")
    g.add_argument("name")
    g.add_argument("key")
    sub.add_parser("live")
    sub.add_parser("cost")
    p = sub.add_parser("pick")
    p.add_argument("--types", required=True)
    p.add_argument("--regions", default="")
    f = sub.add_parser("field")
    f.add_argument("path")
    dl = sub.add_parser("deadline")
    dl.add_argument("--max-hours", type=float, required=True)
    w = sub.add_parser("watchdog-config")
    w.add_argument("name")
    w.add_argument("--idle-min", type=float, default=30.0)
    w.add_argument("--api", default="https://cloud.lambda.ai/api/v1")
    args = ap.parse_args()
    if args.cmd in ("new-run", "add", "set", "cost"):
        with locked():
            run(args)
    else:
        run(args)


def run(args: argparse.Namespace) -> None:
    if args.cmd == "new-run":
        d = update_cost(load())
        live = [i for i in d["instances"] if i.get("status") != "terminated"]
        if live:
            sys.exit(f"refusing: instances still live: {[i.get('name') for i in live]}")
        prev = d.pop("previous_runs", [])
        if d.get("instances"):
            prev.append(d)
        save({"run": args.name, "started_at": now(), "budget": {"usd": args.usd, "instance_hours": args.hours},
              "instances": [], "previous_runs": prev})
    elif args.cmd == "add":
        d = load()
        if find(d, args.name) is not None and find(d, args.name).get("status") != "terminated":  # type: ignore[union-attr]
            sys.exit(f"instance {args.name} already recorded and not terminated")
        if find(d, args.name) is not None:  # reuse of a name after termination: keep the old record
            find(d, args.name)["name"] = f"{args.name}-old-{len(d['instances'])}"  # type: ignore[index]
        d["instances"].append({"name": args.name, **parse_pairs(args.pairs)})
        save(update_cost(d))
    elif args.cmd == "set":
        d = load()
        inst = find(d, args.name)
        if inst is None:
            sys.exit(f"no instance {args.name} in {LAST_RUN.name}")
        inst.update(parse_pairs(args.pairs))
        save(update_cost(d))
    elif args.cmd == "get":
        inst = find(load(), args.name)
        v = None if inst is None else inst.get(args.key)
        print("" if v is None else v)
    elif args.cmd == "live":
        for inst in load()["instances"]:
            if inst.get("status") != "terminated":
                print(inst.get("name"), inst.get("instance_id"), inst.get("ip") or "-")
    elif args.cmd == "cost":
        d = update_cost(load())
        save(d)
        for inst in d["instances"]:
            print(json.dumps({k: inst.get(k) for k in ("name", "instance_id", "instance_type", "region", "status",
                                                        "hours", "estimated_cost_usd")}))
        b = d.get("budget") or {}
        t = d["totals"]
        print(json.dumps({"totals": t, "budget": b,
                          "over_budget": bool(b) and (t["hours"] > b.get("instance_hours", 1e9)
                                                      or t["estimated_cost_usd"] > b.get("usd", 1e9))}))
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
            ordered = [r for r in regions if r in avail] + ([] if regions else avail)
            if not ordered:
                continue
            print(t, ordered[0], v["instance_type"]["price_cents_per_hour"] / 100)
            return
        sys.exit("no capacity for any of: " + args.types + (f" in {args.regions}" if args.regions else ""))
    elif args.cmd == "field":
        v = dotted(json.load(sys.stdin), args.path)
        print("" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else v))
    elif args.cmd == "deadline":
        if not 0 < args.max_hours <= 168:
            sys.exit("--max-hours must be in (0, 168]")
        print(*deadline_after(args.max_hours))
    elif args.cmd == "watchdog-config":
        inst = find(load(), args.name)
        if inst is None or not inst.get("instance_id") or not inst.get("deadline"):
            sys.exit(f"{args.name}: need instance_id and deadline in {LAST_RUN.name} (launch.sh records both)")
        print(json.dumps({"instance_id": inst["instance_id"], "ip": inst.get("ip") or "",
                          "name": inst.get("api_name") or inst["name"], "deadline": inst["deadline"],
                          "idle_minutes": args.idle_min, "gpu_idle_pct": 5, "api": args.api}, indent=1))


if __name__ == "__main__":
    main()
