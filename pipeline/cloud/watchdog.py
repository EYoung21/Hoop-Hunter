"""Local / scheduled backstop: terminate every Hoop Hunter Lambda instance that is past its max lifetime.

The on-instance watchdog (watchdog_instance.py) is the first line of defence. This one runs from this PC
(Windows Task Scheduler), any always-on machine, or GitHub Actions, and covers an instance whose own watchdog
never got installed, crashed, or holds a stale API key.

    python cloud/watchdog.py                   # one pass (what a scheduler runs)
    python cloud/watchdog.py --loop 10         # a pass every 10 min until Ctrl+C
    python cloud/watchdog.py --dry-run         # report only, never terminate
    python cloud/watchdog.py --key-env LAMBDA_API_KEY   # key from an environment variable (GitHub Actions)

An instance is ours when its name starts with "hh" or "hoophunter" (case-insensitive); anything else on the
account is listed and never touched. Its deadline is the EARLIEST of:

* the ``-xYYMMDDHHMM`` suffix (UTC) that launch.sh puts in the name (``hh-scan2-x2610092000``);
* ``deadline`` (or ``launched_at`` + ``max_hours``) of its record in cloud/last_run.json;
* launch / first sighting + ``--hard-cap-hours`` (default 24), whatever the name or record says;
* first sighting by this watchdog + ``--default-max-hours`` (default 6) when neither name nor record has one
  (first sightings are kept in the state file, cloud/logs/watchdog_state.json).

Past the deadline it calls POST /instance-operations/terminate, re-reading the key before every call and
retrying 401/403/5xx/network errors for ``--retry-s`` (default 300 s). If that still fails it prints
``TERMINATE MANUALLY: instance <id>`` and exits 2. Every pass is appended to cloud/logs/watchdog.log.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

log = logging.getLogger("hh-watchdog-local")

CLOUD_DIR = Path(__file__).resolve().parent
DEFAULT_API = "https://cloud.lambda.ai/api/v1"
DEFAULT_KEY_FILE = Path.home() / ".config" / "hoop-hunter" / "lambda_api_key"
OURS = re.compile(r"^(hh|hoophunter)", re.IGNORECASE)
NAME_DEADLINE = re.compile(r"-x(\d{10})(?:$|-)")
GONE = {"terminated", "terminating"}

if TYPE_CHECKING:
    HttpFn = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(s: Any) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def name_deadline(name: str) -> dt.datetime | None:
    m = NAME_DEADLINE.search(name or "")
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def record_times(rec: dict[str, Any] | None) -> tuple[dt.datetime | None, dt.datetime | None]:
    """(deadline, launched_at) from a last_run.json record."""
    if not rec:
        return None, None
    launched = parse_iso(rec.get("launched_at"))
    deadline = parse_iso(rec.get("deadline"))
    if deadline is None and launched is not None and rec.get("max_hours"):
        deadline = launched + dt.timedelta(hours=float(rec["max_hours"]))
    return deadline, launched


@dataclass
class Verdict:
    instance_id: str
    name: str
    status: str
    ours: bool
    deadline: dt.datetime | None
    source: str
    expired: bool


def judge(inst: dict[str, Any], records: dict[str, dict[str, Any]], seen: dict[str, str], now: dt.datetime,
          default_max_h: float, hard_cap_h: float, last_progress: dt.datetime | None = None,
          stall_minutes: float = 60.0) -> Verdict:
    iid, name, status = str(inst.get("id", "")), str(inst.get("name") or ""), str(inst.get("status") or "")
    ours = bool(OURS.match(name)) or iid in records
    if not ours or status in GONE:
        return Verdict(iid, name, status, ours, None, "not ours" if not ours else "already " + status, False)
    first_seen = parse_iso(seen.get(iid)) or now
    rec_deadline, launched = record_times(records.get(iid))
    start = launched or first_seen
    candidates: list[tuple[dt.datetime, str]] = [(start + dt.timedelta(hours=hard_cap_h), f"hard cap {hard_cap_h:g} h")]
    nd = name_deadline(name)
    if nd is not None:
        candidates.append((nd, "name"))
    if rec_deadline is not None:
        candidates.append((rec_deadline, "last_run.json"))
    if nd is None and rec_deadline is None:
        candidates.append((first_seen + dt.timedelta(hours=default_max_h), f"first seen + {default_max_h:g} h"))
    deadline, source = min(candidates, key=lambda c: c[0])
    # A node that keeps finishing work is never stopped by a deadline: the result puller records when each
    # node's finished-file count last grew (cloud/logs/progress.json); the deadline moves to that + stall_minutes.
    if last_progress is not None and last_progress + dt.timedelta(minutes=stall_minutes) > deadline:
        deadline, source = last_progress + dt.timedelta(minutes=stall_minutes), f"{source}, moved out by progress"
    return Verdict(iid, name, status, True, deadline, source, now >= deadline)


def load_progress(path: Path) -> dict[str, dt.datetime]:
    """name -> last time its finished-file count grew, as written by cloud/pull_results.py."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for name, rec in d.items():
        t = parse_iso(rec.get("last_progress")) if isinstance(rec, dict) else None
        if t is not None:
            out[name] = t
    return out


