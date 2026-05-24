#!/usr/bin/env bash
# anp2_chrome_launch.sh — launch an isolated Chrome that goes through
# the EC2 SSH SOCKS5 proxy + has en-US locale + UTC timezone override.
#
# Purpose: GitHub account creation + 30-day own-repo activity, all
# originating from the relay's public IP, with browser fingerprints
# normalised to en-US / UTC.
#
# What this script does:
#   1. Ensures `ssh -D 1080` tunnel to the relay is running (starts
#      if not — launchd typically manages persistence).
#   2. Verifies the tunnel actually exits via the relay's IP.
#   3. Launches Chrome with a dedicated user-data-dir (isolated profile),
#      SOCKS5 proxy, and en-US / UTC env overrides.
#
# After launch, visit https://ifconfig.me in the Chrome window — it
# should show the relay IP, not the local IP.
#
# Configuration: set ANP2_RELAY_IP=<relay public ipv4>, or have
# env/relay-ip.txt contain just that single line.
set -euo pipefail

# Resolve relay IP: env var > env/relay-ip.txt > error
if [ -z "${ANP2_RELAY_IP:-}" ]; then
    if [ -r "$(dirname "$0")/../env/relay-ip.txt" ]; then
        ANP2_RELAY_IP=$(<"$(dirname "$0")/../env/relay-ip.txt")
    else
        echo "set ANP2_RELAY_IP=<relay public ipv4> or create env/relay-ip.txt" >&2
        exit 2
    fi
fi
SERVER_IP="$ANP2_RELAY_IP"
KEY="${ANP2_SSH_KEY:-/Users/ai/ai-net-stack/env/anp2.pem}"
SOCKS_PORT=1080
PROFILE_DIR="$HOME/anp2-chrome-profile"
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ -x "$CHROME_BIN" ] || CHROME_BIN="/Applications/Chromium.app/Contents/MacOS/Chromium"
[ -x "$CHROME_BIN" ] || { echo "Chrome not found at standard path"; exit 1; }

# ── ensure tunnel is up ─────────────────────────────────────────────
if pgrep -f "ssh -D $SOCKS_PORT" >/dev/null 2>&1; then
    echo "anp2_chrome: SSH SOCKS5 tunnel already running"
else
    echo "anp2_chrome: starting SSH SOCKS5 tunnel"
    ssh -D "$SOCKS_PORT" -N -f \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes \
        -i "$KEY" ec2-user@"$SERVER_IP"
    sleep 2
fi

# ── verify exit IP ─────────────────────────────────────────────────
echo "anp2_chrome: verifying tunnel exit IP …"
exit_ip=$(curl -s --max-time 8 --socks5-hostname 127.0.0.1:$SOCKS_PORT ifconfig.me)
if [ "$exit_ip" = "$SERVER_IP" ]; then
    echo "anp2_chrome: tunnel verified — Chrome will appear as $exit_ip"
else
    echo "anp2_chrome: tunnel exit IP is '$exit_ip' (expected $SERVER_IP) — abort"
    exit 1
fi

# ── prepare isolated profile dir ────────────────────────────────────
mkdir -p "$PROFILE_DIR"
echo "anp2_chrome: profile dir: $PROFILE_DIR"

# ── launch Chrome ──────────────────────────────────────────────────
# Flags:
#   --user-data-dir   : separate profile (cookies/cache/extensions isolated)
#   --proxy-server    : route through SOCKS5
#   --lang            : interface language
#   --no-first-run    : skip Chrome welcome page
#   --no-default-browser-check
# WebRTC: Chrome flags alone cannot fully disable WebRTC IP leak.
# Recommend installing "WebRTC Network Limiter" extension after launch.
# TZ env: TZ=America/New_York overrides this process's timezone.
echo "anp2_chrome: launching Chrome …"
TZ=America/New_York exec "$CHROME_BIN" \
    --user-data-dir="$PROFILE_DIR" \
    --proxy-server="socks5://127.0.0.1:$SOCKS_PORT" \
    --lang=en-US \
    --no-first-run \
    --no-default-browser-check \
    --new-window \
    "https://ifconfig.me/" &

cat <<EOF

═══════════════════════════════════════════════════════════════
Chrome 起動完了
═══════════════════════════════════════════════════════════════

確認:
  1. 開いた窓の IP が relay public IP と一致したら OK
  2. 一致しなければ tunnel か proxy の問題 — このスクリプトを再実行

次の手順 (Chrome 内で):
  3. https://github.com/signup で新 account 作成
  4. 完全に新規の email を使う (既存とリンクしない)
  5. 2FA を直後に有効化
  6. 30 日間 own-repo の commit のみ — fork / PR / cross-repo は禁止

tunnel を止めたいとき:
  pkill -f "ssh -D $SOCKS_PORT"
launchd 経由で auto-restart する場合:
  launchctl unload ~/Library/LaunchAgents/com.anp2.socks5-tunnel.plist
EOF
