#!/usr/bin/env bash
# Run ON the post-processing node of the national run (cloud/start_stage.sh job ... bash cloud/national_post.sh PART):
#   prep     Overture buildings for every state, per-state site slices, OSM per-state GeoJSON, OSM p_home
#            (needs data/sites/{sites,residential}_us.parquet, data/osm_us/courts_us.parquet, the NAIP catalog)
#   merge    scan_engine merge of every node's packs (data/national/packed) -> out/national/detections.parquet
#   rescore  fresh NAIP chips for candidates >= HH_RESCORE_MIN (default 0.4): v2 again, verifier, context CNN
#   privacy  per-state privacy classes (rules + p_home) for candidates and OSM features, then summarize
#   all      merge + rescore + privacy
set -uo pipefail
cd ~/hh/pipeline
PY=~/hhvenv/bin/python
export PYTHONPATH=~/hh/pipeline
PART="${1:?prep|merge|rescore|privacy|all}"
step() { echo "== $* $(date -u +%FT%TZ)"; "$@" || { echo "STEP FAILED: $*"; return 1; }; }

if [ "$PART" = "prep" ]; then
  step $PY -m hoop_pipeline.national osm-geojson || exit 1
  step $PY -m hoop_pipeline.national split-sites || exit 1
  step $PY -m hoop_pipeline.national osm-phome --workers 128 || exit 1
  step $PY -m hoop_pipeline.sites buildings --states CONUS --workers "${HH_BUILDING_WORKERS:-6}" || exit 1
  ls data/sites/buildings_*.parquet | wc -l
  echo "PREP_DONE"
fi
if [ "$PART" = "merge" ] || [ "$PART" = "all" ]; then
  step $PY -m hoop_pipeline.scan_engine merge --plan data/scan2/US --packed data/national/packed --out out/national \
    --v1-mask none || exit 1
  echo "MERGE_DONE"
fi
if [ "$PART" = "rescore" ] || [ "$PART" = "all" ]; then
  step $PY -m hoop_pipeline.rescore --candidates out/national/detections.parquet --min-conf "${HH_RESCORE_MIN:-0.4}" \
    --weights weights/best_v2.pt --imgsz 416 --verifier weights/verifier_v2.pt --context-model weights/context_v2.pt \
    --chips-dir data/national/rescore_chips --chunk 4096 --batch 256 --workers 128 \
    ${HH_RESCORE_SHARD:+--shard "$HH_RESCORE_SHARD"} --out out/national/rescore${HH_RESCORE_SHARD:+_${HH_RESCORE_SHARD/\//of}}.parquet \
    || exit 1
  echo "RESCORE_DONE"
fi
if [ "$PART" = "privacy" ] || [ "$PART" = "all" ]; then
  step $PY -m hoop_pipeline.national privacy --rescore out/national/rescore.parquet \
    --osm-phome data/national/osm_phome.parquet --workers "${HH_PRIVACY_WORKERS:-16}" || exit 1
  step $PY -m hoop_pipeline.national summarize || exit 1
  echo "PRIVACY_DONE"
fi
