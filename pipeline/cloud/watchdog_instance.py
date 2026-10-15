#!/usr/bin/env python3
"""On-instance safety shutoff: terminates THIS Lambda instance when its max lifetime passes or it sits idle.

The failure it guards against is the coordinating agent (or laptop) dying while instances run: the
instance terminates itself through the Lambda API without anyone watching. Standard library only
(runs under the system python3 of Lambda Stack, as root).

Installed by ``cloud/watchdog_install.sh`` (called by ``launch.sh`` right after the instance turns active,
and again by ``setup_remote.sh``) as a systemd timer that runs ``check`` every 2 minutes, plus a cron
fallback every 5 minutes. Files on the instance:

    /etc/hoophunter/watchdog.json        config: instance_id, ip, name, deadline (UTC), idle_minutes, api  (root, 0600)
    /etc/hoophunter/lambda_api_key       the API key (root, 0600) -- an accepted trade-off for a self-terminating node
    /var/lib/hoophunter/watchdog.json    state: last activity, terminate attempts
    /var/log/hoophunter-watchdog.log     every decision and API call (never the key)

Rules, checked in order on every run:

1. **max lifetime**: now >= deadline (``launch.sh --max-hours``, default 6) -> terminate.
2. **idle**: no activity for ``idle_minutes`` (default 30) -> terminate. Activity is any of: GPU utilisation
   >= ``gpu_idle_pct`` (5%), a process on the GPU, a pipeline process (``run_remote.sh``, ``hoop_pipeline``,
   the venv's python, ``setup_remote.sh``, ...), or a hold set with ``watchdog_instance.py hold MINUTES``
   (written to ~ubuntu/hh/watchdog_hold_until; a hold never extends past the deadline).

The terminate call re-reads the key file for every attempt, retries with back-off on network errors,
401/403 and 5xx, resolves the instance id from the API by matching this node's IP when the configured id is
unknown (404), and logs every attempt. If every attempt of a run fails, the next timer run tries again.

    python3 watchdog_instance.py check [--dry-run] [--config PATH] [--now ISO]
    python3 watchdog_instance.py status            # decision as JSON, never terminates
    python3 watchdog_instance.py hold 45           # count as active for 45 more minutes (capped at the deadline)
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

log = logging.getLogger("hh-watchdog")

DEFAULT_CONFIG = Path("/etc/hoophunter/watchdog.json")
DEFAULT_JOB_PATTERNS = ["run_remote.sh", "hoop_pipeline", "hhvenv/bin/python", "setup_remote.sh", "start_stage"]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        if s.replace(".", "", 1).isdigit():
            return dt.datetime.fromtimestamp(float(s), dt.timezone.utc)
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


@dataclass
class Config:
    instance_id: str
    deadline: dt.datetime
    ip: str = ""
    name: str = ""
    idle_minutes: float = 30.0
    gpu_idle_pct: float = 5.0
    api: str = "https://cloud.lambda.ai/api/v1"
    key_file: Path = Path("/etc/hoophunter/lambda_api_key")
    state_file: Path = Path("/var/lib/hoophunter/watchdog.json")
    log_file: Path = Path("/var/log/hoophunter-watchdog.log")
    hold_file: Path = Path("/home/ubuntu/hh/watchdog_hold_until")
    job_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_JOB_PATTERNS))
    attempts: int = 5
    backoff_s: list[float] = field(default_factory=lambda: [5.0, 15.0, 30.0, 60.0])
    http_timeout_s: float = 30.0

    @classmethod
    def load(cls, path: Path) -> Config:
        d = json.loads(path.read_text(encoding="utf-8"))
        deadline = parse_time(d.get("deadline"))
        if deadline is None:
            raise SystemExit(f"{path}: 'deadline' (UTC ISO time) is required")
        kw: dict[str, Any] = {"instance_id": d.get("instance_id", ""), "deadline": deadline}
        for k in ("ip", "name", "api"):
            if d.get(k):
                kw[k] = str(d[k])
        for k in ("idle_minutes", "gpu_idle_pct", "http_timeout_s"):
            if d.get(k) is not None:
                kw[k] = float(d[k])
        for k in ("key_file", "state_file", "log_file", "hold_file"):
            if d.get(k):
                kw[k] = Path(d[k])
        if d.get("job_patterns"):
            kw["job_patterns"] = [str(p) for p in d["job_patterns"]]
        if d.get("attempts"):
            kw["attempts"] = int(d["attempts"])
        if d.get("backoff_s") is not None:
            kw["backoff_s"] = [float(x) for x in d["backoff_s"]]
        return cls(**kw)


# --------------------------------------------------------------------------- probes (replaced in tests)
def gpu_activity() -> tuple[float, int] | None:
    """(max GPU utilisation %, processes using a GPU), or None when nvidia-smi does not answer."""
    try:
        util = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=30, check=True).stdout
        apps = subprocess.run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=30, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    vals = [float(x) for x in util.split() if x.replace(".", "", 1).isdigit()]
    return (max(vals) if vals else 0.0, len([x for x in apps.splitlines() if x.strip()]))


def pipeline_processes(patterns: list[str]) -> list[str]:
    """Command lines (shortened) of running processes that match a pipeline pattern."""
    me = os.getpid()
    hits = []
    for p in Path("/proc").glob("[0-9]*"):
        if int(p.name) == me:
            continue
        try:
            cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if cmd and "watchdog_instance.py" not in cmd and any(pat in cmd for pat in patterns):
            hits.append(cmd[:160])
    return hits


def local_ips() -> list[str]:
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return out.split()


# --------------------------------------------------------------------------- state
@contextlib.contextmanager
def single_run(state_file: Path) -> Iterator[bool]:
    """Non-blocking lock so the timer and the cron fallback never run at the same time (POSIX only)."""
    lock = state_file.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "a+")  # noqa: SIM115
    try:
        if sys.platform != "win32":  # tests on Windows run without the lock
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
        yield True
    finally:
        fh.close()


def load_state(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)


def read_hold(path: Path) -> dt.datetime | None:
    try:
        return parse_time(path.read_text(encoding="utf-8"))
    except OSError:
        return None


# --------------------------------------------------------------------------- decision
@dataclass
class Decision:
    terminate: bool
    reason: str
    idle_minutes: float
    active: list[str]
    terminated: bool | None = None  # set by run_check after a terminate call


def decide(cfg: Config, state: dict[str, Any], now: dt.datetime, gpu: tuple[float, int] | None, jobs: list[str],
           hold_until: dt.datetime | None) -> Decision:
    """Pure decision; updates ``state['last_active']`` (and sets it on the first run)."""
    first = parse_time(state.get("first_run"))
    if first is None:
        state["first_run"] = now.isoformat(timespec="seconds")
    active = []
    if gpu is not None and gpu[0] >= cfg.gpu_idle_pct:
        active.append(f"gpu {gpu[0]:.0f}%")
    if gpu is not None and gpu[1] > 0:
        active.append(f"{gpu[1]} GPU process(es)")
    if jobs:
        active.append(f"{len(jobs)} pipeline process(es)")
    if hold_until is not None and now < min(hold_until, cfg.deadline):
        active.append(f"hold until {min(hold_until, cfg.deadline).isoformat(timespec='minutes')}")
    if active or parse_time(state.get("last_active")) is None:
        state["last_active"] = now.isoformat(timespec="seconds")
    last = parse_time(state.get("last_active")) or now
    idle = max(0.0, (now - last).total_seconds() / 60)
    if now >= cfg.deadline:
        return Decision(True, f"max lifetime reached (deadline {cfg.deadline.isoformat(timespec='minutes')})", idle, active)
    if idle >= cfg.idle_minutes:
        return Decision(True, f"idle for {idle:.0f} min (GPU < {cfg.gpu_idle_pct:.0f}%, no GPU or pipeline process, "
                              f"no hold)", idle, active)
    left = (cfg.deadline - now).total_seconds() / 3600
    return Decision(False, f"ok: {'active (' + ', '.join(active) + ')' if active else f'idle {idle:.0f} min'}; "
                           f"{left:.1f} h to deadline", idle, active)


# --------------------------------------------------------------------------- Lambda API
if TYPE_CHECKING:  # annotation-only alias, so the module also imports on an older system python
    HttpFn = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


def http(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https API base)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


def api_call(cfg: Config, method: str, path: str, payload: dict[str, Any] | None, httpf: HttpFn) -> tuple[int, Any]:
    try:
        key = cfg.key_file.read_text(encoding="utf-8").strip()  # fresh read on every call (the key may be rotated)
    except OSError as e:
        log.error("cannot read key file %s: %s", cfg.key_file, e.strerror)
        return 0, None
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    code, raw = httpf(method, cfg.api.rstrip("/") + path, headers, body, cfg.http_timeout_s)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        data = None
    return code, data


def resolve_instance_id(cfg: Config, httpf: HttpFn, ips: list[str]) -> str:
    """Instance id from GET /instances by matching this node's public IP (config ip or a local address)."""
    code, data = api_call(cfg, "GET", "/instances", None, httpf)
    if code != 200 or not isinstance(data, dict):
        log.warning("GET /instances -> HTTP %s; cannot resolve the instance id", code)
        return ""
    want = {x for x in [cfg.ip, *ips] if x}
    for inst in data.get("data", []):
        if inst.get("ip") in want or inst.get("private_ip") in want:
            return str(inst.get("id", ""))
    log.warning("no instance on the account matches IPs %s", sorted(want))
    return ""


