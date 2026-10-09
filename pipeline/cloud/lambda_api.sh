#!/usr/bin/env bash
# Shared helpers for the Lambda Cloud scripts. Source it, don't run it.
#
# The API key is read from $LAMBDA_KEY_FILE (default ~/.config/hoop-hunter/lambda_api_key)
# and handed to curl on stdin (-H @-), so it never shows up in argv, logs or the repo.
# Nothing here copies the key to the instance; the instance never needs it.

LAMBDA_API="${LAMBDA_API:-https://cloud.lambda.ai/api/v1}"
LAMBDA_KEY_FILE="${LAMBDA_KEY_FILE:-$HOME/.config/hoop-hunter/lambda_api_key}"
CLOUD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$(cd "$CLOUD_DIR/.." && pwd)"
LAST_RUN="$CLOUD_DIR/last_run.json"
SSH_KEY="${HH_SSH_KEY:-$HOME/.ssh/hoophunter_lambda}"
SSH_KEY_NAME="${HH_SSH_KEY_NAME:-hoophunter-claude}"
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ConnectTimeout=20)

if [ -z "${PY:-}" ]; then
  if [ -x "$PIPELINE_DIR/.venv/Scripts/python.exe" ]; then PY="$PIPELINE_DIR/.venv/Scripts/python.exe"
  elif [ -x "$PIPELINE_DIR/.venv/bin/python" ]; then PY="$PIPELINE_DIR/.venv/bin/python"
  else PY=python3; fi
fi

lambda_api() {  # METHOD PATH [JSON_BODY] -> response body on stdout
  local method="$1" path="$2" body="${3:-}"
  if [ ! -s "$LAMBDA_KEY_FILE" ]; then echo "missing API key file $LAMBDA_KEY_FILE" >&2; return 1; fi
  if [ -n "$body" ]; then
    printf 'Authorization: Bearer %s\n' "$(tr -d '\r\n' < "$LAMBDA_KEY_FILE")" |
      curl -sS -m 60 -X "$method" -H @- -H "Content-Type: application/json" --data "$body" "$LAMBDA_API$path"
  else
    printf 'Authorization: Bearer %s\n' "$(tr -d '\r\n' < "$LAMBDA_KEY_FILE")" |
      curl -sS -m 60 -X "$method" -H @- "$LAMBDA_API$path"
  fi
}

lastrun() { "$PY" "$CLOUD_DIR/lastrun.py" "$@"; }
json_field() { "$PY" "$CLOUD_DIR/lastrun.py" field "$1"; }   # stdin JSON -> dotted field

hh_ip() { lastrun get ip; }
hh_ssh() { ssh "${SSH_OPTS[@]}" "ubuntu@$(hh_ip)" "$@"; }
hh_scp_up() { scp "${SSH_OPTS[@]}" "$1" "ubuntu@$(hh_ip):$2"; }            # LOCAL REMOTE
hh_scp_down() { scp "${SSH_OPTS[@]}" -r "ubuntu@$(hh_ip):$1" "$2"; }       # REMOTE LOCAL

running_instances() {  # prints "id name status type" per instance on the account
  lambda_api GET /instances | "$PY" -c '
import json, sys
for i in json.load(sys.stdin).get("data", []):
    print(i.get("id"), i.get("name"), i.get("status"), (i.get("instance_type") or {}).get("name"), i.get("region", {}).get("name"))
'
}
