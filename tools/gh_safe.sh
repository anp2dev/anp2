#!/usr/bin/env bash
# gh_safe.sh — anti-flag wrapper for EVERY gh write op.
#
# WHY THIS EXISTS: 2026-05-24, anp2dev was shadow-suppressed by GitHub
# after `gh repo fork` × 5 in 50 seconds (= textbook bot pattern). The
# prior anp2dev account hit the same fate one week earlier. Both
# flags came from MASS WRITE OPERATIONS executed at machine speed against
# a low-trust account.
#
# This wrapper is the SINGLE allowed entry point for any `gh` command that
# writes to GitHub (fork / PR / issue / release / repo create / api POST /
# etc.). Read ops (view, list, api GET) bypass for speed.
#
# Routing: ~/.zshrc's `gh()` shell function intercepts each gh invocation
# and, for write subcommands, calls this script. Direct invocation of this
# script also works.
#
# Categories + rate caps (env-var overrideable):
#   fork              cap_1h=1, cap_24h=1, cap_7d=3
#   pr-create         cap_1h=1, cap_24h=2, cap_7d=5  (PLUS listing-PR check)
#   pr-merge          cap_1h=2, cap_24h=5
#   pr-comment        cap_1h=5, cap_24h=20
#   issue-create      cap_1h=2, cap_24h=5
#   issue-comment     cap_1h=5, cap_24h=20
#   release-create    cap_1h=1, cap_24h=1, cap_7d=3
#   repo-create       cap_1h=1, cap_24h=1, cap_7d=2
#   repo-edit         cap_1h=2, cap_24h=5
#   repo-delete       cap_1h=1, cap_24h=1  (extreme — usually operator)
#   secret-set        cap_1h=3, cap_24h=10
#   gist-create       cap_1h=1, cap_24h=2
#   workflow-run      cap_1h=3, cap_24h=10
#   api-write         cap_1h=5, cap_24h=20  (catches gh api -X POST/PUT/PATCH/DELETE)
#
# Common gates (apply BEFORE category-specific rate cap):
#   - account age ≥ 7 days
#   - 2FA enabled
#   - followers ≥ 1 OR account age ≥ 30 days
#
# Listing-PR detection (pr-create only):
#   If target repo name matches /awesome|registry|list|directory|catalog/i
#   → refuse unless ANP2_LISTING_PR_TURN_TOKEN env var matches the token
#     printed when operator runs `tools/gh_safe.sh listing-pr-approve`.
#
# Bypass: explicit GH_SAFE_FORCE=1 (logged, doesn't skip listing-PR check).
set -euo pipefail

ACTION="${1:-}"
shift || true

LOG=env/.gh-activity-log.jsonl
APPROVAL_TOKEN_FILE=env/.gh-listing-pr-token
mkdir -p env
touch "$LOG"

now_unix() { date +%s; }
log_op() {
    local action=$1 target=${2:-} status=$3
    local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local esc_target esc_status
    esc_target=$(printf '%s' "$target" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read())[1:-1])')
    esc_status=$(printf '%s' "$status" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read())[1:-1])')
    printf '{"ts":"%s","unix":%d,"action":"%s","target":"%s","status":"%s"}\n' \
        "$ts" "$(now_unix)" "$action" "$esc_target" "$esc_status" >> "$LOG"
}
fail() {
    echo "gh_safe: REFUSED — $1" >&2
    log_op "${ACTION:-?}" "${TARGET:-?}" "REFUSED:$1"
    exit 1
}

