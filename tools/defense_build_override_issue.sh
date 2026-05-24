#!/usr/bin/env bash
# defense_build_override_issue.sh — issue a single-use override token that
# lets ONE commit through the pre-commit Flag-risk FAIL gate.
#
# Use case: account is flagged + we need to commit defense improvements
# (which is itself the work that gets us off the flagged state). The
# alternative — wait until flag clears — has its own cost.
#
# Token is single-use and consumed on first successful commit.
set -euo pipefail
mkdir -p env
token=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
echo "$token" > env/.flag-risk-commit-override
echo ""
echo "  Defense-build override granted. SINGLE-USE token:"
echo ""
echo "    $token"
echo ""
echo "  Usage:"
echo "    ANP2_FLAG_RISK_OVERRIDE=$token git commit -m 'message'"
echo ""
echo "  Token is consumed on first successful commit."
echo "  pushing to a flagged remote is still blocked separately by pre-push."
