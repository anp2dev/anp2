#!/usr/bin/env bash
# caddy_apply.sh (JP-redacted) apply the relay's /etc/caddy/Caddyfile SAFELY.
#
# WHY THIS EXISTS (2026-05-22): a syntactically-invalid Caddyfile edit was
# followed by an UNGATED `systemctl restart caddy`. `caddy validate` had
# actually failed, but its result was never checked (JP-redacted) the restart ran anyway,
# its start phase failed on the bad config, and the public site went down.
#
# RULE: never `systemctl restart caddy` without validating first AND without
# an automatic rollback. This script is that gate (JP-redacted) the only sanctioned way
# to apply a Caddyfile change.
#
# Workflow: edit /etc/caddy/Caddyfile on the relay host, then run this.
# It validates; restarts only if valid; checks caddy actually came back
# active; and on ANY failure restores the last config that successfully
# applied (/etc/caddy/Caddyfile.lastgood) and restarts from that.
#
# Usage:  ANP2_SERVER_IP=... ANP2_SSH_KEY=... bash tools/caddy_apply.sh
set -euo pipefail
SERVER_IP="${ANP2_SERVER_IP:?set ANP2_SERVER_IP=<relay public IP>}"
KEY="${ANP2_SSH_KEY:?set ANP2_SSH_KEY=<path to SSH private key>}"

ssh -i "$KEY" -o StrictHostKeyChecking=no "ec2-user@$SERVER_IP" 'bash -s' <<'REMOTE'
set -u
CF=/etc/caddy/Caddyfile
LKG=/etc/caddy/Caddyfile.lastgood
sudo cp -p "$CF" "$CF.bak.$(date +%s)"

ok=0
# Source every known EnvironmentFile (under sudo, since they're typically
# mode 600 owned by caddy) so `{env.X}` references in the Caddyfile resolve
# at validate time. The heredoc keeps `$f` un-expanded by the outer shell.
if sudo bash <<'VBASH'
for f in /etc/caddy/dashboard.env; do
    [ -r "$f" ] && { set -a; . "$f"; set +a; }
done
caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile >/dev/null 2>&1
VBASH
then
    if sudo systemctl restart caddy; then
        sleep 2
        [ "$(systemctl is-active caddy)" = active ] && ok=1
    fi
fi

if [ "$ok" = 1 ]; then
    sudo cp -p "$CF" "$LKG"          # advance the last-known-good snapshot
    echo "caddy_apply: OK (JP-redacted) config valid, caddy active, lastgood updated"
    exit 0
fi

echo "caddy_apply: FAILED (invalid config or restart did not come up) (JP-redacted) rolling back"
if [ -f "$LKG" ]; then
    sudo cp "$LKG" "$CF"
    sudo systemctl restart caddy
    sleep 2
    echo "caddy_apply: rolled back to lastgood (JP-redacted) caddy=$(systemctl is-active caddy)"
else
    echo "caddy_apply: no lastgood snapshot (JP-redacted) fix $CF by hand; caddy=$(systemctl is-active caddy)"
fi
exit 1
REMOTE
