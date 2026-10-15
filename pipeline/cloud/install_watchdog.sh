#!/usr/bin/env bash
# Install the self-terminating watchdog on node NAME (see cloud/watchdog_instance.py). launch.sh runs this as
# soon as the instance is active and terminates the instance if it fails, so no node runs unprotected.
#
#   bash cloud/install_watchdog.sh scan2              # config + key + systemd timer + cron fallback
#   bash cloud/install_watchdog.sh scan2 --key-only   # after rotating the API key: refresh the instance's copy
#   HH_IDLE_MIN=45 bash cloud/install_watchdog.sh main
#
# The deadline comes from last_run.json (launch.sh --max-hours). The API key is piped over SSH straight into a
# root-only file (/etc/hoophunter/lambda_api_key, 0600); it never touches argv, logs, the repo or ~ubuntu.
# This is the one place the key leaves this PC, an accepted trade-off for an instance that can kill itself.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lambda_api.sh"

NAME="${1:?node name}"
MODE="${2:-}"
export HH_NODE="$NAME"
[ -s "$LAMBDA_KEY_FILE" ] || { echo "missing API key file $LAMBDA_KEY_FILE" >&2; exit 1; }
IP="$(hh_ip)"
[ -n "$IP" ] || { echo "$NAME has no ip in last_run.json" >&2; exit 1; }

# SSH can lag a minute behind status=active
ok=0
for _ in $(seq 1 30); do
  if hh_ssh true 2>/dev/null; then ok=1; break; fi
  sleep 10
done
[ "$ok" = 1 ] || { echo "$NAME ($IP): ssh not reachable after 5 min" >&2; exit 1; }

push_key() {
  tr -d '\r\n' < "$LAMBDA_KEY_FILE" | hh_ssh "sudo install -d -o root -g root -m 700 /etc/hoophunter && sudo sh -c 'umask 077; cat > /etc/hoophunter/lambda_api_key'"
}

if [ "$MODE" = "--key-only" ]; then
  push_key
  echo "watchdog key refreshed on $NAME"
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
lastrun watchdog-config "$NAME" --idle-min "${HH_IDLE_MIN:-30}" --api "$LAMBDA_API" > "$TMP/watchdog.json"
hh_ssh 'mkdir -p ~/hh_watchdog'
hh_scp_up "$CLOUD_DIR/watchdog_instance.py" hh_watchdog/watchdog_instance.py
hh_scp_up "$CLOUD_DIR/watchdog_install.sh" hh_watchdog/watchdog_install.sh
hh_scp_up "$TMP/watchdog.json" hh_watchdog/watchdog.json
push_key
hh_ssh 'sed -i "s/\r$//" ~/hh_watchdog/watchdog_install.sh && sudo install -o root -g root -m 600 ~/hh_watchdog/watchdog.json /etc/hoophunter/watchdog.json && rm -f ~/hh_watchdog/watchdog.json && sudo bash ~/hh_watchdog/watchdog_install.sh'
lastrun set "$NAME" watchdog=installed watchdog_at=now
echo "watchdog installed on $NAME (deadline $(lastrun get "$NAME" deadline), idle ${HH_IDLE_MIN:-30} min)"