def terminate_self(cfg: Config, httpf: HttpFn = http, sleep: Callable[[float], None] = time.sleep,
                   ips: Callable[[], list[str]] = local_ips) -> bool:
    iid = cfg.instance_id
    for attempt in range(1, cfg.attempts + 1):
        if not iid:
            iid = resolve_instance_id(cfg, httpf, ips())
        if iid:
            code, data = api_call(cfg, "POST", "/instance-operations/terminate", {"instance_ids": [iid]}, httpf)
            done = [i.get("id") for i in ((data or {}).get("data") or {}).get("terminated_instances", [])] \
                if isinstance(data, dict) else []
            log.warning("terminate %s attempt %d/%d -> HTTP %s %s", iid, attempt, cfg.attempts, code, done or "")
            if code == 200:
                return True
            if code == 404:  # unknown id (e.g. a wrong record): look it up by IP next time round
                iid = ""
        if attempt < cfg.attempts:
            sleep(cfg.backoff_s[min(attempt - 1, len(cfg.backoff_s) - 1)] if cfg.backoff_s else 0.0)
    log.error("TERMINATE FAILED after %d attempts (instance %s); the next timer run retries", cfg.attempts,
              iid or "unknown")
    return False


# --------------------------------------------------------------------------- commands
def run_check(cfg: Config, *, dry_run: bool = False, now: dt.datetime | None = None,
              gpu: Callable[[], tuple[float, int] | None] = gpu_activity,
              jobs: Callable[[list[str]], list[str]] = pipeline_processes, httpf: HttpFn = http,
              sleep: Callable[[float], None] = time.sleep, ips: Callable[[], list[str]] = local_ips) -> Decision:
    now = now or utcnow()
    with single_run(cfg.state_file) as ok:
        if not ok:
            log.info("another watchdog run holds the lock; skipping")
            return Decision(False, "locked", 0.0, [])
        state = load_state(cfg.state_file)
        d = decide(cfg, state, now, gpu(), jobs(cfg.job_patterns), read_hold(cfg.hold_file))
        state["last_check"] = now.isoformat(timespec="seconds")
        state["last_decision"] = d.reason
        if d.terminate:
            log.warning("%s -> %s", d.reason, "DRY RUN, not terminating" if dry_run else "terminating this instance")
            if not dry_run:
                state["terminate_attempts"] = int(state.get("terminate_attempts", 0)) + 1
                save_state(cfg.state_file, state)
                state["terminated"] = d.terminated = terminate_self(cfg, httpf, sleep, ips)
        else:
            log.info("%s", d.reason)
        save_state(cfg.state_file, state)
        return d


