#!/usr/bin/env bash
# Install or update the Brilliantly Local agent on one panel.
# Usage: SSHPASS='<panel root password>' ./install_agent.sh <panel-ip>
set -euo pipefail
host="${1:?usage: SSHPASS=... $0 <panel-ip>}"
: "${SSHPASS:?export SSHPASS with the panel root password}"
here="$(cd "$(dirname "$0")" && pwd)"
ssh_opts=(-o NumberOfPasswordPrompts=1 -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

sshpass -e scp -q "${ssh_opts[@]}" "$here/../agent/brilliant_local_agent.py" "root@$host:/tmp/brilliant_local_agent.py"
sshpass -e scp -q "${ssh_opts[@]}" "$here/brilliant-local.service" "root@$host:/tmp/brilliant-local.service"
sshpass -e ssh "${ssh_opts[@]}" "root@$host" 'set -e
  install -d -m 700 /var/brilliant-local /var/brilliant-local/app
  install -m 644 /tmp/brilliant_local_agent.py /var/brilliant-local/app/brilliant_local_agent.py
  install -m 644 /tmp/brilliant-local.service /etc/systemd/system/brilliant-local.service
  rm -f /tmp/brilliant_local_agent.py /tmp/brilliant-local.service
  if [ ! -s /var/brilliant-local/token ]; then
    ( umask 077; /data/switch-embedded/env/bin/python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /var/brilliant-local/token )
  fi
  systemctl daemon-reload
  systemctl enable brilliant-local.service >/dev/null 2>&1
  systemctl restart brilliant-local.service
  sleep 4
  systemctl --no-pager --lines=15 status brilliant-local.service || true
  echo "TOKEN: $(cat /var/brilliant-local/token)"'
