#!/usr/bin/env bash
# Scrub credential literals from full git history before public push.
# Run from repo root. Idempotent: re-running on already-clean history is a no-op.
#
# Strategy:
#   1. Prefer `git filter-repo` (modern, fast, recommended).
#   2. Fall back to `git filter-branch` (bundled with git, slower).
#
# After running, verify with: bash tools/scrub_secrets.sh --verify

set -euo pipefail

SECRETS=(
  '<REDACTED-old-dashboard-pw>|<REDACTED-old-dashboard-pw>'
  '<REDACTED-dashboard-pw>|<REDACTED-dashboard-pw>'
  '<REDACTED-totp-secret>|<REDACTED-totp-secret>'
  '<REDACTED-reddit-pw>|<REDACTED-reddit-pw>'
  '<REDACTED-ops-email>|<REDACTED-ops-email>'
  '<REDACTED-operator-email>|<REDACTED-operator-email>'
)

verify() {
  local hits=0
  for entry in "${SECRETS[@]}"; do
    local needle="${entry%%|*}"
    local count
    count=$(git log --all -S"$needle" --oneline 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$count" -gt 0 ]]; then
      echo "LEAK: '$needle' still present in $count commit(s)"
      hits=$((hits + count))
    fi
  done
  if [[ "$hits" -eq 0 ]]; then
    echo "OK: history is credential-clean across ${#SECRETS[@]} known secrets"
    return 0
  fi
  return 1
}

if [[ "${1:-}" == "--verify" ]]; then
  verify
  exit $?
fi

# Build the sed program once
SED_PROGRAM=""
for entry in "${SECRETS[@]}"; do
  needle="${entry%%|*}"
  replacement="${entry##*|}"
  needle_esc=$(printf '%s\n' "$needle" | sed 's/[.[\*^$/]/\\&/g')
  replacement_esc=$(printf '%s\n' "$replacement" | sed 's/[&/\]/\\&/g')
  SED_PROGRAM+="s/${needle_esc}/${replacement_esc}/g; "
done

if command -v git-filter-repo >/dev/null 2>&1; then
  echo "Using git-filter-repo"
  # filter-repo wants a replacements file
  TMPFILE=$(mktemp)
  for entry in "${SECRETS[@]}"; do
    needle="${entry%%|*}"
    replacement="${entry##*|}"
    printf '%s==>%s\n' "$needle" "$replacement" >> "$TMPFILE"
  done
  git filter-repo --replace-text "$TMPFILE" --force
  rm "$TMPFILE"
else
  echo "Using git filter-branch (slower)"
  # tree-filter on every commit
  FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch --force --tree-filter "
    find . -type f \\( -name '*.md' -o -name '*.txt' -o -name '*.py' -o -name '*.json' -o -name '*.html' -o -name '*.toml' -o -name '*.sh' \\) -not -path './.git/*' -print0 | xargs -0 sed -i.bak -e '$SED_PROGRAM' 2>/dev/null || true
    find . -name '*.bak' -not -path './.git/*' -delete 2>/dev/null || true
  " -- --all
  rm -rf .git/refs/original 2>/dev/null || true
  git reflog expire --expire=now --all
  git gc --prune=now --aggressive
fi

echo "--- post-scrub verification ---"
verify