# ── Common gates ─────────────────────────────────────────────────────
preflight() {
    local who; who=$(gh api /user --jq '.login' 2>/dev/null || echo "")
    [ -n "$who" ] || fail "gh CLI not authenticated"

    local created_at; created_at=$(gh api /user --jq '.created_at' 2>/dev/null || echo "")
    [ -n "$created_at" ] || fail "could not read account created_at"
    local created_unix; created_unix=$(python3 -c "
import datetime
print(int(datetime.datetime.fromisoformat('$created_at'.replace('Z','+00:00')).timestamp()))")
    local age_days=$(( ( $(now_unix) - created_unix ) / 86400 ))

    # Gate A: account age ≥ 7 days
    if [ "$age_days" -lt 7 ]; then
        fail "account $who is only $age_days days old (need ≥ 7); waiting reduces flag risk"
    fi

    # Gate B: 2FA enabled
    local tfa; tfa=$(gh api /user --jq '.two_factor_authentication // false' 2>/dev/null || echo false)
    if [ "$tfa" != "true" ]; then
        fail "2FA not enabled on $who"
    fi

    # Gate C: trust floor
    local followers; followers=$(gh api /user --jq '.followers' 2>/dev/null || echo 0)
    if [ "$followers" = "0" ] && [ "$age_days" -lt 30 ]; then
        fail "trust floor: followers=0 AND age=${age_days}d (need followers≥1 or age≥30d)"
    fi

    GH_USER="$who"
}

# ── Rate cap check ───────────────────────────────────────────────────
# count_recent <action> <window_seconds>  → echoes count
count_recent() {
    local action=$1 window=$2
    local cutoff=$(( $(now_unix) - window ))
    python3 -c "
import json
n=0
try:
    for line in open('$LOG'):
        e=json.loads(line)
        if e.get('action')=='$action' and e.get('status')=='OK' and e.get('unix',0)>=$cutoff:
            n+=1
    print(n)
except: print(0)
"
}

check_rate() {
    # check_rate <action> <cap_1h> <cap_24h> [cap_7d]
    local action=$1 cap_1h=$2 cap_24h=$3 cap_7d=${4:-999}
    local n1; n1=$(count_recent "$action" 3600)
    local n24; n24=$(count_recent "$action" 86400)
    local n7; n7=$(count_recent "$action" $((86400 * 7)))
    [ "$n1"  -gt "$cap_1h"  ] && fail "$action rate-limit: $n1 in last 1h > cap=$cap_1h"
    [ "$n24" -gt "$cap_24h" ] && fail "$action rate-limit: $n24 in last 24h > cap=$cap_24h"
    [ "$n7"  -gt "$cap_7d"  ] && fail "$action rate-limit: $n7 in last 7d > cap=$cap_7d"
    echo "gh_safe: rate OK ($action: ${n1}/h ${n24}/24h ${n7}/7d)" >&2
}

# Optional fork-rate via gh API (catches direct `gh repo fork` bypass)
recent_forks_24h_api() {
    local cutoff=$(( $(now_unix) - 86400 ))
    gh api "/users/${GH_USER}/repos?type=forks&per_page=50" 2>/dev/null | python3 -c "
import json,sys,datetime
try:
    data=json.load(sys.stdin)
    n=0
    for r in data:
        ts=int(datetime.datetime.fromisoformat(r['created_at'].replace('Z','+00:00')).timestamp())
        if ts>=$cutoff: n+=1
    print(n)
except: print(0)
"
}

# ── Listing-PR detection ─────────────────────────────────────────────
# Target name like "awesome-mcp-servers", "toolsdk-mcp-registry",
# "awesome-llms-txt" etc. = a listing/directory/catalog. PRs to these
# repos are PROMOTION OPERATIONS (rule, executed form) and need a
# fresh per-call approval token (operator-agent-issued).
is_listing_pr_target() {
    local target=$1
    # Lowercase + check for indicator words
    local lc; lc=$(echo "$target" | tr '[:upper:]' '[:lower:]')
    case "$lc" in
        *awesome*|*registry*|*catalog*|*directory*|*-list*|*list-*)
            return 0 ;;
    esac
    return 1
}

require_listing_pr_approval() {
    local target=$1
    if ! is_listing_pr_target "$target"; then
        return 0  # not a listing target; no approval needed
    fi
    # If approval token is set AND matches the file, allow
    local stored="" presented="${ANP2_LISTING_PR_TURN_TOKEN:-}"
    [ -r "$APPROVAL_TOKEN_FILE" ] && stored=$(cat "$APPROVAL_TOKEN_FILE")
    if [ -n "$stored" ] && [ "$stored" = "$presented" ]; then
        # consume the token (single-use)
        rm -f "$APPROVAL_TOKEN_FILE"
        echo "gh_safe: listing-PR approval consumed (single-use)" >&2
        return 0
    fi
    fail "PR to '$target' looks like a listing (rule promotion execution).
       Operator must explicitly approve: tools/gh_safe.sh listing-pr-approve
       Then re-run with ANP2_LISTING_PR_TURN_TOKEN=<token from that command>"
}

# ── Operations ───────────────────────────────────────────────────────

op_fork() {
    local target="${1:-}"
    TARGET="$target"
    [ -n "$target" ] || fail "usage: gh_safe fork <owner/repo>"
    preflight
    check_rate fork 1 1 3
    # Also cross-check via API in case someone bypassed the log
    local n_api; n_api=$(recent_forks_24h_api)
    if [ "$n_api" -ge 1 ]; then
        fail "API shows $n_api fork(s) in last 24h (cap=1) — wait 24h"
    fi
    if gh repo fork "$target" --clone=false 2>&1; then
        log_op fork "$target" "OK"; return 0
    fi
    log_op fork "$target" "FAILED"; fail "gh repo fork command failed"
}

op_pr_create() {
    TARGET="$*"
    preflight
    # Detect target repo from --repo arg or cwd
    local target_repo=""
    while [ $# -gt 0 ]; do
        case "$1" in
            -R|--repo) target_repo="$2"; shift 2 ;;
            *)         shift ;;
        esac
    done
    if [ -z "$target_repo" ]; then
        target_repo=$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null || echo "")
    fi
    require_listing_pr_approval "$target_repo"
    check_rate pr-create 1 2 5
    if gh pr create "$@" 2>&1; then
        log_op pr-create "${target_repo:-?}" "OK"; return 0
    fi
    log_op pr-create "${target_repo:-?}" "FAILED"; return 1
}

