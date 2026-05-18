#!/usr/bin/env bash
set -euo pipefail

SERVER_IP="${ANP2_SERVER_IP:-<REDACTED-IP>}"
KEY="${ANP2_SSH_KEY:-/Users/ai/ai-net-stack/env/ANP2.pem}"
REMOTE_USER="ec2-user"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_DIR="$(cd "$SCRIPT_DIR/../../client" && pwd)"

SSH() { ssh -i "$KEY" -o StrictHostKeyChecking=no "$REMOTE_USER@$SERVER_IP" "$@"; }
SYNC() { rsync -e "ssh -i $KEY -o StrictHostKeyChecking=no" "$@"; }

echo "[1/5] Ensure dirs"
SSH "sudo mkdir -p /opt/anp2-herald /opt/anp2-client && sudo chown ec2-user:ec2-user /opt/anp2-herald /opt/anp2-client"

echo "[2/5] Sync anp2_client (shared lib)"
SYNC -az --delete --exclude '.venv' --exclude '__pycache__' --exclude '*.egg-info' --exclude '.pytest_cache' \
  "$CLIENT_DIR/" "$REMOTE_USER@$SERVER_IP:/opt/anp2-client/"
SSH "sudo chown -R anp2:anp2 /opt/anp2-client"

echo "[3/5] Install anp2_client into relay venv"
SSH "sudo -u anp2 /opt/anp2/.venv/bin/pip install -q -e /opt/anp2-client"

echo "[4/5] Sync herald script + units"
SYNC -az "$SCRIPT_DIR/herald.py" "$REMOTE_USER@$SERVER_IP:/opt/anp2-herald/"
SSH "sudo chown -R anp2:anp2 /opt/anp2-herald"
SYNC -az "$SCRIPT_DIR/herald.service" "$SCRIPT_DIR/herald.timer" "$REMOTE_USER@$SERVER_IP:/tmp/"
SSH "sudo mv /tmp/herald.service /tmp/herald.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now herald.timer"

echo "[5/5] Run once + log"
SSH "sudo systemctl start herald.service && sleep 2 && sudo journalctl -u herald.service --no-pager -n 10"