# --------------------------------------------------------------------------- API
def http(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https API base)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


class Api:
    def __init__(self, base: str, key_file: Path | None, key_env: str | None, httpf: HttpFn = http,
                 timeout: float = 60.0) -> None:
        self.base, self.key_file, self.key_env, self.httpf, self.timeout = base.rstrip("/"), key_file, key_env, httpf, timeout

    def key(self) -> str:
        """Fresh read on every call: the owner may rotate the key at any time."""
        if self.key_env:
            return os.environ.get(self.key_env, "").strip()
        if self.key_file is None:
            return ""
        try:
            return self.key_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        key = self.key()
        if not key:
            log.error("no API key (%s)", f"env {self.key_env}" if self.key_env else self.key_file)
            return 0, None
        # Lambda's API returns 403 to the default Python-urllib User-Agent, so always send our own.
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
                   "User-Agent": "hoophunter-watchdog/1.0"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        code, raw = self.httpf(method, self.base + path, headers, body, self.timeout)
        try:
            return code, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return code, None


def terminate(api: Api, iid: str, retry_s: float, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic, wait_s: float = 15.0) -> bool:
    t0 = clock()
    while True:
        code, data = api.call("POST", "/instance-operations/terminate", {"instance_ids": [iid]})
        log.warning("terminate %s -> HTTP %s", iid, code)
        if code == 200:
            return True
        if code == 404 or (code == 400 and "not found" in json.dumps(data or "").lower()):
            log.warning("instance %s not found: already gone", iid)
            return True
        if code not in (0, 401, 403, 429) and code < 500:
            log.error("terminate %s: unexpected HTTP %s %s", iid, code, json.dumps(data)[:200] if data else "")
        if clock() - t0 >= retry_s:
            return False
        sleep(wait_s)  # the key file is re-read on the next call


# --------------------------------------------------------------------------- one pass
def load_records(path: Path) -> dict[str, dict[str, Any]]:
    """instance_id -> record from last_run.json (current run and previous runs)."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for run in [d, *d.get("previous_runs", [])]:
        for rec in run.get("instances", []):
            if rec.get("instance_id"):
                out[str(rec["instance_id"])] = rec
    return out


def mark_terminated(path: Path, iid: str, reason: str) -> None:
    """Record the termination in last_run.json when the instance has a record (best effort, no lock needed:
    the shell scripts are not running when this backstop has to act)."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for rec in d.get("instances", []):
        if rec.get("instance_id") == iid and rec.get("status") != "terminated":
            rec.update({"status": "terminated", "terminated_at": utcnow().isoformat(timespec="seconds"),
                        "note": f"terminated by cloud/watchdog.py: {reason}"})
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
            return


