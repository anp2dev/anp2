#!/usr/bin/env bash
set -euo pipefail

# Deploy anp2-relay to EC2.
SERVER_IP="${ANP2_SERVER_IP:?set ANP2_SERVER_IP=<your relay host or IP>}"
KEY="${ANP2_SSH_KEY:?set ANP2_SSH_KEY=<path to your SSH private key>}"
REMOTE_USER="ec2-user"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELAY_DIR="$(dirname "$SCRIPT_DIR")"

SSH() { ssh -i "$KEY" -o StrictHostKeyChecking=no "$REMOTE_USER@$SERVER_IP" "$@"; }
SCP_RSYNC() { rsync -e "ssh -i $KEY -o StrictHostKeyChecking=no" "$@"; }

echo "[1/5] Ensure anp2 user + dirs"
SSH "sudo id anp2 >/dev/null 2>&1 || sudo useradd --system --home /opt/anp2 --shell /usr/sbin/nologin anp2"
SSH "sudo mkdir -p /opt/anp2 /var/lib/anp2 /var/log/anp2 && sudo chown -R anp2:anp2 /var/lib/anp2 /var/log/anp2"

echo "[2/5] Sync code"
SCP_RSYNC -az --delete \
  --exclude '__pycache__' --exclude '*.egg-info' --exclude '.pytest_cache' --exclude '.venv' --exclude 'anp2.db*' \
  "$RELAY_DIR/" "$REMOTE_USER@$SERVER_IP:/tmp/anp2-relay/"
SSH "sudo rsync -a --delete /tmp/anp2-relay/ /opt/anp2/ && sudo chown -R anp2:anp2 /opt/anp2"

echo "[3/5] Install venv + deps"
# anp2-client is installed alongside the relay because all seed-agent scripts
# (under /opt/anp2-*) execute against /opt/anp2/.venv/bin/python. Forgetting
# this caused a 64-minute seed-agent outage on 2026-05-19 when 16 systemd
# services failed with ModuleNotFoundError after a relay redeploy.
SSH "sudo -u anp2 bash -c 'cd /opt/anp2 && (python3.11 -m venv .venv 2>/dev/null || true) && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q -e . && if [ -d /opt/anp2-client/src/anp2_client ]; then .venv/bin/pip install -q --upgrade -e /opt/anp2-client; else .venv/bin/pip install -q --upgrade anp2-client; fi'"

echo "[4/5] Install systemd unit + restart to load new code"
# `enable --now` only STARTS a stopped service (JP-redacted) it does NOT reload code into an
# already-running relay, so a code deploy would silently not take effect. An
# explicit restart is required.
SSH "sudo cp /opt/anp2/scripts/anp2-relay.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable anp2-relay && sudo systemctl restart anp2-relay && sleep 2 && sudo systemctl status anp2-relay --no-pager | head -10"

echo "[5/5] Smoke test"
SSH "curl -sS http://127.0.0.1:8000/health"
echo
echo "Deploy done."
