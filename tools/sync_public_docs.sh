#!/usr/bin/env bash
# sync_public_docs.sh (JP-redacted) push local /docs, /spec, /CONCEPT.md, /README.md to
# /var/www/anp2-public-docs/ on the relay host. Caddy serves these paths
# at https://anp2.com/<file>. Without this rsync, a `git push` to the public
# GitHub repo updates the canonical source but leaves the live site stale (JP-redacted)
# a trap we hit on 2026-05-19 when an ONBOARDING_AI.md fix went to GitHub
# but not to anp2.com for 12 minutes.
#
# Usage:
#   ANP2_SERVER_IP=... ANP2_SSH_KEY=... bash tools/sync_public_docs.sh
#
# Idempotent. Safe to run after every docs commit.

set -euo pipefail

SERVER_IP="${ANP2_SERVER_IP:?set ANP2_SERVER_IP=<relay public IP>}"
KEY="${ANP2_SSH_KEY:?set ANP2_SSH_KEY=<path to SSH private key>}"
REMOTE_USER="ec2-user"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_ROOT="/var/www/anp2-public-docs"

SSH() { ssh -i "$KEY" -o StrictHostKeyChecking=no "$REMOTE_USER@$SERVER_IP" "$@"; }
SCP_RSYNC() { rsync -e "ssh -i $KEY -o StrictHostKeyChecking=no" "$@"; }

echo "[1/4] Stage docs to /tmp on remote"
SSH "mkdir -p /tmp/anp2-public-docs/docs /tmp/anp2-public-docs/spec"
SCP_RSYNC -az --delete \
  --include='*.md' --include='*/' --exclude='*' \
  "$REPO_ROOT/docs/" "$REMOTE_USER@$SERVER_IP:/tmp/anp2-public-docs/docs/"

for f in CONCEPT.md README.md STATUS.md CONTRIBUTING.md CODE_OF_CONDUCT.md SECURITY.md; do
  if [ -f "$REPO_ROOT/$f" ]; then
    SCP_RSYNC -az "$REPO_ROOT/$f" "$REMOTE_USER@$SERVER_IP:/tmp/anp2-public-docs/$f"
  fi
done

# spec/ is markdown + JSON schema files (JP-redacted) keep both.
SCP_RSYNC -az --delete \
  --include='*.md' --include='*.json' --include='*/' --exclude='*' \
  "$REPO_ROOT/spec/" "$REMOTE_USER@$SERVER_IP:/tmp/anp2-public-docs/spec/"

# prototypes/<subpkg>/{README,PORTING}.md (JP-redacted) these are referenced by relative
# links in docs/ONBOARDING_AI.md and need to resolve under anp2.com.
SSH "mkdir -p /tmp/anp2-public-docs/prototypes"
SCP_RSYNC -az --delete \
  --include='*/' \
  --include='*/README.md' --include='*/PORTING.md' \
  --exclude='*' \
  "$REPO_ROOT/prototypes/" "$REMOTE_USER@$SERVER_IP:/tmp/anp2-public-docs/prototypes/"

echo "[2/4] Copy to /var/www/anp2-public-docs/ (root-owned, caddy:caddy)"
SSH "sudo rsync -a --delete /tmp/anp2-public-docs/ ${REMOTE_ROOT}/ && sudo chown -R caddy:caddy ${REMOTE_ROOT}"

echo "[3/4] Verify a known landmark line is live"
landmark=$(curl -s "https://anp2.com/docs/ONBOARDING_AI.md" | grep -c "publicly readable and writeable" || true)
if [ "$landmark" -ge 1 ]; then
  echo "  (JP-redacted) ONBOARDING_AI.md reflects the 'publicly readable' fix"
else
  echo "  (JP-redacted)ï(JP-redacted)  expected landmark line not found (JP-redacted) manual check needed"
fi

echo "[4/4] Done. Files now live under https://anp2.com/{docs,spec,CONCEPT.md,README.md,STATUS.md,...}"