def run_once(api: Api, *, last_run: Path, state_file: Path, now: dt.datetime | None = None, dry_run: bool = False,
             default_max_h: float = 6.0, hard_cap_h: float = 24.0, retry_s: float = 300.0,
             sleep: Callable[[float], None] = time.sleep, progress_file: Path | None = None) -> int:
    """One pass. Returns 0 (nothing to do / all terminated), 1 (API unreachable), 2 (a terminate failed)."""
    now = now or utcnow()
    code, data = api.call("GET", "/instances")
    if code != 200 or not isinstance(data, dict):
        log.error("GET /instances -> HTTP %s; cannot check instances", code)
        return 1
    instances = data.get("data", [])
    records = load_records(last_run)
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    seen: dict[str, str] = state.setdefault("first_seen", {})
    progress = load_progress(progress_file or state_file.parent / "progress.json")
    rc = 0
    for inst in instances:
        iid = str(inst.get("id", ""))
        seen.setdefault(iid, now.isoformat(timespec="seconds"))
        v = judge(inst, records, seen, now, default_max_h, hard_cap_h,
                  progress.get(str(inst.get("name") or "")))
        if not v.ours or v.deadline is None:
            log.info("%s %s (%s): %s", iid, v.name, v.status, v.source)
            continue
        left_h = (v.deadline - now).total_seconds() / 3600
        if not v.expired:
            log.info("%s %s (%s): deadline %s from %s, %.1f h left", iid, v.name, v.status,
                     v.deadline.isoformat(timespec="minutes"), v.source, left_h)
            continue
        reason = f"past its deadline {v.deadline.isoformat(timespec='minutes')} ({v.source}) by {-left_h:.1f} h"
        if dry_run:
            log.warning("DRY RUN: would terminate %s %s: %s", iid, v.name, reason)
            continue
        log.warning("terminating %s %s: %s", iid, v.name, reason)
        if terminate(api, iid, retry_s, sleep=sleep):
            mark_terminated(last_run, iid, reason)
        else:
            print(f"TERMINATE MANUALLY: instance {iid} ({v.name})", flush=True)
            log.error("TERMINATE MANUALLY: instance %s (%s)", iid, v.name)
            rc = 2
    live = {str(i.get("id")) for i in instances}
    state["first_seen"] = {k: v for k, v in seen.items() if k in live}  # forget instances that are gone
    state["last_pass"] = now.isoformat(timespec="seconds")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")
    log.info("pass done: %d instance(s) on the account", len(instances))
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("LAMBDA_API", DEFAULT_API))
    ap.add_argument("--key-file", type=Path, default=Path(os.environ.get("LAMBDA_KEY_FILE", DEFAULT_KEY_FILE)))
    ap.add_argument("--key-env", help="read the key from this environment variable instead of the file")
    ap.add_argument("--last-run", type=Path, default=CLOUD_DIR / "last_run.json")
    ap.add_argument("--state", type=Path, default=CLOUD_DIR / "logs" / "watchdog_state.json")
    ap.add_argument("--log", type=Path, default=CLOUD_DIR / "logs" / "watchdog.log")
    ap.add_argument("--default-max-hours", type=float, default=6.0)
    ap.add_argument("--hard-cap-hours", type=float, default=24.0)
    ap.add_argument("--retry-s", type=float, default=300.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", type=float, default=0.0, help="minutes between passes (0 = one pass)")
    args = ap.parse_args(argv)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    args.log.parent.mkdir(parents=True, exist_ok=True)
    handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers,
                        force=True)
    api = Api(args.api, None if args.key_env else args.key_file, args.key_env)
    while True:
        rc = run_once(api, last_run=args.last_run, state_file=args.state, dry_run=args.dry_run,
                      default_max_h=args.default_max_hours, hard_cap_h=args.hard_cap_hours, retry_s=args.retry_s)
        if args.loop <= 0:
            return rc
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    sys.exit(main())