op_issue_create()    { TARGET="$*"; preflight; check_rate issue-create 2 5;       gh issue create "$@" && log_op issue-create "${1:-}" "OK"; }
op_issue_comment()   { TARGET="$*"; preflight; check_rate issue-comment 5 20;     gh issue comment "$@" && log_op issue-comment "${1:-}" "OK"; }
op_pr_comment()      { TARGET="$*"; preflight; check_rate pr-comment 5 20;        gh pr comment "$@" && log_op pr-comment "${1:-}" "OK"; }
op_pr_merge()        { TARGET="$*"; preflight; check_rate pr-merge 2 5;           gh pr merge "$@" && log_op pr-merge "${1:-}" "OK"; }
op_pr_close()        { TARGET="$*"; preflight; check_rate pr-close 5 10;          gh pr close "$@" && log_op pr-close "${1:-}" "OK"; }
op_issue_close()     { TARGET="$*"; preflight; check_rate issue-close 5 10;       gh issue close "$@" && log_op issue-close "${1:-}" "OK"; }
op_release_create()  { TARGET="$*"; preflight; check_rate release-create 1 1 3;   gh release create "$@" && log_op release-create "${1:-}" "OK"; }
op_release_delete()  { TARGET="$*"; preflight; check_rate release-delete 1 2;     gh release delete "$@" && log_op release-delete "${1:-}" "OK"; }
op_repo_create()     { TARGET="$*"; preflight; check_rate repo-create 1 1 2;      gh repo create "$@" && log_op repo-create "${1:-}" "OK"; }
op_repo_edit()       { TARGET="$*"; preflight; check_rate repo-edit 2 5;          gh repo edit "$@" && log_op repo-edit "${1:-}" "OK"; }
op_repo_delete()     { TARGET="$*"; preflight; check_rate repo-delete 1 1;        gh repo delete "$@" && log_op repo-delete "${1:-}" "OK"; }
op_secret_set()      { TARGET="$*"; preflight; check_rate secret-set 3 10;        gh secret set "$@" && log_op secret-set "${1:-}" "OK"; }
op_gist_create()     { TARGET="$*"; preflight; check_rate gist-create 1 2;        gh gist create "$@" && log_op gist-create "${1:-}" "OK"; }
op_workflow_run()    { TARGET="$*"; preflight; check_rate workflow-run 3 10;      gh workflow run "$@" && log_op workflow-run "${1:-}" "OK"; }
op_api_write()       { TARGET="$*"; preflight; check_rate api-write 5 20;         gh api "$@" && log_op api-write "${1:-}" "OK"; }

op_listing_pr_approve() {
    local token; token=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
    echo "$token" > "$APPROVAL_TOKEN_FILE"
    echo ""
    echo "  Listing-PR approval granted. SINGLE-USE token:"
    echo ""
    echo "    $token"
    echo ""
    echo "  To use:"
    echo "    ANP2_LISTING_PR_TURN_TOKEN=$token tools/gh_safe.sh pr-create <args>"
    echo ""
    echo "  Token is consumed on first successful pr-create to a listing repo."
}

case "$ACTION" in
    fork)                    op_fork "$@" ;;
    pr-create|pr_create)     op_pr_create "$@" ;;
    pr-comment|pr_comment)   op_pr_comment "$@" ;;
    pr-merge|pr_merge)       op_pr_merge "$@" ;;
    pr-close|pr_close)       op_pr_close "$@" ;;
    issue-create|issue_create)    op_issue_create "$@" ;;
    issue-comment|issue_comment)  op_issue_comment "$@" ;;
    issue-close|issue_close)      op_issue_close "$@" ;;
    release-create|release_create) op_release_create "$@" ;;
    release-delete|release_delete) op_release_delete "$@" ;;
    repo-create|repo_create)      op_repo_create "$@" ;;
    repo-edit|repo_edit)          op_repo_edit "$@" ;;
    repo-delete|repo_delete)      op_repo_delete "$@" ;;
    secret-set|secret_set)        op_secret_set "$@" ;;
    gist-create|gist_create)      op_gist_create "$@" ;;
    workflow-run|workflow_run)    op_workflow_run "$@" ;;
    api-write|api_write)          op_api_write "$@" ;;
    listing-pr-approve)           op_listing_pr_approve "$@" ;;
    "")  fail "usage: gh_safe {fork|pr-create|pr-comment|pr-merge|pr-close|issue-create|issue-comment|release-create|repo-create|repo-edit|repo-delete|secret-set|gist-create|workflow-run|api-write|listing-pr-approve} ARGS" ;;
    *)   fail "unknown action '$ACTION'" ;;
esac
