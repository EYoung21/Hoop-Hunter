#!/usr/bin/env bash
# Copy the pipeline code (no data, venvs or results) plus the Census state boundaries
# to ~/hh/pipeline on node $HH_NODE (default "main") recorded in last_run.json.
#   HH_NODE=scan2 bash cloud/sync_code.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lambda_api.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
(cd "$PIPELINE_DIR" && tar czf "$TMP/hh_code.tgz" \
  hoop_pipeline/*.py regions.json requirements.txt pyproject.toml README.md \
  cloud/*.sh cloud/*.py cloud/requirements-cloud.txt data/census/cb_2023_us_state_500k.zip)
hh_ssh 'mkdir -p ~/hh/pipeline ~/hh/logs'
hh_scp_up "$TMP/hh_code.tgz" "hh/hh_code.tgz"
hh_ssh 'cd ~/hh/pipeline && tar xzf ../hh_code.tgz && sed -i "s/\r$//" cloud/*.sh && ls hoop_pipeline | wc -l'
echo "code synced to $HH_NODE $(hh_ip):~/hh/pipeline"
