#!/usr/bin/env bash
# defense_integrity.sh — verify the integrity of the defense stack.
#
# Defense files (leak_audit.py / account_health.py / *_safe.sh / hooks/*)
# control the entire safety surface. If they get silently modified (by
# accident, or by a regression patch, or hypothetically by a future
# adversary), the safety guarantees evaporate.
#
# This script:
#   1. Maintains a SHA256 manifest of all defense files in
#      env/.defense-manifest.sha256 (gitignored).
#   2. Verifies current files match the manifest.
#   3. Reports diff vs manifest (any file that drifted needs explicit
#      operator approval before being re-pinned).
#
# Modes:
#   verify   — default, exit 0 if match, 1 if drift
#   pin      — write current SHA256 set to manifest (operator action only)
#   show     — print current vs pinned for each file
#
# Called by pre-push hook so drift blocks publish.
set -euo pipefail

MANIFEST=env/.defense-manifest.sha256
mkdir -p env

DEFENSE_FILES=(
    tools/leak_audit.py
    tools/account_health.py
    tools/gh_safe.sh
    tools/git_safe.sh
    tools/publish_safe.sh
    tools/flag_risk_check.sh
    tools/defense_integrity.sh
    tools/defense_build_override_issue.sh
    hooks/pre-commit
    hooks/pre-push
)

ACTION="${1:-verify}"

current_sha() {
    local f=$1
    [ -r "$f" ] && shasum -a 256 "$f" | awk '{print $1}' || echo "MISSING"
}

case "$ACTION" in
    pin)
        : > "$MANIFEST"
        for f in "${DEFENSE_FILES[@]}"; do
            local_sha=$(current_sha "$f")
            printf '%s  %s\n' "$local_sha" "$f" >> "$MANIFEST"
        done
        echo "defense_integrity: pinned $(wc -l < "$MANIFEST" | tr -d ' ') files into $MANIFEST"
        ;;
    show)
        printf '%-65s  %-65s  %s\n' "CURRENT_SHA" "PINNED_SHA" "FILE"
        for f in "${DEFENSE_FILES[@]}"; do
            local_sha=$(current_sha "$f")
            pinned_sha=$(grep -F "  $f" "$MANIFEST" 2>/dev/null | awk '{print $1}' || echo "—")
            printf '%-65s  %-65s  %s\n' "$local_sha" "$pinned_sha" "$f"
        done
        ;;
    verify)
        if [ ! -r "$MANIFEST" ]; then
            echo "defense_integrity: no manifest at $MANIFEST. Pin first: $0 pin" >&2
            exit 1
        fi
        drift=0
        # manifest lines: "<sha>  <path>" (two-space separator per shasum format).
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            want_sha=$(printf '%s' "$line" | awk '{print $1}')
            f=$(printf '%s' "$line" | awk '{$1=""; sub(/^  */,""); print}')
            have_sha=$(current_sha "$f")
            if [ "$want_sha" != "$have_sha" ]; then
                echo "defense_integrity: DRIFT — $f" >&2
                echo "  want: $want_sha" >&2
                echo "  have: $have_sha" >&2
                drift=$((drift + 1))
            fi
        done < "$MANIFEST"
        if [ "$drift" -gt 0 ]; then
            echo "" >&2
            echo "defense_integrity: $drift file(s) drifted from manifest." >&2
            echo "  • If the drift is intentional, re-pin: $0 pin" >&2
            echo "  • Pin should be an operator-approved action (not a routine bypass)." >&2
            exit 1
        fi
        echo "defense_integrity: all $(wc -l < "$MANIFEST" | tr -d ' ') defense files match manifest"
        ;;
    *)
        echo "usage: $0 {verify|pin|show}" >&2
        exit 2
        ;;
esac
