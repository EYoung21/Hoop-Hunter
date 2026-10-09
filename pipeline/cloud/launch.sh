#!/usr/bin/env bash
# Launch ONE single-GPU Lambda Cloud instance and record it in cloud/last_run.json.
#
#   bash cloud/launch.sh                       # first type with capacity from the default list
#   bash cloud/launch.sh gpu_1x_a10            # explicit preference list (comma-separated)
#
# Refuses to launch while last_run.json records an instance that isn't terminated.
# Uses the SSH key already registered on the account ($HH_SSH_KEY_NAME, default hoophunter-claude).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lambda_api.sh"

TYPES="${1:-gpu_1x_a100_sxm4,gpu_1x_a10,gpu_1x_h100_sxm5}"
# us-east first: closest to Geofabrik (DE) and NAIP blobs (Azure West Europe)
REGIONS="${HH_REGIONS:-us-east-1,us-east-2,us-east-3,us-south-1,us-south-2,us-south-3,us-midwest-1,us-west-1,us-west-2,us-west-3,us-southeast-1}"

if [ -f "$LAST_RUN" ] && [ -n "$(lastrun get instance_id)" ] && [ "$(lastrun get status)" != "terminated" ]; then
  echo "last_run.json still records instance $(lastrun get instance_id) ($(lastrun get status)); run cloud/terminate.sh first" >&2
  exit 1
fi

read -r ITYPE REGION PRICE < <(lambda_api GET /instance-types | lastrun pick --types "$TYPES" --regions "$REGIONS")
echo "launching $ITYPE in $REGION at \$$PRICE/h"
BODY=$(printf '{"region_name":"%s","instance_type_name":"%s","ssh_key_names":["%s"],"name":"hoophunter-train"}' \
  "$REGION" "$ITYPE" "$SSH_KEY_NAME")
RESP=$(lambda_api POST /instance-operations/launch "$BODY")
ID=$(printf '%s' "$RESP" | json_field data.instance_ids.0 || true)
if [ -z "$ID" ]; then echo "launch failed: $RESP" >&2; exit 1; fi
rm -f "$LAST_RUN"
lastrun set instance_id="$ID" instance_type="$ITYPE" region="$REGION" price_per_hour="$PRICE" \
  launched_at=now status=booting ip= name=hoophunter-train
echo "instance $ID launched; waiting for it to become active"

for _ in $(seq 1 120); do
  INFO=$(lambda_api GET "/instances/$ID" || true)
  STATUS=$(printf '%s' "$INFO" | json_field data.status || true)
  IP=$(printf '%s' "$INFO" | json_field data.ip || true)
  if [ "$STATUS" = "active" ] && [ -n "$IP" ]; then
    lastrun set status=active ip="$IP" active_at=now
    echo "active: $IP"
    exit 0
  fi
  if [ "$STATUS" = "terminated" ] || [ "$STATUS" = "unhealthy" ]; then
    lastrun set status="$STATUS"
    echo "instance went $STATUS" >&2
    exit 1
  fi
  sleep 15
done
echo "instance $ID not active after 30 min (status '$STATUS'); terminate it with cloud/terminate.sh" >&2
exit 1
