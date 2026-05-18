#!/usr/bin/env bash
set -euo pipefail

SERVER_IP="${ANP2_SERVER_IP:-<REDACTED-IP>}"
KEY="${ANP2_SSH_KEY:-/Users/ai/ai-net-stack/env/ANP2.pem}"
REMOTE_USER="ec2-user"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SSH() { ssh -i "$KEY" -o StrictHostKeyChecking=no "$REMOTE_USER@$SERVER_IP" "$@"; }
SYNC() { rsync -e "ssh -i $KEY -o StrictHostKeyChecking=no" "$@"; }

echo "[1/4] Ensure dir"
SSH "sudo mkdir -p /opt/anp2-herald && sudo chown ec2-user:ec2-user /opt/anp2-herald"

echo "[2/4] Sync code"
SYNC -a "$SCRIPT_DIR/herald.py" "$REMOTE_USER@$SERVER_IP:/opt/anp2-herald/"
SSH "sudo chown -R anp2:anp2 /opt/anp2-herald"

echo "[3/4] Install systemd unit + timer"
SYNC -a "$SCRIPT_DIR/herald.service" "$SCRIPT_DIR/herald.timer" "$REMOTE_USER@$SERVER_IP:/tmp/"
SSH "sudo mv /tmp/herald.service /tmp/herald.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now herald.timer"

echo "[4/4] Trigger first run + log"
SSH "sudo systemctl start herald.service && sleep 2 && sudo journalctl -u herald.service --no-pager -n 20"
