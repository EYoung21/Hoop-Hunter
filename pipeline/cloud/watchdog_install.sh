#!/usr/bin/env bash
# Run ON the instance as root (sudo): install or refresh the self-terminating watchdog (watchdog_instance.py).
#   sudo bash cloud/watchdog_install.sh          # from ~/hh/pipeline (setup_remote.sh does this)
#   sudo bash ~/hh_watchdog/watchdog_install.sh  # what install_watchdog.sh runs right after launch
#
# Needs /etc/hoophunter/watchdog.json and /etc/hoophunter/lambda_api_key, which only the local
# cloud/install_watchdog.sh writes (launch.sh calls it as soon as the instance is active). Exits non-zero
# when either is missing, so setup_remote.sh refuses to prepare an unprotected instance.
# Idempotent: a systemd timer every 2 min plus a cron fallback every 5 min; a file lock keeps runs apart.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF=/etc/hoophunter/watchdog.json
KEY=/etc/hoophunter/lambda_api_key
if [ "$(id -u)" != "0" ]; then echo "run as root (sudo)" >&2; exit 1; fi
[ -s "$CONF" ] || { echo "watchdog: $CONF missing (run cloud/install_watchdog.sh NODE locally)" >&2; exit 1; }
[ -s "$KEY" ] || { echo "watchdog: $KEY missing (run cloud/install_watchdog.sh NODE locally)" >&2; exit 1; }
chown root:root /etc/hoophunter "$CONF" "$KEY"
chmod 700 /etc/hoophunter
chmod 600 "$CONF" "$KEY"
install -d -m 755 /opt/hoophunter
install -d -m 700 /var/lib/hoophunter
install -m 755 "$SRC/watchdog_instance.py" /opt/hoophunter/watchdog_instance.py
PY3="$(command -v python3)"

cat > /etc/systemd/system/hh-watchdog.service <<EOF
[Unit]
Description=Hoop Hunter watchdog: terminate this Lambda instance at max lifetime or after 30 min idle
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=$PY3 /opt/hoophunter/watchdog_instance.py check
TimeoutStartSec=900
EOF
cat > /etc/systemd/system/hh-watchdog.timer <<'EOF'
[Unit]
Description=Run the Hoop Hunter watchdog every 2 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
AccuracySec=15s
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now hh-watchdog.timer
# fallback in case the timer is stopped or systemd misbehaves; the script's lock prevents overlapping runs
echo "*/5 * * * * root $PY3 /opt/hoophunter/watchdog_instance.py check >/dev/null 2>&1" > /etc/cron.d/hh-watchdog
chmod 644 /etc/cron.d/hh-watchdog
# the ubuntu user may set holds (never past the deadline)
install -d -o ubuntu -g ubuntu -m 755 /home/ubuntu/hh 2>/dev/null || true
systemctl list-timers hh-watchdog.timer --no-pager | head -3
"$PY3" /opt/hoophunter/watchdog_instance.py status
echo "watchdog installed"
