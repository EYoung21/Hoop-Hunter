#!/usr/bin/env bash
# Launch ONE single-GPU Lambda Cloud instance as node NAME, record it in cloud/last_run.json the moment the
# API returns its id, and install the self-terminating watchdog before anything else runs on it.
#
#   bash cloud/launch.sh main --max-hours 6                    # default type list, default 6 h lifetime
#   bash cloud/launch.sh scan2 gpu_1x_a100_sxm4 --max-hours 3   # explicit preference list (comma-separated)
#   HH_REGIONS=us-east-1 bash cloud/launch.sh scan3              # restrict regions
#
# Safety: --max-hours (default 6, at most 168) sets a hard deadline. It is recorded in last_run.json and
# encoded in the instance name ("hh-<NAME>-xYYMMDDHHMM", UTC), so cloud/watchdog.py can enforce it from any
# machine. On the instance, cloud/watchdog_instance.py terminates the node at the deadline or after 30 min
# idle. If the watchdog cannot be installed, the instance is terminated at once.
#
# Before launching it checks that the API key file works (GET /instance-types -> 200; the key may
# have been rotated), refuses if NAME is already live, and prints the run's cost so far.
# Only single-GPU types are accepted. Uses the SSH key registered on the account ($HH_SSH_KEY_NAME).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lambda_api.sh"

MAX_HOURS=6
POS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --max-hours) MAX_HOURS="${2:?--max-hours needs a value}"; shift 2 ;;
    --max-hours=*) MAX_HOURS="${1#*=}"; shift ;;
    *) POS+=("$1"); shift ;;
  esac
done
NAME="${POS[0]:?node name, e.g. main}"
TYPES="${POS[1]:-gpu_1x_a100_sxm4,gpu_1x_a10,gpu_1x_h100_sxm5}"
# us-east first: closest to Geofabrik (DE) and the NAIP blobs (Azure West Europe)
REGIONS="${HH_REGIONS:-us-east-1,us-east-2,us-east-3,us-south-1,us-south-2,us-south-3,us-midwest-1,us-west-1,us-west-2,us-west-3,us-southeast-1}"
case ",$TYPES," in *gpu_[2-9]x*|*gpu_[1-9][0-9]x*) echo "multi-GPU types are not allowed: $TYPES" >&2; exit 1 ;; esac

if [ "$(lastrun get "$NAME" status)" != "" ] && [ "$(lastrun get "$NAME" status)" != "terminated" ]; then
  echo "node $NAME is still recorded as $(lastrun get "$NAME" status) ($(lastrun get "$NAME" instance_id)); terminate it first" >&2
  exit 1
fi
read -r DEADLINE SUFFIX < <(lastrun deadline --max-hours "$MAX_HOURS") || true
[ -n "${SUFFIX:-}" ] || { echo "bad --max-hours $MAX_HOURS (must be in (0, 168])" >&2; exit 1; }
API_NAME="hh-$NAME-$SUFFIX"
check_key
lastrun cost | tail -1

TMPJ="$(mktemp)"
trap 'rm -f "$TMPJ"' EXIT
CODE=$(lambda_api_code GET /instance-types "" "$TMPJ")
[ "$CODE" = "200" ] || { echo "GET /instance-types -> $CODE" >&2; exit 1; }
read -r ITYPE REGION PRICE < <(lastrun pick --types "$TYPES" --regions "$REGIONS" < "$TMPJ")
echo "launching $NAME as $API_NAME: $ITYPE in $REGION at \$$PRICE/h, max lifetime $MAX_HOURS h (until $DEADLINE)"
BODY=$(printf '{"region_name":"%s","instance_type_name":"%s","ssh_key_names":["%s"],"name":"%s"}' \
  "$REGION" "$ITYPE" "$SSH_KEY_NAME" "$API_NAME")
RESP=$(lambda_api POST /instance-operations/launch "$BODY")
ID=$(printf '%s' "$RESP" | json_field data.instance_ids.0 || true)
if [ -z "$ID" ]; then echo "launch failed: $(printf '%s' "$RESP" | json_field error)" >&2; exit 1; fi
lastrun add "$NAME" instance_id="$ID" instance_type="$ITYPE" region="$REGION" price_per_hour="$PRICE" \
  launched_at=now status=booting ip= api_name="$API_NAME" max_hours="$MAX_HOURS" deadline="$DEADLINE"
echo "instance $ID ($NAME) launched and recorded; waiting for it to become active"

STATUS=""
for _ in $(seq 1 120); do
  CODE=$(lambda_api_code GET "/instances/$ID" "" "$TMPJ" || true)
  STATUS=$(json_field data.status < "$TMPJ" 2>/dev/null || true)
  IP=$(json_field data.ip < "$TMPJ" 2>/dev/null || true)
  if [ "$STATUS" = "active" ] && [ -n "$IP" ]; then
    lastrun set "$NAME" status=active ip="$IP" active_at=now
    echo "active: $NAME $IP"
    if [ "${HH_NO_WATCHDOG:-0}" = "1" ]; then
      echo "WARNING: HH_NO_WATCHDOG=1, no on-instance watchdog; only cloud/watchdog.py protects $NAME" >&2
      exit 0
    fi
    if bash "$CLOUD_DIR/install_watchdog.sh" "$NAME"; then
      echo "reminder: keep the local backstop running too (python cloud/watchdog.py --loop 10, see README)"
      exit 0
    fi
    echo "watchdog install failed on $NAME; terminating it rather than leaving it unprotected" >&2
    bash "$CLOUD_DIR/terminate.sh" "$NAME" || true
    exit 1
  fi
  if [ "$STATUS" = "terminated" ] || [ "$STATUS" = "unhealthy" ]; then
    echo "instance went $STATUS; terminating it" >&2
    bash "$CLOUD_DIR/terminate.sh" "$NAME" || true
    exit 1
  fi
  sleep 15
done
echo "instance $ID not active after 30 min (status '$STATUS', last HTTP $CODE); terminating it" >&2
bash "$CLOUD_DIR/terminate.sh" "$NAME" || true
exit 1
