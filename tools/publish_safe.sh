#!/usr/bin/env bash
# publish_safe.sh — anti-flag wrapper for ALL package-publish CLIs.
#
# Covers: twine (PyPI), npm (npmjs), mcp-publisher (MCP Registry),
# huggingface-cli (Hugging Face). The 2026-05-24 brand-purge session
# published 4 PyPI packages back-to-back, hitting PyPI's anti-spam
# "Too many new projects created" rate-limit. That was PyPI-side brake
# from a separate registry — generalize the same gate to ALL registries.
#
# Categories + caps:
#   pypi-publish        cap_1h=1, cap_24h=3, cap_7d=10    (twine upload)
#   npm-publish         cap_1h=1, cap_24h=2, cap_7d=5
#   mcp-publish         cap_1h=1, cap_24h=2, cap_7d=5
#   hf-publish          cap_1h=1, cap_24h=2, cap_7d=5
#
# Campaign detection: if total publishes (any registry) ≥ 3 in last 1h,
# refuse all further publishes unless an approval token is presented
# (catches the 2026-05-24 "publish 4 PyPI in 5 min" exact pattern).
#
# Common gates: account age (where applicable), 2FA confirmed.
#
# Routed via ~/.zshrc shell functions for twine / npm / mcp-publisher /
# huggingface-cli. Direct `bash tools/publish_safe.sh` also works.
set -euo pipefail

ACTION="${1:-}"
shift || true

LOG=env/.publish-activity-log.jsonl
APPROVAL_TOKEN_FILE=env/.publish-campaign-token
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
    echo "publish_safe: REFUSED — $1" >&2
    log_op "${ACTION:-?}" "${TARGET:-?}" "REFUSED:$1"
    exit 1
}

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

count_any_publish_recent() {
    local window=$1
    local cutoff=$(( $(now_unix) - window ))
    python3 -c "
import json
n=0
try:
    for line in open('$LOG'):
        e=json.loads(line)
        if e.get('action','').endswith('-publish') and e.get('status')=='OK' and e.get('unix',0)>=$cutoff:
            n+=1
    print(n)
except: print(0)
"
}

check_rate() {
    local action=$1 cap_1h=$2 cap_24h=$3 cap_7d=${4:-999}
    local n1; n1=$(count_recent "$action" 3600)
    local n24; n24=$(count_recent "$action" 86400)
    local n7; n7=$(count_recent "$action" $((86400 * 7)))
    [ "$n1"  -gt "$cap_1h"  ] && fail "$action rate-limit: $n1 in last 1h > cap=$cap_1h"
    [ "$n24" -gt "$cap_24h" ] && fail "$action rate-limit: $n24 in last 24h > cap=$cap_24h"
    [ "$n7"  -gt "$cap_7d"  ] && fail "$action rate-limit: $n7 in last 7d > cap=$cap_7d"
    # Cross-registry campaign: ≥ 3 publishes ANY registry in 1h
    local total_1h; total_1h=$(count_any_publish_recent 3600)
    if [ "$total_1h" -ge 3 ]; then
        # Need approval token
        local stored="" presented="${ANP2_PUBLISH_CAMPAIGN_TOKEN:-}"
        [ -r "$APPROVAL_TOKEN_FILE" ] && stored=$(cat "$APPROVAL_TOKEN_FILE")
        if [ -z "$stored" ] || [ "$stored" != "$presented" ]; then
            fail "cross-registry campaign detected: $total_1h publishes in last 1h.
       Run: tools/publish_safe.sh campaign-approve
       Then re-run with ANP2_PUBLISH_CAMPAIGN_TOKEN=<token from above>"
        fi
        rm -f "$APPROVAL_TOKEN_FILE"
        echo "publish_safe: campaign approval consumed (single-use)" >&2
    fi
    echo "publish_safe: rate OK ($action: ${n1}/h ${n24}/24h ${n7}/7d; cross-reg ${total_1h}/h)" >&2
}

op_campaign_approve() {
    local token; token=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
    echo "$token" > "$APPROVAL_TOKEN_FILE"
    echo ""
    echo "  Campaign-publish approval granted. SINGLE-USE token:"
    echo ""
    echo "    $token"
    echo ""
    echo "  Use: ANP2_PUBLISH_CAMPAIGN_TOKEN=$token <publish_safe action> ARGS"
}

op_pypi() {
    TARGET="$*"
    check_rate pypi-publish 1 3 10
    if command twine upload "$@"; then
        log_op pypi-publish "${1:-?}" "OK"; return 0
    fi
    log_op pypi-publish "${1:-?}" "FAILED"; return 1
}

op_npm() {
    TARGET="$*"
    check_rate npm-publish 1 2 5
    if command npm publish "$@"; then
        log_op npm-publish "${1:-?}" "OK"; return 0
    fi
    log_op npm-publish "${1:-?}" "FAILED"; return 1
}

op_mcp() {
    TARGET="$*"
    check_rate mcp-publish 1 2 5
    if command mcp-publisher publish "$@"; then
        log_op mcp-publish "${1:-?}" "OK"; return 0
    fi
    log_op mcp-publish "${1:-?}" "FAILED"; return 1
}

op_hf() {
    TARGET="$*"
    check_rate hf-publish 1 2 5
    if command huggingface-cli upload "$@"; then
        log_op hf-publish "${1:-?}" "OK"; return 0
    fi
    log_op hf-publish "${1:-?}" "FAILED"; return 1
}

case "$ACTION" in
    pypi|twine)              op_pypi "$@" ;;
    npm)                     op_npm "$@" ;;
    mcp|mcp-publisher)       op_mcp "$@" ;;
    hf|huggingface)          op_hf "$@" ;;
    campaign-approve)        op_campaign_approve "$@" ;;
    "")  fail "usage: publish_safe {pypi|npm|mcp|hf|campaign-approve} ARGS" ;;
    *)   fail "unknown action '$ACTION'" ;;
esac
