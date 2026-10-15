#!/usr/bin/env bash
# Run ON the main instance after every node's Tennessee results are collected into data/scan2/TN/results:
#   bash cloud/start_stage.sh job post_scan bash cloud/post_scan_tn.sh
# merge -> privacy classification (rules + context CNN) -> stratified review sample (eval cells) -> review crops
set -uo pipefail
cd ~/hh/pipeline
PY=~/hhvenv/bin/python
export PYTHONPATH=.
$PY -m hoop_pipeline.scan_engine merge --plan data/scan2/TN --out out/scan2_tn
$PY -m hoop_pipeline.privacy classify --candidates out/scan2_tn/detections.parquet --scope TN \
  --buildings data/sites/buildings_TN.parquet --context-model weights/context_v2.pt --context-min-conf 0.25 \
  --region-label tennessee --model-name best_v2.pt --out out/scan2_tn/detections_privacy.parquet
$PY cloud/sample_review.py out/scan2_tn/detections_privacy.parquet --out out/review_v2/sample.geojson --per-band 34
$PY cloud/review_crops.py out/review_v2/sample.geojson --out out/review_v2/crops --crop-px 320
echo POST_SCAN_DONE
