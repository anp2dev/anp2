#!/usr/bin/env bash
# tools/push.sh — the ONLY sanctioned local-push path for this repo.
#
# Why: bare `git push` works via the pre-push hook, BUT `--no-verify`
# bypasses the hook entirely, and several other flags (`--force`,
# `--force-with-lease`, `--mirror`, `--no-thin`) are dangerous on a repo
# that's been through filter-repo history rewrites. This wrapper refuses
# all of them, runs the full audit chain proactively (not just at hook
# time), and logs each push for post-incident audit.
#
# Usage:
#   tools/push.sh                       # push current branch to origin
#   tools/push.sh <remote> <ref>        # explicit
#
# Forbidden flags (and operator-discipline reasons):
#   --no-verify         bypasses hooks (the whole point of hooks)
#   --force             rewrites remote history — never on publish-bound
#   --force-with-lease  same intent, just safer; still forbidden on main
#   --mirror            pushes ALL refs incl. tags & deletes — too broad
#   --delete            ref deletion needs explicit operator intent
#
# Bypass (intentional, awkward): `command git push --force ...`
# Documented in [[feedback-ai-net-github-account-discipline]].
set -e

root="$(git rev-parse --show-toplevel)"
cd "$root"

REMOTE="${1:-origin}"
REF="${2:-$(git rev-parse --abbrev-ref HEAD)}"

# Refuse dangerous flags
for arg in "$@"; do
  case "$arg" in
    --no-verify|--force|--force-with-lease|--mirror|--delete)
      echo "push.sh: '$arg' is FORBIDDEN on this repo." >&2
      echo "  Reason: defeats audits or rewrites public history." >&2
      echo "  Memory rule: [[feedback-ai-net-github-account-discipline]]" >&2
      echo "  Deliberate bypass (last resort): command git push $arg ..." >&2
      exit 1 ;;
  esac
done

# Sanity: hooks must be configured
if [ "$(git config --get core.hooksPath)" != "hooks" ]; then
  echo "push.sh: core.hooksPath is not 'hooks' — refusing to push." >&2
  echo "  Run: git config core.hooksPath hooks" >&2
  exit 1
fi

# Show what we're about to push (commit count + range)
upstream="$REMOTE/$REF"
if git rev-parse --verify "$upstream" >/dev/null 2>&1; then
  range="$upstream..$REF"
  count=$(git rev-list --count "$range")
else
  range="$REF (initial push)"
  count=$(git rev-list --count "$REF")
fi
burst=$(git rev-list --since="1 hour ago" --count "$REF")
echo "push.sh: pushing $count commit(s) to $REMOTE/$REF (last-1h burst: $burst)" >&2

# Pre-flight: run BOTH audits before invoking git push so failures fail
# fast and visibly (pre-push hook is the last line; this is the first).
echo "push.sh: pre-flight leak audit (default + --full)…" >&2
if ! python3 tools/leak_audit.py >/dev/null; then
  echo "push.sh: leak audit (default) FAILED — push aborted." >&2
  python3 tools/leak_audit.py >&2 || true; exit 1
fi
if ! python3 tools/leak_audit.py --full >/dev/null; then
  echo "push.sh: leak audit (--full) FAILED — push aborted." >&2
  python3 tools/leak_audit.py --full >&2 || true; exit 1
fi

echo "push.sh: pre-flight account-health audit…" >&2
if ! ANP2_INCOMING_COMMITS=$count python3 tools/account_health.py >/dev/null; then
  echo "push.sh: account-health audit FAILED — push aborted." >&2
  ANP2_INCOMING_COMMITS=$count python3 tools/account_health.py >&2 || true; exit 1
fi

# Persist a local push log (gitignored; for post-incident audit)
LOG="$root/internal/env/.push-log.jsonl"
mkdir -p "$(dirname "$LOG")"
python3 - <<PY >> "$LOG"
import json, time
print(json.dumps({
    "ts": int(time.time()),
    "remote": "$REMOTE",
    "ref": "$REF",
    "count": $count,
    "burst_1h": $burst,
    "head": "$(git rev-parse HEAD)",
}))
PY

# Invoke git push WITHOUT --no-verify so pre-push hook also runs (defense
# in depth — if anything changed between pre-flight and now)
echo "push.sh: invoking git push…" >&2
git push "$REMOTE" "$REF"

echo "push.sh: ✅ push complete + logged to internal/env/.push-log.jsonl" >&2
