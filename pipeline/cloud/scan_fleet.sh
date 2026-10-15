#!/usr/bin/env bash
# Multi-node orchestration for scan_engine (static, lock-free partitioning; one manifest per node).
#
# Every node is a single-GPU Lambda instance recorded in cloud/last_run.json under a short name.
# The plan (data/scan2/<SCOPE>/plan.parquet) and manifests are built once (on "main" or locally) and
# copied to every node; a node only processes its own manifest and writes per-item results with done
# markers, so a restart (or a replacement node given the same manifest) skips finished items.
#
#   HH_MAX_HOURS=4 bash cloud/scan_fleet.sh launch scan2 scan3 scan4   # launch (+ watchdog) + sync + setup, parallel
#   bash cloud/scan_fleet.sh push TN main scan2 scan3 scan4   # copy plan, manifests, weights (+ engine) from main
#        (HH_PUSH_WEIGHTS="weights/best_v1.pt weights/best_v1.json" pushes other weights; HH_PUSH_VERIFIER=1 the verifier)
#   bash cloud/scan_fleet.sh start TN "--weights weights/best_v2.pt --imgsz 416" main scan2 scan3 scan4
#   bash cloud/scan_fleet.sh status TN main scan2 scan3 scan4
#   bash cloud/scan_fleet.sh collect TN main scan2 scan3 scan4   # pull every node's results into main's results/
#   bash cloud/scan_fleet.sh terminate scan2 scan3 scan4
#
# Node k (in the order given to `start`) runs manifests/node<k>_p*.txt; partition first with
#   python -m hoop_pipeline.scan_engine partition --plan data/scan2/TN --nodes 4
# If a node dies: `partition --pending --nodes <survivors>` on main after `collect`, then `push` and `start`.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lambda_api.sh"

CMD="${1:?launch|push|start|status|collect|terminate}"
shift
STAGE_DIR="$PIPELINE_DIR/out/cloud/fleet"
mkdir -p "$STAGE_DIR"

node_ssh() { local n="$1"; shift; ssh "${SSH_OPTS[@]}" "ubuntu@$(lastrun get "$n" ip)" "$@"; }
node_scp_up() { scp "${SSH_OPTS[@]}" "$2" "ubuntu@$(lastrun get "$1" ip):$3"; }
node_scp_down() { scp "${SSH_OPTS[@]}" "ubuntu@$(lastrun get "$1" ip):$2" "$3"; }

case "$CMD" in
  launch)
    pids=()
    for n in "$@"; do
      ( bash "$CLOUD_DIR/launch.sh" "$n" "${HH_TYPES:-gpu_1x_a100_sxm4,gpu_1x_a10,gpu_1x_h100_sxm5}" \
          --max-hours "${HH_MAX_HOURS:-6}" \
          && HH_NODE="$n" bash "$CLOUD_DIR/sync_code.sh" \
          && HH_NODE="$n" bash "$CLOUD_DIR/start_stage.sh" setup ) > "$STAGE_DIR/launch_$n.log" 2>&1 &
      pids+=($!)
      sleep 3
    done
    rc=0
    for p in "${pids[@]}"; do wait "$p" || rc=1; done
    for n in "$@"; do echo "== $n"; tail -3 "$STAGE_DIR/launch_$n.log"; done
    exit $rc ;;
  push)
    SCOPE="${1:?scope}"; shift
    SRC="${1:?source node (has the plan)}"; shift
    # the verifier (45 MB) is only pushed when the scan uses it: HH_PUSH_VERIFIER=1
    EXTRA=""
    [ "${HH_PUSH_VERIFIER:-0}" = "1" ] && EXTRA="\$(ls weights/verifier*.pt 2>/dev/null)"
    WEIGHTS="${HH_PUSH_WEIGHTS:-weights/best_v2.pt weights/best_v2.json}"
    node_ssh "$SRC" "cd ~/hh/pipeline && tar czf ~/hh/fleet_push.tgz data/scan2/$SCOPE/plan.parquet data/scan2/$SCOPE/plan_stats.json data/scan2/$SCOPE/manifests $WEIGHTS \$(ls weights/*.engine 2>/dev/null) $EXTRA"
    node_scp_down "$SRC" hh/fleet_push.tgz "$STAGE_DIR/fleet_push.tgz"
    for n in "$@"; do
      [ "$n" = "$SRC" ] && continue
      node_scp_up "$n" "$STAGE_DIR/fleet_push.tgz" hh/fleet_push.tgz && node_ssh "$n" "cd ~/hh/pipeline && tar xzf ~/hh/fleet_push.tgz && ls data/scan2/$SCOPE/manifests | head" &
    done
    wait ;;
  start)
    SCOPE="${1:?scope}"; shift
    ARGS="${1:?run args}"; shift
    k=0
    for n in "$@"; do
      for m in $(node_ssh "$n" "ls ~/hh/pipeline/data/scan2/$SCOPE/manifests/node${k}_p*.txt 2>/dev/null"); do
        j=$(basename "$m" .txt)
        # shellcheck disable=SC2086
        HH_NODE="$n" bash "$CLOUD_DIR/start_stage.sh" job "scan_${SCOPE}_$j" python -m hoop_pipeline.scan_engine run \
          --plan "data/scan2/$SCOPE" --manifest "data/scan2/$SCOPE/manifests/$j.txt" $ARGS
      done
      k=$((k + 1))
    done ;;
  status)
    SCOPE="${1:?scope}"; shift
    for n in "$@"; do
      echo "== $n"
      node_ssh "$n" "cd ~/hh/pipeline && ls data/scan2/$SCOPE/results/*/*.done.json 2>/dev/null | wc -l; for f in ~/hh/logs/scan_${SCOPE}_node*.log; do [ -f \"\$f\" ] && tail -1 \"\$f\" | cut -c1-260; done; nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader" || echo "unreachable"
    done ;;
  collect)
    SCOPE="${1:?scope}"; shift
    DST="${1:?destination node}"; shift
    for n in "$@"; do
      [ "$n" = "$DST" ] && continue
      node_ssh "$n" "cd ~/hh/pipeline && tar czf ~/hh/results_$n.tgz data/scan2/$SCOPE/results ~/hh/logs/scan_${SCOPE}_node*.log 2>/dev/null; ls -la ~/hh/results_$n.tgz"
      node_scp_down "$n" "hh/results_$n.tgz" "$STAGE_DIR/results_$n.tgz"
      node_scp_up "$DST" "$STAGE_DIR/results_$n.tgz" "hh/results_$n.tgz"
      node_ssh "$DST" "cd ~/hh/pipeline && tar xzf ~/hh/results_$n.tgz data/scan2 && mkdir -p ~/hh/logs/fleet_$n && tar xzf ~/hh/results_$n.tgz -C ~/hh/logs/fleet_$n --wildcards '*scan_${SCOPE}_node*' 2>/dev/null; ls data/scan2/$SCOPE/results/*/*.done.json | wc -l"
    done ;;
  terminate)
    rc=0
    for n in "$@"; do bash "$CLOUD_DIR/terminate.sh" "$n" || rc=1; done
    exit $rc ;;
  *)
    echo "unknown command $CMD" >&2; exit 2 ;;
esac
