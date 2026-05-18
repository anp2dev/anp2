#!/usr/bin/env bash
set -euo pipefail

# Deploy anp2-relay to EC2.
SERVER_IP="${ANP2_SERVER_IP:-<REDACTED-IP>}"
KEY="${ANP2_SSH_KEY:-/Users/ai/ai-net-stack/env/ANP2.pem}"
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
SSH "sudo -u anp2 bash -c 'cd /opt/anp2 && (python3.11 -m venv .venv 2>/dev/null || true) && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q -e .'"

echo "[4/5] Install systemd unit"
SSH "sudo cp /opt/anp2/scripts/anp2-relay.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now anp2-relay && sleep 2 && sudo systemctl status anp2-relay --no-pager | head -10"

echo "[5/5] Smoke test"
SSH "curl -sS http://127.0.0.1:8000/health"
echo
echo "Deploy done."
