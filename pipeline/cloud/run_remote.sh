#!/usr/bin/env bash
# Run ON the instance: one pipeline stage, with start/done/failed markers in ~/hh/logs.
# Normally started in the background by cloud/start_stage.sh; extra args go to the Python step.
#
# Stages, in order:
#   setup        apt + venv + pip (setup_remote.sh)
#   pbf          download the Geofabrik US extract (~12 GB), verify md5
#   osm          osm_extract -> data/osm_us/{courts_us,indoor_us,context_us}.parquet, courts_TN.geojson
#   naip         NAIP GeoParquet catalog + per-state year summary
#   dataset      build_dataset_us -> datasets/us   (pilot: dataset --pilot 2000 --out datasets/us_pilot)
#   train        YOLO training -> weights/best.pt, runs/<name>/
#   scan_plan    TN mask + tile plan; scan_fetch: cache TN imagery blocks (can overlap training)
#   eval         evaluate on the TN holdout (val) and the val_us slice
#   scan_detect  detector over cached TN blocks -> out/candidates_tn.geojson + crops
#   post_train   eval + scan_detect + 640 px re-tiled scan + inference benchmark (GPU work after training)
#   package      tar the results for cloud/fetch_results.sh
#
# The whole script is one function that is called on the last line: bash parses it
# completely before running anything, so editing this file while a stage runs is safe.
# (bash reads plain scripts incrementally; an in-place edit during a long stage once made
# the stage die after its job finished, before writing its .done marker.)
set -E
main() {
  set -uo pipefail
  STAGE="${1:?stage}"
  shift
  cd ~/hh/pipeline
  PY=~/hhvenv/bin/python
  LOGS=~/hh/logs
  mkdir -p "$LOGS" data/osm_us
  date -u +%FT%TZ > "$LOGS/$STAGE.start"
  trap 'date -u +%FT%TZ > "$LOGS/$STAGE.failed"; echo "STAGE $STAGE FAILED"' ERR
  set -e
  RUN_NAME="${HH_RUN_NAME:-us-y11s}"

  case "$STAGE" in
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
from collections import defaultdict
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
    post_train)
      # everything that needs the GPU after training, in order
      bash cloud/run_remote.sh eval
      bash cloud/run_remote.sh scan_detect --crops 300
      $PY -m hoop_pipeline.scan_state detect --state TN --weights weights/best.pt --retile 640 \
        --out out/candidates_tn_640.geojson --crops 0 --no-tag-kinds
      timeout 1500 env PYTHONPATH=. $PY cloud/bench_infer.py --trt || echo "bench_infer exited with $?" ;;
    package)
      trap - ERR
      set +e
      cd ~/hh/pipeline
      mkdir -p out/cloud/logs data/dataset_us
      cp -f "$LOGS"/* out/cloud/logs/ 2>/dev/null
      nvidia-smi > out/cloud/logs/nvidia-smi.txt 2>&1
      cp -f datasets/us/manifest.csv.gz datasets/us/stats.json datasets/us/data.yaml data/dataset_us/ 2>/dev/null
      cp -rf datasets/us/previews data/dataset_us/ 2>/dev/null
      LIST=$(ls -d weights/best.pt weights/best.json runs/*/results.csv runs/*/results.png runs/*/*.png runs/*/*.jpg \
        runs/*/args.yaml runs/*/hoop_summary.json out/eval_tn out/eval_us out/candidates_tn.geojson \
        out/candidates_tn_640.geojson out/candidates_tn_crops \
        out/candidates_tn_crops_sheet_*.jpg out/bench_*.json out/cloud data/osm_us/courts_us.parquet \
        data/osm_us/indoor_us.parquet data/osm_us/courts_TN.geojson data/osm_us/summary.json \
        data/naip_index/naip_summary.json data/dataset_us data/scan/TN/plan_stats.json data/scan/TN/fetch_stats.json \
        data/scan/TN/mask.geojson 2>/dev/null)
      tar czf ~/hh/results.tgz $LIST
      ls -la ~/hh/results.tgz ;;
    *)
      echo "unknown stage $STAGE" >&2
      exit 2 ;;
  esac
  date -u +%FT%TZ > "$LOGS/$STAGE.done"
  echo "STAGE $STAGE DONE"
}
main "$@"; exit $?
