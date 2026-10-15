"""Copy new national-scan result files from every live scan node to this PC.

Runs from Windows Task Scheduler every 10 min, so results reach this PC even if the
coordinating agent dies. Each node writes one Parquet plus one .done.json per NAIP
file under ~/hh/pipeline/data/scan2/US/results/<STATE>/. Only files this PC doesn't
have yet (or whose size changed) are copied, streamed through ssh + tar. The node
watchdogs kill a node 30 min after it goes idle, so a 10-min pull cadence copies
everything before a node disappears.

    python cloud/pull_results.py             # one pass over all live hh-s* nodes
    python cloud/pull_results.py --dry-run   # list what would be copied
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

PIPE = Path(__file__).resolve().parents[1]
REMOTE_ROOT = "~/hh/pipeline"
REL = "data/scan2/US"
LOCAL = PIPE / REL
KEY_FILE = Path.home() / ".config" / "hoop-hunter" / "lambda_api_key"
SSH_KEY = Path.home() / ".ssh" / "hoophunter_lambda"
API = "https://cloud.lambda.ai/api/v1"
log = logging.getLogger("pull_results")


def ssh_exe() -> str:
    return shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"


def ssh_cmd(ip: str, remote: str) -> list[str]:
    return [ssh_exe(), "-i", str(SSH_KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
            "-o", "StrictHostKeyChecking=accept-new", f"ubuntu@{ip}", remote]


def live_nodes() -> list[tuple[str, str]]:
    """(name, ip) of active instances whose name starts with hh-s (scan nodes)."""
    req = urllib.request.Request(f"{API}/instances", headers={
        "Authorization": f"Bearer {KEY_FILE.read_text().strip()}", "Accept": "application/json",
        "User-Agent": "hoophunter-pull/1.0"})  # Lambda returns 403 to the default Python-urllib agent
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed https API base)
        data = json.load(r)["data"]
    return [(i["name"], i["ip"]) for i in data
            if (i.get("name") or "").startswith("hh-s") and i.get("status") == "active" and i.get("ip")]


def remote_listing(ip: str) -> dict[str, int]:
    out = subprocess.run(ssh_cmd(ip, f"cd {REMOTE_ROOT} && find {REL}/results {REL}/manifests {REL}/plan_stats.json "
                                     f"-type f -printf '%s %p\\n' 2>/dev/null"),
                         capture_output=True, text=True, timeout=120, check=False)
    files: dict[str, int] = {}
    for line in out.stdout.splitlines():
        size, _, path = line.partition(" ")
        if path:
            files[path] = int(size)
    return files


def fetch(ip: str, paths: list[str]) -> int:
    proc = subprocess.Popen(ssh_cmd(ip, f"cd {REMOTE_ROOT} && tar -cf - -T -"),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdin and proc.stdout
    proc.stdin.write(("\n".join(paths) + "\n").encode())
    proc.stdin.close()
    n = 0
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
        for m in tf:
            if m.isfile() and m.name.startswith(REL + "/") and ".." not in m.name:
                tf.extract(m, PIPE, filter="data")
                n += 1
    proc.wait(timeout=600)
    return n


def scan_complete(ip: str) -> bool:
    """True only when no scan process runs AND every item in the node's manifest has a done marker.
    A node that is still setting up, or whose scan crashed part-way, keeps its hold (until its deadline)."""
    r = subprocess.run(ssh_cmd(ip, f"cd {REMOTE_ROOT}/{REL} && m=$(cat manifests/node*_p*.txt 2>/dev/null | wc -l); "
                                   "d=$(find results -name '*.done.json' 2>/dev/null | wc -l); "
                                   "pgrep -f 'hoop_pipeline[.]scan_engine run' >/dev/null && s=running || s=idle; "
                                   "echo $s $d $m"),
                       capture_output=True, text=True, timeout=60, check=False)
    parts = r.stdout.split()
    if len(parts) != 3:
        return False
    state, done, items = parts[0], int(parts[1]), int(parts[2])
    return state == "idle" and items > 0 and done >= items


PROGRESS = PIPE / "cloud" / "logs" / "progress.json"


def record_progress(name: str, files: dict[str, int]) -> None:
    """Note when this node's finished-file count last grew; cloud/watchdog.py keeps working nodes alive past
    their deadline based on it."""
    import datetime as dt
    done = sum(1 for p in files if p.endswith(".done.json"))
    try:
        d = json.loads(PROGRESS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    rec = d.get(name, {})
    if done > int(rec.get("done", -1)):
        rec = {"done": done, "last_progress": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    d[name] = rec
    PROGRESS.write_text(json.dumps(d, indent=1), encoding="utf-8")


def progress(name: str, files: dict[str, int]) -> str:
    done = sum(1 for p in files if p.endswith(".done.json"))
    return f"{name}: {done} files done on node"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    (PIPE / "cloud" / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(PIPE / "cloud" / "logs" / "pull_results.log"), logging.StreamHandler()])
    try:
        nodes = live_nodes()
    except Exception as e:  # noqa: BLE001 (keep the scheduled task alive)
        log.error("cannot list instances: %s", e)
        return
    total_local = sum(1 for _ in LOCAL.glob("results/*/*.done.json")) if LOCAL.exists() else 0
    log.info("pass start: %d live scan node(s); %d files already on this PC", len(nodes), total_local)
    for name, ip in nodes:
        try:
            files = remote_listing(ip)
            record_progress(name, files)
            need = [p for p, s in files.items() if not (PIPE / p).exists() or (PIPE / p).stat().st_size != s]
            # copy a .done.json only together with (after) its parquet, so a local done marker always has its data
            need.sort(key=lambda p: p.endswith(".done.json"))
            log.info("%s (%s); %d new file(s) to copy", progress(name, files), ip, len(need))
            if need and not args.dry_run:
                for i in range(0, len(need), 2000):
                    log.info("%s: copied %d", name, fetch(ip, need[i:i + 2000]))
            # Each node carries a watchdog hold (~/hh/watchdog_hold_until) so it can't go idle-terminate
            # before its results are here. Release it once the node's whole manifest is done and everything is copied;
            # the node's watchdog then terminates it after 30 idle minutes. If this PC is asleep, the hold
            # keeps the node (and its results) alive until the node's hard deadline instead.
            if not args.dry_run and scan_complete(ip):
                left = [p for p, s in remote_listing(ip).items()
                        if not (PIPE / p).exists() or (PIPE / p).stat().st_size != s]
                if not left:
                    subprocess.run(ssh_cmd(ip, "rm -f ~/hh/watchdog_hold_until"), timeout=60, check=False)
                    log.info("%s: scan finished and fully copied; released the watchdog hold", name)
        except Exception as e:  # noqa: BLE001
            log.error("%s (%s): %s", name, ip, e)
    log.info("pass done")


if __name__ == "__main__":
    main()
