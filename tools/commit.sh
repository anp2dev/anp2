#!/usr/bin/env bash
# tools/commit.sh — the ONLY sanctioned local-commit path for this repo.
#
# Why: `git commit --no-verify` bypasses the pre-commit hook. This wrapper
# refuses to expose that flag at all, runs the FULL audit (not just the
# staged diff), and adds an Audit-Pass trailer to the commit message so
# server-side hooks (CI, branch-protection) can verify the commit went
# through this path.
#
# Usage:
#   tools/commit.sh -m "commit message"
#   tools/commit.sh -m "$(cat <<'EOF'
#   multi-line
#   message
#   EOF
#   )"
#
# Forbidden flags: --no-verify, --amend (use a fresh commit instead),
# --allow-empty (no reason to need an empty commit on this repo).
#
# Per [[feedback-ai-net-leak-audit-procedure]] this is the canonical
# commit path. Bare `git commit` still works (the pre-commit hook also
# protects it) but `--no-verify` is forbidden by memory rule and can be
# blocked at the shell-alias layer (see tools/dev-env/git).
set -e

root="$(git rev-parse --show-toplevel)"
cd "$root"

# Parse args — we accept -m / --message and forward all other args to git
# commit. Refuse anything that smells like bypassing.
MSG=""
EXTRA_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -m|--message)
      MSG="$2"; shift 2 ;;
    --no-verify)
      echo "commit.sh: --no-verify is FORBIDDEN in this repo." >&2
      echo "  If a commit is being blocked by the audit, fix the leak instead" >&2
      echo "  of bypassing. The rule is in" >&2
      echo "  [[feedback-ai-net-leak-audit-procedure]]." >&2
      exit 1 ;;
    --amend)
      echo "commit.sh: --amend is FORBIDDEN — make a fresh commit instead." >&2
      echo "  Amended history complicates audit-trailer verification." >&2
      exit 1 ;;
    --allow-empty|--allow-empty-message)
      echo "commit.sh: empty commits are FORBIDDEN on this repo." >&2
      exit 1 ;;
    *)
      EXTRA_ARGS+=("$1"); shift ;;
  esac
done

if [ -z "$MSG" ]; then
  echo "commit.sh: -m \"message\" is required." >&2
  exit 1
fi

# Refuse to run if hooks/ isn't configured — that is the discipline anchor.
if [ "$(git config --get core.hooksPath)" != "hooks" ]; then
  echo "commit.sh: core.hooksPath is not 'hooks'." >&2
  echo "  Run: git config core.hooksPath hooks" >&2
  echo "  (then re-run this script)" >&2
  exit 1
fi

# Stage everything currently dirty (the wrapper takes the same view of
# "what's about to be committed" that the audit checks).
git add -A

# 1. STAGED audit (fast) — catches the new lines being introduced.
echo "commit.sh: running staged-mode leak audit…" >&2
if ! python3 tools/leak_audit.py --staged >&2; then
  echo "commit.sh: staged audit FAILED — commit aborted." >&2
  exit 1
fi

# 2. WORKING-TREE audit (also fast) — catches anything already in tracked
#    files that the staged check might not see (e.g. files staged earlier).
echo "commit.sh: running working-tree leak audit…" >&2
if ! python3 tools/leak_audit.py >&2; then
  echo "commit.sh: working-tree audit FAILED — commit aborted." >&2
  exit 1
fi

# 3. FULL-history audit (slow ~10s) — catches anything in the cumulative
#    blob set, including dangling objects from a prior aborted operation.
#    This is the "are we still publish-safe" check.
echo "commit.sh: running --full leak audit (slow)…" >&2
if ! python3 tools/leak_audit.py --full >&2; then
  echo "commit.sh: --full audit FAILED — commit aborted." >&2
  echo "  Cumulative history contains a leak pattern. Run a filter-repo" >&2
  echo "  scrub pass before committing more." >&2
  exit 1
fi

# Add an Audit-Pass trailer so server-side / future-audit code can
# verify this commit went through this wrapper.
AUDIT_VER=$(sha256sum tools/leak_audit.py | cut -c1-12)
TRAILER="Audit-Pass: leak_audit.py@${AUDIT_VER}"

# Compose the final message: original + blank line + trailer.
# If the user already included an Audit-Pass trailer, don't double-add.
if echo "$MSG" | grep -q "^Audit-Pass:"; then
  FINAL_MSG="$MSG"
else
  FINAL_MSG="${MSG}

${TRAILER}"
fi

# Final commit. We pass through any extra args (e.g. -s for sign-off)
# but never --no-verify.
echo "commit.sh: 3/3 audits PASS — committing." >&2
git commit -m "$FINAL_MSG" "${EXTRA_ARGS[@]}"

echo "commit.sh: ✅ commit complete with trailer '$TRAILER'" >&2
