#!/usr/bin/env bash
# Run ON the instance: one pipeline stage, with start/done/failed markers in ~/hh/logs.
# Normally started in the background by cloud/start_stage.sh; extra args go to the Python step.
#
# Stages (v1 run order: setup, pbf, osm, naip, dataset, train, scan_plan, scan_fetch, eval, scan_detect):
#   setup        apt + venv + pip (setup_remote.sh)
#   pbf          download the Geofabrik US extract (~12 GB), verify md5
#   osm          osm_extract -> data/osm_us/{courts_us,indoor_us,context_us}.parquet, courts_TN.geojson
#   naip         NAIP GeoParquet catalog + per-state year summary
#   dataset      build_dataset_us -> datasets/us   (v1)
#   dataset2     build_dataset_us -> datasets/us2  (v2 negative mix, see README "v2 workflow")
#   train        YOLO training -> weights/best.pt, runs/<name>/
#   eval         evaluate on the TN holdout (val) and the val_us slice
#   scan_plan / scan_fetch / scan_detect   v1 masked statewide scan (scan_state)
#   package      tar the results for cloud/fetch_results.sh
#   job NAME CMD...   run any command (e.g. "python -m hoop_pipeline.label_clean score ...") with markers
#                     NAME.{start,done,failed} and log NAME.log; "python" means the venv's python
#
# The whole script is one function that is called on the last line: bash parses it
# completely before running anything, so editing this file while a stage runs is safe.
set -E
main() {
  set -uo pipefail
  STAGE="${1:?stage}"
  shift
  cd ~/hh/pipeline
  PY=~/hhvenv/bin/python
  LOGS=~/hh/logs
  mkdir -p "$LOGS" data/osm_us
  MARK="$STAGE"
  if [ "$STAGE" = "job" ]; then MARK="${1:?job name}"; shift; fi
  date -u +%FT%TZ > "$LOGS/$MARK.start"
  trap 'date -u +%FT%TZ > "$LOGS/$MARK.failed"; echo "STAGE $MARK FAILED"' ERR
  set -e
  RUN_NAME="${HH_RUN_NAME:-us-y11s}"
  export PYTHONPATH=~/hh/pipeline
  export HH_ANONYMOUS_READS="${HH_ANONYMOUS_READS:-}"

  case "$STAGE" in
    job)
      if [ "${1:-}" = "python" ]; then shift; "$PY" "$@"; else "$@"; fi ;;
    setup)
      bash cloud/setup_remote.sh ;;
    pbf)
      URL="${HH_PBF_URL:-https://download.geofabrik.de/north-america/us-latest.osm.pbf}"
      OUT=data/osm_us/us-latest.osm.pbf
      if [ ! -s "$OUT" ]; then
        curl -L --fail --retry 8 --retry-delay 10 -C - -o "$OUT.part" "$URL"
        mv "$OUT.part" "$OUT"
      fi
      EXPECT=$(curl -sL --retry 5 "$URL.md5" | awk '{print $1}')
      GOT=$(md5sum "$OUT" | awk '{print $1}')
      echo "md5 expected $EXPECT got $GOT"
      [ "$EXPECT" = "$GOT" ]
      ls -la "$OUT" ;;
    osm)
      $PY -m hoop_pipeline.osm_extract --pbf data/osm_us/us-latest.osm.pbf --state-geojson TN "$@" ;;
    naip)
      $PY - "$@" <<'EOF'
import json
import numpy as np
from hoop_pipeline import config
from hoop_pipeline.common import setup_logging, write_json
from hoop_pipeline.naip_index import NaipCatalog
setup_logging(0)
cat = NaipCatalog.load()
out = {}
for st in sorted(set(cat.states.tolist())):
    idx = np.flatnonzero(cat.states == st)
    years = {int(y): int(n) for y, n in zip(*np.unique(cat.years[idx], return_counts=True))}
    latest = max(years)
    li = idx[cat.years[idx] == latest]
    d = np.sort(cat.dates[li].astype("datetime64[D]"))
    out[st] = {"years": years, "latest": latest, "latest_dates": [str(d[0]), str(d[-1])],
               "latest_gsd": sorted({float(g) for g in cat.gsd[li]})}
write_json(config.NAIP_INDEX_DIR / "naip_summary.json", out)
print(json.dumps({k: [v["latest"], v["latest_gsd"]] for k, v in out.items()}))
EOF
      ;;
    dataset)
      $PY -m hoop_pipeline.build_dataset_us --out datasets/us --workers 64 "$@" ;;
    dataset2)
      $PY -m hoop_pipeline.build_dataset_us --out datasets/us2 --workers 96 --neg-ratio 2.0 \
        --neg-mix "pitch_tennis=0.22,pitch_pickleball=0.08,pitch_volleyball=0.05,pitch_other=0.05,parking=0.12,pool=0.07,playground=0.04,park=0.09,school=0.09,residential=0.07,apartments=0.04,random_land=0.08" \
        "$@" ;;
    train)
      $PY -m hoop_pipeline.train --data datasets/us/data.yaml --model yolo11s.pt --epochs 80 --imgsz 320 \
        --batch 256 --workers 16 --patience 15 --cache ram --name "$RUN_NAME" --set deterministic=False "$@" ;;
    eval)
      $PY -m hoop_pipeline.evaluate --weights weights/best.pt --data datasets/us/data.yaml --split val --out out/eval_tn "$@"
      $PY -m hoop_pipeline.evaluate --weights weights/best.pt --data datasets/us/data_val_us.yaml --split val \
        --out out/eval_us --montage 12 ;;
    scan_plan)
      $PY -m hoop_pipeline.scan_state plan --state TN "$@" ;;
    scan_fetch)
      $PY -m hoop_pipeline.scan_state fetch --state TN --workers 48 "$@" ;;
    scan_detect)
      $PY -m hoop_pipeline.scan_state detect --state TN --weights weights/best.pt --out out/candidates_tn.geojson "$@" ;;
    package)
      trap - ERR
      set +e
      cd ~/hh/pipeline
      mkdir -p out/cloud/logs
      cp -f "$LOGS"/* out/cloud/logs/ 2>/dev/null
      nvidia-smi > out/cloud/logs/nvidia-smi.txt 2>&1
      LIST=$(ls -d "$@" 2>/dev/null)
      tar czf ~/hh/results.tgz out/cloud/logs $LIST
      ls -la ~/hh/results.tgz ;;
    *)
      echo "unknown stage $STAGE" >&2
      exit 2 ;;
  esac
  date -u +%FT%TZ > "$LOGS/$MARK.done"
  echo "STAGE $MARK DONE"
}
main "$@"; exit $?
