#!/usr/bin/env bash
# Terminate instances, wait until they are gone, record terminate times and cost in cloud/last_run.json,
# and list whatever is still on the account.
#
#   bash cloud/terminate.sh main            # node name from last_run.json
#   bash cloud/terminate.sh <instance-id>
#   bash cloud/terminate.sh --all           # every live node in last_run.json AND anything else on the account
#
# The API key file is re-read on every call. If terminate answers 401/403 (key rotated mid-run), it keeps
# retrying with a fresh read of the key file for up to $HH_TERMINATE_RETRY_S seconds (default 600) and,
# if it still fails, prints "TERMINATE MANUALLY: instance <id>" and exits non-zero.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lambda_api.sh"

RETRY_S="${HH_TERMINATE_RETRY_S:-600}"
TMPJ="$(mktemp)"
trap 'rm -f "$TMPJ"' EXIT

ids=()
if [ "${1:-}" = "--all" ]; then
  while read -r name id ip; do [ -n "$id" ] && ids+=("$id"); done < <(lastrun live)
  CODE=$(lambda_api_code GET /instances "" "$TMPJ")
  if [ "$CODE" = "200" ]; then
    while read -r id; do
      [ -n "$id" ] && [[ ! " ${ids[*]-} " == *" $id "* ]] && ids+=("$id")
    done < <("$PY" -c 'import json,sys; [print(i["id"]) for i in json.load(open(sys.argv[1])).get("data", []) if i.get("status") != "terminated"]' "$TMPJ")
  fi
else
  KEY="${1:?node name, instance id or --all}"
  ID="$(lastrun get "$KEY" instance_id)"
  ids+=("${ID:-$KEY}")
fi
[ "${#ids[@]}" -gt 0 ] || { echo "nothing to terminate"; running_instances; exit 0; }

fail=0
for ID in "${ids[@]}"; do
  echo "terminating $ID"
  t0=$(date +%s)
  while :; do
    CODE=$(lambda_api_code POST /instance-operations/terminate "{\"instance_ids\":[\"$ID\"]}" "$TMPJ" || true)
    # print only id/status: the full response can carry per-instance tokens (e.g. Jupyter)
    "$PY" -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
for i in (d.get("data") or {}).get("terminated_instances", []):
    print("  ", i.get("id"), i.get("status"))
if "error" in d:
    print("   error:", d["error"].get("code") if isinstance(d["error"], dict) else d["error"])
' "$TMPJ"
    if [ "$CODE" = "200" ]; then break; fi
    if [ "$CODE" = "401" ] || [ "$CODE" = "403" ] || [ "$CODE" = "000" ] || [ "${CODE:0:1}" = "5" ]; then
      if [ $(( $(date +%s) - t0 )) -gt "$RETRY_S" ]; then
        echo "TERMINATE MANUALLY: instance $ID (terminate kept failing with HTTP $CODE)"; fail=1; break
      fi
      echo "   HTTP $CODE; re-reading the key file and retrying in 20 s"
      sleep 20
      continue
    fi
    # 404 etc: already gone or unknown id; verify below
    echo "   HTTP $CODE"
    break
  done
  for _ in $(seq 1 60); do
    CODE=$(lambda_api_code GET "/instances/$ID" "" "$TMPJ" || true)
    STATUS=$(json_field data.status < "$TMPJ" 2>/dev/null || true)
    if [ "$CODE" = "404" ] || [ "$STATUS" = "terminated" ]; then STATUS=terminated; break; fi
    echo "  status: ${STATUS:-?} (HTTP $CODE)"
    sleep 10
  done
  if [ "$STATUS" = "terminated" ]; then
    if [ -n "$(lastrun get "$ID" instance_id)" ] && [ "$(lastrun get "$ID" status)" != "terminated" ]; then
      lastrun set "$ID" status=terminated terminated_at=now
    fi
  else
    echo "TERMINATE MANUALLY: instance $ID (still '$STATUS' after waiting)"; fail=1
  fi
done
lastrun cost
echo "instances still on the account (GET /instances):"
running_instances
exit $fail
