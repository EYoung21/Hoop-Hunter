#!/usr/bin/env bash
# Copy results from node $HH_NODE (default "main") into the git-ignored local folders (weights/, out/, data/).
# Chips, cached imagery and the big context layers stay on the instance.
#
#   bash cloud/fetch_results.sh                 # the v2 default list below
#   bash cloud/fetch_results.sh out/scan2_tn weights/best_v2.pt   # explicit paths (relative to ~/hh/pipeline)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lambda_api.sh"

if [ "$#" -gt 0 ]; then
  PATHS=("$@")
else
  PATHS=(weights/best_v2.pt weights/best_v2.json weights/context_v2.pt weights/context_v2.json weights/verifier_v2.pt
         weights/verifier_v2.json runs/us2-y11s-416/results.csv runs/us2-y11s-416/results.png runs/us2-y11s-416/args.yaml
         out/eval_tn_v2 out/eval_tn_v2_tta out/eval_tn_v1 out/eval_us_v2 out/eval_us_v1 out/clean/summary.json
         out/clean/select_stats.json out/clean/montage_groups.json out/clean/removed_labels.txt out/clean/excluded_labels.txt
         out/scan2_tn out/review_v2 out/bench_fastdet.json out/bench_fastdet_v2.json out/bench_fastdet_trt.json
         out/context_eval_tn.json out/context_eval_val.json out/verifier_eval.json out/privacy_eval_osm_TN.json
         out/privacy_eval_osm_TN_rules.json out/privacy_eval_osm_TN_rules_cnn.json out/osm_privacy_TN.json
         out/osm_privacy_TN_detail.json data/gold/preds.json data/gold/preds_v1.json data/gold/tiles.json
         data/osm_us/summary.json data/osm_us/courts_TN.geojson data/sites/sources.json data/sites/buffer_eval_TN.json
         data/scan2/TN/plan_stats.json data/scan2/TN/manifests data/scan2/US/plan_stats.json datasets/us2/stats.json
         out/gold_label_data.zip out/label_tool/data/tasks.js out/review_gold out/disagree_tn out/verifier_eval_v1.json
         out/bench_fastdet_trt_v1.json out/scan2_tn_v1 out/eval_tn_v2_engine)
fi
hh_ssh "bash ~/hh/pipeline/cloud/run_remote.sh package ${PATHS[*]}"
mkdir -p "$PIPELINE_DIR/out/cloud"
hh_scp_down "hh/results.tgz" "$PIPELINE_DIR/out/cloud/results_v2.tgz"
(cd "$PIPELINE_DIR" && tar xzf out/cloud/results_v2.tgz && ls -la out/cloud/results_v2.tgz)
echo "results unpacked into $PIPELINE_DIR (weights/, out/, data/)"
