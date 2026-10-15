#!/usr/bin/env bash
# Local orchestration of the national scan: a fleet of single-GPU nodes, each launched with launch.sh (deadline
# + on-instance watchdog) and each running its own manifest of the CONUS plan (data/scan2/US, partitioned with
# `scan_engine partition --by-state`). Node -> manifest assignments live in data/national/fleet.json.
#
#   bash cloud/national.sh up NODE K          # sync code, push plan + manifest K + weights, start national_node.sh
#   bash cloud/national.sh status NODE...     # done/pending, last scan log line, GPU
#   bash cloud/national.sh pull NODE...       # pack on the node, fetch new/changed packs -> data/national/packed/
#   bash cloud/national.sh logs NODE          # tail of the node job log
#
# Packs are per state and node (<ST>.<NODE>.det.parquet / .items.parquet, <ST>.<NODE>.final when the node's
# share of the state is done), so a pull only moves compact detections, never imagery.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lambda_api.sh"

CMD="${1:?up|status|pull|logs}"
shift
NAT="$PIPELINE_DIR/data/national"
FLEET="$NAT/fleet.json"
PACKED="$NAT/packed"
PLAN=data/scan2/US
mkdir -p "$NAT" "$PACKED" "$PIPELINE_DIR/out/cloud/national"

node_ssh() { local n="$1"; shift; ssh "${SSH_OPTS[@]}" "ubuntu@$(lastrun get "$n" ip)" "$@"; }
manifest_of() { "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$FLEET" "$1" 2>/dev/null | tr -d '\r'; }

case "$CMD" in
  up)
    N="${1:?node}"; K="${2:?manifest index}"
    MAN="$PLAN/manifests/node${K}_p0.txt"
    [ -s "$PIPELINE_DIR/$MAN" ] || { echo "missing $MAN (partition first)" >&2; exit 1; }
    [ "$(lastrun get "$N" watchdog)" = "installed" ] || { echo "$N: watchdog not recorded as installed; refusing" >&2; exit 1; }
    "$PY" - "$FLEET" "$N" "$MAN" <<'EOF'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
d = json.loads(p.read_text()) if p.exists() else {}
d[sys.argv[2]] = sys.argv[3]
p.write_text(json.dumps(d, indent=1) + "\n")
EOF
    HH_NODE="$N" bash "$CLOUD_DIR/sync_code.sh" || exit 1
    TMP="$(mktemp -d)"
    (cd "$PIPELINE_DIR" && tar czf "$TMP/push.tgz" "$PLAN/plan.parquet" "$PLAN/plan_stats.json" "$MAN" \
      "$PLAN/manifests/partition.json" weights/best_v2.pt weights/best_v2.json weights/verifier_v2.pt weights/verifier_v2.json)
    t0=$(date +%s)
    scp "${SSH_OPTS[@]}" "$TMP/push.tgz" "ubuntu@$(lastrun get "$N" ip):hh/push.tgz" || exit 1
    echo "pushed $(du -h "$TMP/push.tgz" | cut -f1) in $(( $(date +%s) - t0 )) s"
    rm -rf "$TMP"
    node_ssh "$N" "cd ~/hh/pipeline && tar xzf ~/hh/push.tgz && sed -i 's/\r$//' cloud/*.sh && ls -la $MAN" || exit 1
    HH_NODE="$N" bash "$CLOUD_DIR/start_stage.sh" job node bash cloud/national_node.sh "$PLAN" "$MAN" "$N" ;;
  status)
    for n in "$@"; do
      MAN="$(manifest_of "$n")"
      echo "== $n ($MAN) $(lastrun get "$n" status) deadline $(lastrun get "$n" deadline)"
      node_ssh "$n" "cd ~/hh/pipeline && PYTHONPATH=. ~/hhvenv/bin/python -m hoop_pipeline.scan_engine status --plan $PLAN --manifest $MAN 2>/dev/null; grep -E 'items, .* tiles|backend:|NODE_SCAN|FAILED|Traceback|failed:' ~/hh/logs/node.log | tail -2 | cut -c1-300; nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader; uptime | sed 's/.*load/load/'" || echo "unreachable"
    done ;;
  pull)
    for n in "$@"; do
      MAN="$(manifest_of "$n")"
      [ -n "$MAN" ] || { echo "$n: no manifest in $FLEET" >&2; continue; }
      LIST="$(node_ssh "$n" "cd ~/hh/pipeline && PYTHONPATH=. ~/hhvenv/bin/python -m hoop_pipeline.scan_engine pack --plan $PLAN --manifest $MAN --node $n --out $PLAN/packed 2>/dev/null >/dev/null; cd $PLAN/packed 2>/dev/null && find . -maxdepth 1 -type f ! -name '.*' -printf '%P\t%s\t%T@\n'")" || { echo "$n: unreachable"; continue; }
      [ -n "$LIST" ] || { echo "$n: nothing packed yet"; continue; }
      # fetch files that are new or whose size/mtime changed since the last pull
      WANT="$("$PY" - "$PACKED/.index_$n.json" <<EOF
import json, sys
from pathlib import Path
idx = Path(sys.argv[1])
old = json.loads(idx.read_text()) if idx.exists() else {}
want = []
for line in """$LIST""".splitlines():
    name, size, mt = line.split("\t")
    if old.get(name) != [size, mt] or not (idx.parent / name).exists():
        want.append(name)
print(" ".join(want))
EOF
)"
      WANT="$(printf '%s' "$WANT" | tr -d '\r')"
      if [ -n "$WANT" ]; then
        node_ssh "$n" "cd ~/hh/pipeline/$PLAN/packed && tar czf ~/hh/pull_$n.tgz $WANT" && \
          scp -q "${SSH_OPTS[@]}" "ubuntu@$(lastrun get "$n" ip):hh/pull_$n.tgz" "$PACKED/.pull_$n.tgz" && \
          tar xzf "$PACKED/.pull_$n.tgz" -C "$PACKED" && rm -f "$PACKED/.pull_$n.tgz" || { echo "$n: fetch failed"; continue; }
      fi
      "$PY" - "$PACKED/.index_$n.json" <<EOF
import json, sys
from pathlib import Path
d = {}
for line in """$LIST""".splitlines():
    name, size, mt = line.split("\t")
    d[name] = [size, mt]
Path(sys.argv[1]).write_text(json.dumps(d))
EOF
      echo "$n: $(printf '%s' "$WANT" | wc -w) file(s) fetched; finals: $(ls "$PACKED" | grep -c "\.$n\.final$")"
    done ;;
  logs)
    node_ssh "${1:?node}" "tail -n ${2:-30} ~/hh/logs/node.log" ;;
  *)
    echo "unknown command $CMD" >&2; exit 2 ;;
esac
