#!/usr/bin/env bash
# Deploy seed agents to EC2. Usage: ./deploy.sh [agent_name] [interval_minutes]
# Without args, deploys all seed agents.
set -eu

SERVER_IP="${ANP2_SERVER_IP:-<REDACTED-IP>}"
KEY="${ANP2_SSH_KEY:-/Users/ai/ai-net-stack/env/ANP2.pem}"
REMOTE_USER="ec2-user"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_DIR="$(cd "$SCRIPT_DIR/../client" && pwd)"

# Default agent table: "name:interval_minutes"
DEFAULT_AGENTS="herald:10 welcome:5 echo:5 oracle:60 translate:5 citation:30 health:15 catalyst:15"

SSH() { ssh -i "$KEY" -o StrictHostKeyChecking=no "$REMOTE_USER@$SERVER_IP" "$@"; }
SYNC() { rsync -e "ssh -i $KEY -o StrictHostKeyChecking=no" "$@"; }

deploy_one() {
    name="$1"
    interval_min="$2"
    interval="${interval_min}min"

    [ -d "$SCRIPT_DIR/$name" ] || { echo "skip $name (no folder)"; return 0; }
    [ -f "$SCRIPT_DIR/$name/$name.py" ] || { echo "skip $name (no script)"; return 0; }

    echo "(JP-redacted) deploy seed: $name (every $interval)"
    SSH "sudo mkdir -p /opt/anp2-$name && sudo chown ec2-user:ec2-user /opt/anp2-$name"
    SYNC -az "$SCRIPT_DIR/$name/$name.py" "$REMOTE_USER@$SERVER_IP:/opt/anp2-$name/"
    SSH "sudo chown -R anp2:anp2 /opt/anp2-$name"

    unit_tmp=$(mktemp -d)
    sed -e "s/{{NAME}}/$name/g" -e "s/{{INTERVAL}}/$interval/g" \
        "$SCRIPT_DIR/_systemd/seed-agent.service.tmpl" > "$unit_tmp/$name.service"
    sed -e "s/{{NAME}}/$name/g" -e "s/{{INTERVAL}}/$interval/g" \
        "$SCRIPT_DIR/_systemd/seed-agent.timer.tmpl" > "$unit_tmp/$name.timer"

    SYNC -az "$unit_tmp/$name.service" "$unit_tmp/$name.timer" \
         "$REMOTE_USER@$SERVER_IP:/tmp/"
    rm -rf "$unit_tmp"

    SSH "sudo mv /tmp/$name.service /tmp/$name.timer /etc/systemd/system/ && \
         sudo systemctl daemon-reload && \
         sudo systemctl enable --now $name.timer && \
         sudo systemctl start $name.service && \
         sleep 1"
}

# Sync shared client lib first
echo "[shared] anp2_client (JP-redacted) server"
SSH "sudo mkdir -p /opt/anp2-client && sudo chown -R ec2-user:ec2-user /opt/anp2-client"
SYNC -az --delete --exclude '.venv' --exclude '__pycache__' --exclude '*.egg-info' --exclude '.pytest_cache' \
    "$CLIENT_DIR/" "$REMOTE_USER@$SERVER_IP:/opt/anp2-client/" 2>&1 | grep -v "failed to set times" || true
SSH "sudo chown -R anp2:anp2 /opt/anp2-client && sudo -u anp2 /opt/anp2/.venv/bin/pip install -q -e /opt/anp2-client"

if [ $# -ge 1 ]; then
    target="$1"
    interval="${2:-10}"
    deploy_one "$target" "$interval"
else
    for entry in $DEFAULT_AGENTS; do
        name="${entry%%:*}"
        interval="${entry##*:}"
        deploy_one "$name" "$interval"
    done
fi

echo ""
echo "Active timers:"
SSH "systemctl list-timers --no-pager 2>/dev/null | grep -E '(herald|welcome|echo|oracle|translate|citation|health|catalyst)' || true"
