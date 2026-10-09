#!/usr/bin/env bash
# Copy results from the instance into the git-ignored local folders:
#   weights/   best.pt + best.json
#   out/       eval, TN candidates + crops, training run (curves, results.csv), cloud logs
#   data/      osm_us layers (GeoParquet), summaries, dataset manifest/stats, scan plan stats
# Chips and cached imagery blocks stay on the instance.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lambda_api.sh"

hh_ssh 'bash ~/hh/pipeline/cloud/run_remote.sh package'
mkdir -p "$PIPELINE_DIR/out/cloud"
hh_scp_down "hh/results.tgz" "$PIPELINE_DIR/out/cloud/results.tgz"
(cd "$PIPELINE_DIR" && tar xzf out/cloud/results.tgz && ls -la out/cloud/results.tgz)
echo "results unpacked into $PIPELINE_DIR (weights/, out/, data/)"
