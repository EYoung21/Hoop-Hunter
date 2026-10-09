#!/usr/bin/env bash
# Terminate the instance recorded in cloud/last_run.json (or the id given), wait until it is gone,
# record the terminate time and estimated cost, and list whatever is still running on the account.
#
#   bash cloud/terminate.sh            # instance from last_run.json
#   bash cloud/terminate.sh <id>
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lambda_api.sh"

ID="${1:-$(lastrun get instance_id)}"
if [ -z "$ID" ]; then echo "no instance id given or recorded" >&2; exit 1; fi
echo "terminating $ID"
# print only id/status: the full response also carries per-instance tokens (e.g. Jupyter)
lambda_api POST /instance-operations/terminate "{\"instance_ids\":[\"$ID\"]}" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
for i in (d.get("data") or {}).get("terminated_instances", []):
    print("  ", i.get("id"), i.get("status"))
if "error" in d:
    print("  error:", d["error"])
'
for _ in $(seq 1 60); do
  STATUS=$(lambda_api GET "/instances/$ID" | json_field data.status || true)
  if [ -z "$STATUS" ] || [ "$STATUS" = "terminated" ]; then break; fi
  echo "  status: $STATUS"
  sleep 10
done
if [ "$(lastrun get instance_id)" = "$ID" ]; then
  lastrun set status=terminated terminated_at=now
  lastrun cost
fi
echo "instances still on the account (GET /instances):"
running_instances || true
