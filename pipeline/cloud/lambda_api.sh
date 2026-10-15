#!/usr/bin/env bash
# Shared helpers for the Lambda Cloud scripts. Source it, don't run it.
#
# The API key is read from $LAMBDA_KEY_FILE (default ~/.config/hoop-hunter/lambda_api_key) fresh on
# EVERY call (the key may be rotated at any time; the file is the only source of truth) and handed to
# curl on stdin (-H @-), so it never shows up in argv, logs or the repo. The only copy that leaves this PC is
# the one install_watchdog.sh pipes into a root-only file on each instance, so the instance can terminate itself.
#
# Several instances can be live at once (cloud/last_run.json keeps one record per node name).
# Node-aware helpers take the node name from $HH_NODE (default "main"):
#   HH_NODE=scan2 hh_ssh 'nvidia-smi'

LAMBDA_API="${LAMBDA_API:-https://cloud.lambda.ai/api/v1}"
LAMBDA_KEY_FILE="${LAMBDA_KEY_FILE:-$HOME/.config/hoop-hunter/lambda_api_key}"
CLOUD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$(cd "$CLOUD_DIR/.." && pwd)"
LAST_RUN="$CLOUD_DIR/last_run.json"
SSH_KEY="${HH_SSH_KEY:-$HOME/.ssh/hoophunter_lambda}"
SSH_KEY_NAME="${HH_SSH_KEY_NAME:-hoophunter-claude}"
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ConnectTimeout=20)
HH_NODE="${HH_NODE:-main}"

if [ -z "${PY:-}" ]; then
  if [ -x "$PIPELINE_DIR/.venv/Scripts/python.exe" ]; then PY="$PIPELINE_DIR/.venv/Scripts/python.exe"
  elif [ -x "$PIPELINE_DIR/.venv/bin/python" ]; then PY="$PIPELINE_DIR/.venv/bin/python"
  else PY=python3; fi
fi

_auth_header() {  # reads the key file fresh; prints the header line for curl -H @-
  printf 'Authorization: Bearer %s\n' "$(tr -d '\r\n' < "$LAMBDA_KEY_FILE")"
}

lambda_api() {  # METHOD PATH [JSON_BODY] -> response body on stdout
  local method="$1" path="$2" body="${3:-}"
  if [ ! -s "$LAMBDA_KEY_FILE" ]; then echo "missing API key file $LAMBDA_KEY_FILE" >&2; return 1; fi
  if [ -n "$body" ]; then
    _auth_header | curl -sS -m 60 -X "$method" -H @- -H "Content-Type: application/json" --data "$body" "$LAMBDA_API$path"
  else
    _auth_header | curl -sS -m 60 -X "$method" -H @- "$LAMBDA_API$path"
  fi
}

lambda_api_code() {  # METHOD PATH [JSON_BODY] OUTFILE -> HTTP status code on stdout, body in OUTFILE
  local method="$1" path="$2" body="${3:-}" out="${4:-/dev/null}"
  if [ ! -s "$LAMBDA_KEY_FILE" ]; then echo "000"; return 1; fi
  if [ -n "$body" ]; then
    _auth_header | curl -sS -m 60 -o "$out" -w '%{http_code}' -X "$method" -H @- -H "Content-Type: application/json" \
      --data "$body" "$LAMBDA_API$path"
  else
    _auth_header | curl -sS -m 60 -o "$out" -w '%{http_code}' -X "$method" -H @- "$LAMBDA_API$path"
  fi
}

check_key() {  # the key works iff GET /instance-types answers 200; run before every launch
  local code
  code=$(lambda_api_code GET /instance-types "" /dev/null || true)
  if [ "$code" != "200" ]; then echo "API key check failed: GET /instance-types -> HTTP $code" >&2; return 1; fi
  echo "API key OK (GET /instance-types -> 200)"
}

lastrun() { "$PY" "$CLOUD_DIR/lastrun.py" "$@"; }
json_field() { "$PY" "$CLOUD_DIR/lastrun.py" field "$1"; }   # stdin JSON -> dotted field

hh_ip() { lastrun get "${1:-$HH_NODE}" ip; }
hh_ssh() { ssh "${SSH_OPTS[@]}" "ubuntu@$(hh_ip)" "$@"; }
hh_scp_up() { scp "${SSH_OPTS[@]}" "$1" "ubuntu@$(hh_ip):$2"; }            # LOCAL REMOTE
hh_scp_down() { scp "${SSH_OPTS[@]}" -r "ubuntu@$(hh_ip):$1" "$2"; }       # REMOTE LOCAL

running_instances() {  # prints "id name status type region" per instance on the account
  lambda_api GET /instances | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
if "error" in d:
    print("ERROR", d["error"])
for i in d.get("data", []):
    print(i.get("id"), i.get("name"), i.get("status"), (i.get("instance_type") or {}).get("name"), i.get("region", {}).get("name"))
print("total instances on account:", len(d.get("data", [])))
'
}
