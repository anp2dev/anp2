#!/usr/bin/env bash
# sync_landing.sh — push site/ (AI-facing landing surface) to /var/www/anp2/
# on the relay host. Caddy serves these from the default handler at
# anp2.com/{llms.txt, robots.txt, sitemap.xml, .well-known/*, share/*, ...}.
#
# Companion to sync_public_docs.sh:
#   sync_public_docs.sh → /var/www/anp2-public-docs/ (Caddy /docs* /spec* paths)
#   sync_landing.sh     → /var/www/anp2/             (Caddy default handler)
#
# Source of truth: $REPO_ROOT/site/. Built 2026-05-25 to end the drift between
# repo + live for the discovery surface (llms.txt, .well-known/*, etc.) that
# AI crawlers fetch first.
#
# Two-stage staging: rsync to /tmp/anp2-landing/ first, then atomic-flip into
# the live docroot with rsync --delete. Server-managed assets (favicon, og/,
# dist/, debate/, try/, JOIN.md.bak.*, *.bak-*) are protected by --exclude
# rules below so the flip never prunes them.
#
# Usage:
#   ANP2_SERVER_IP=... ANP2_SSH_KEY=... bash tools/sync_landing.sh
#
# Or, if internal/env/relay-ip.txt + internal/env/anp2.pem are populated (default), bare:
#   bash tools/sync_landing.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_IP="${ANP2_SERVER_IP:-$(cat "$REPO_ROOT/internal/env/relay-ip.txt" 2>/dev/null)}"
KEY="${ANP2_SSH_KEY:-$REPO_ROOT/internal/env/anp2.pem}"
REMOTE_USER="ec2-user"
REMOTE_ROOT="/var/www/anp2"
STAGING="/tmp/anp2-landing"

[ -n "$SERVER_IP" ] || { echo "relay IP not configured (set ANP2_SERVER_IP)"; exit 1; }
[ -f "$KEY" ]       || { echo "SSH key not found (set ANP2_SSH_KEY)"; exit 1; }
[ -d "$REPO_ROOT/site" ] || { echo "site/ directory missing — nothing to sync"; exit 1; }

# Host-key pinning: cache the relay's host key on first run, then verify
# strictly on every subsequent run. Avoids the MITM window inherent to
# StrictHostKeyChecking=no.
KNOWN_HOSTS="$REPO_ROOT/internal/env/.ssh-known-hosts"
SSH_OPTS="-i $KEY -o UserKnownHostsFile=$KNOWN_HOSTS -o StrictHostKeyChecking=accept-new"
SSH() { ssh $SSH_OPTS "$REMOTE_USER@$SERVER_IP" "$@"; }
RSYNC() { rsync -e "ssh $SSH_OPTS" "$@"; }

echo "[1/4] Stage site/ → $STAGING on remote"
# Create staging with 0700 so other accounts on the relay host cannot
# enumerate intermediate landing content (defense-in-depth: even though
# site/ contents are public, treat staging as private until the atomic
# flip).
SSH "mkdir -p $STAGING/.well-known $STAGING/share && chmod 0700 $STAGING"
# Push site/ recursively. --delete makes the staging dir mirror site/ exactly
# so a removed file in repo propagates. Excludes apply when flipping (step 2).
RSYNC -az --delete \
  "$REPO_ROOT/site/" "$REMOTE_USER@$SERVER_IP:$STAGING/"

echo "[2/4] Atomic flip $STAGING → $REMOTE_ROOT (server-managed assets protected)"
# The docroot also holds files this script does NOT manage: favicon.ico,
# apple-touch-icon.png, og/ images, dist/ artifacts, debate/, try/, skill/,
# skills/, the ACME challenge token, and historical .bak files. Exclude
# them so --delete never prunes them.
SSH "sudo rsync -a --delete \
  --exclude='/favicon.ico' \
  --exclude='/apple-touch-icon.png' \
  --exclude='/og/' \
  --exclude='/dist/' \
  --exclude='/debate/' \
  --exclude='/try/' \
  --exclude='/try.html' \
  --exclude='/skill/' \
  --exclude='/skills/' \
  --exclude='/6316e178a2a76ed5c5431e1577fa9019.txt' \
  --exclude='*.bak' \
  --exclude='*.bak-*' \
  --exclude='*.bak.*' \
  $STAGING/ $REMOTE_ROOT/ && sudo chown -R caddy:caddy $REMOTE_ROOT && sudo rm -rf $STAGING"

echo "[3/4] Verify landmark lines"
landmark_llms=$(curl -s "https://anp2.com/llms.txt" | grep -c "ANP2 adds incentive" || true)
landmark_robots=$(curl -s "https://anp2.com/robots.txt" | grep -c "GPTBot" || true)
landmark_positioning=$(curl -s "https://anp2.com/.well-known/positioning.json" | grep -c '"protocol": "ANP2"' || true)
echo "  llms.txt 8-layer hook present: $landmark_llms (expected ≥ 1)"
echo "  robots.txt GPTBot allow present: $landmark_robots (expected ≥ 1)"
echo "  positioning.json protocol=ANP2: $landmark_positioning (expected ≥ 1)"

if [ "$landmark_llms" -lt 1 ] || [ "$landmark_robots" -lt 1 ] || [ "$landmark_positioning" -lt 1 ]; then
  echo "  —⚠—  one or more landmarks missing — manual check needed"
  exit 2
fi

echo "[4/4] Done. site/ is live at https://anp2.com/{llms.txt, robots.txt, sitemap.xml, .well-known/*, share/*}"
