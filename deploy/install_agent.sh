#!/usr/bin/env bash
# Install or update Brilliantly Local on one panel.
#
# Usage: SSHPASS='<panel root password>' ./install_agent.sh [options] <panel-ip>
#
# Options:
#   --wifi-powersave-off  Also install a timer that keeps this panel's Wi-Fi power save off
#                         (lower latency, fewer dropped panel links). Opt-in; affects only
#                         the panel you run it against.
#   --no-agent            Skip the agent (e.g. to apply only --wifi-powersave-off to other panels).
set -euo pipefail

agent=1
wifi=0
host=""
for arg in "$@"; do
  case "$arg" in
    --wifi-powersave-off) wifi=1 ;;
    --no-agent) agent=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) host="$arg" ;;
  esac
done
[ -n "$host" ] || { sed -n '2,10p' "$0" >&2; exit 2; }
[ "$agent" = 1 ] || [ "$wifi" = 1 ] || { echo "--no-agent without --wifi-powersave-off does nothing" >&2; exit 2; }
: "${SSHPASS:?export SSHPASS with the panel root password}"

here="$(cd "$(dirname "$0")" && pwd)"
ssh_opts=(-o NumberOfPasswordPrompts=1 -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

files=()
[ "$agent" = 1 ] && files+=("$here/../agent/brilliant_local_agent.py" "$here/brilliant-local.service")
[ "$wifi" = 1 ] && files+=("$here/brilliant-local-wifi.service" "$here/brilliant-local-wifi.timer")
for f in "${files[@]}"; do
  sshpass -e scp -q "${ssh_opts[@]}" "$f" "root@$host:/tmp/$(basename "$f")"
done

sshpass -e ssh "${ssh_opts[@]}" "root@$host" "AGENT=$agent WIFI=$wifi sh -s" <<'REMOTE'
set -e
if [ "$AGENT" = 1 ]; then
  install -d -m 700 /var/brilliant-local /var/brilliant-local/app
  install -m 644 /tmp/brilliant_local_agent.py /var/brilliant-local/app/brilliant_local_agent.py
  install -m 644 /tmp/brilliant-local.service /etc/systemd/system/brilliant-local.service
  rm -f /tmp/brilliant_local_agent.py /tmp/brilliant-local.service
  if [ ! -s /var/brilliant-local/token ]; then
    ( umask 077; /data/switch-embedded/env/bin/python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /var/brilliant-local/token )
  fi
fi
if [ "$WIFI" = 1 ]; then
  for unit in brilliant-local-wifi.service brilliant-local-wifi.timer; do
    install -m 644 /tmp/$unit /etc/systemd/system/$unit
    rm -f /tmp/$unit
  done
fi
systemctl daemon-reload
if [ "$WIFI" = 1 ]; then
  systemctl enable brilliant-local-wifi.timer >/dev/null 2>&1
  systemctl start brilliant-local-wifi.timer brilliant-local-wifi.service
  echo "Wi-Fi: $(iw dev wlan0 get power_save)"
fi
if [ "$AGENT" = 1 ]; then
  systemctl enable brilliant-local.service >/dev/null 2>&1
  systemctl restart brilliant-local.service
  sleep 4
  systemctl --no-pager --lines=15 status brilliant-local.service || true
  echo "TOKEN: $(cat /var/brilliant-local/token)"
fi
REMOTE
