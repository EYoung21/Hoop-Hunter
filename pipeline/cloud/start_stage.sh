#!/usr/bin/env bash
# Start a run_remote.sh stage on node $HH_NODE (default "main") in the background and return at once.
# Logs: ~/hh/logs/<stage>.log; markers ~/hh/logs/<stage>.{start,done,failed}.
#
#   bash cloud/start_stage.sh osm
#   bash cloud/start_stage.sh dataset2 --workers 128
#   bash cloud/start_stage.sh job score_v1 python -m hoop_pipeline.label_clean score --weights weights/best_v1.pt
#   HH_NODE=scan2 bash cloud/start_stage.sh status    # markers + last log lines of every stage on that node
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lambda_api.sh"

STAGE="${1:?stage}"
shift
if [ "$STAGE" = "status" ]; then
  hh_ssh 'cd ~/hh/logs 2>/dev/null && for f in *.log; do s=${f%.log}; m=$(ls $s.done $s.failed 2>/dev/null | head -1); echo "== $s ${m:-running}"; tail -n 3 "$f" | cut -c1-240; done; uptime; nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader; df -h / | tail -1; free -g | head -2'
  exit 0
fi
MARK="$STAGE"
if [ "$STAGE" = "job" ]; then MARK="${1:?job name}"; fi
ARGS=""
for a in "$@"; do ARGS="$ARGS $(printf '%q' "$a")"; done
# The whole job runs in a detached subshell with every fd redirected, so ssh returns at once.
hh_ssh "cd ~/hh/pipeline && rm -f ~/hh/logs/$MARK.done ~/hh/logs/$MARK.failed && (setsid nohup bash cloud/run_remote.sh $STAGE $ARGS > ~/hh/logs/$MARK.log 2>&1 < /dev/null &) && echo started $MARK on $HH_NODE"
