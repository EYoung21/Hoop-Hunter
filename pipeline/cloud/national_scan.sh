#!/usr/bin/env bash
# Run ON a scan node (cloud/national.sh start does this through start_stage.sh):
#   bash cloud/national_scan.sh PLAN_DIR MANIFEST NODE [scan_engine run args...]
# Runs scan_engine run over this node's manifest until every item has a done marker (at most 4 passes; a pass
# only redoes items without a marker, e.g. after a read error), packing finished states after every pass.
# The pack (PLAN_DIR/packed/<ST>.<NODE>.{det,items}.parquet) is what cloud/national.sh pull fetches.
set -uo pipefail
cd ~/hh/pipeline
PY=~/hhvenv/bin/python
export PYTHONPATH=~/hh/pipeline
PLAN="${1:?plan dir}"; MANIFEST="${2:?manifest}"; NODE="${3:?node name}"
shift 3
P="?"
for pass in 1 2 3 4; do
  echo "== pass $pass $(date -u +%FT%TZ)"
  $PY -m hoop_pipeline.scan_engine run --plan "$PLAN" --manifest "$MANIFEST" "$@"
  P=$($PY -m hoop_pipeline.scan_engine status --plan "$PLAN" --manifest "$MANIFEST" | $PY -c 'import json,sys; print(json.load(sys.stdin)["pending"])')
  $PY -m hoop_pipeline.scan_engine pack --plan "$PLAN" --manifest "$MANIFEST" --node "$NODE" --out "$PLAN/packed"
  if [ "$P" = "0" ]; then
    echo "NODE_SCAN_COMPLETE $NODE $(date -u +%FT%TZ)"
    exit 0
  fi
  echo "pass $pass: $P items pending; retrying in 60 s"
  sleep 60
done
echo "NODE_SCAN_INCOMPLETE $NODE ($P items pending)"
exit 1