def set_hold(cfg: Config, minutes: float, now: dt.datetime | None = None) -> dt.datetime:
    now = now or utcnow()
    until = min(now + dt.timedelta(minutes=minutes), cfg.deadline)
    cfg.hold_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.hold_file.write_text(until.isoformat(timespec="seconds") + "\n", encoding="utf-8")
    return until


def setup_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers,
                        force=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="decide and terminate this instance if a rule fires")
    p.add_argument("--dry-run", action="store_true", help="log the decision, never call the API")
    p.add_argument("--now", help="pretend it is this UTC time (testing)")
    sub.add_parser("status", help="print the decision as JSON; never terminates")
    p = sub.add_parser("hold", help="count as active for MINUTES more (never past the deadline)")
    p.add_argument("minutes", type=float)
    args = ap.parse_args(argv)
    cfg = Config.load(args.config)
    setup_logging(cfg.log_file if args.cmd == "check" else None)
    if args.cmd == "hold":
        print("hold until", set_hold(cfg, args.minutes).isoformat(timespec="seconds"))
        return 0
    if args.cmd == "status":
        state = load_state(cfg.state_file)
        d = decide(cfg, state, utcnow(), gpu_activity(), pipeline_processes(cfg.job_patterns), read_hold(cfg.hold_file))
        print(json.dumps({"instance_id": cfg.instance_id, "name": cfg.name, "deadline": cfg.deadline.isoformat(),
                          "would_terminate": d.terminate, "reason": d.reason, "idle_minutes": round(d.idle_minutes, 1),
                          "active": d.active}, indent=1))
        return 0
    d = run_check(cfg, dry_run=args.dry_run, now=parse_time(args.now))
    return 3 if d.terminated is False else 0  # non-zero only when a terminate call failed (shows in systemctl)


if __name__ == "__main__":
    sys.exit(main())
