#!/usr/bin/env bash
# git_safe.sh — anti-flag wrapper for git ops that interact with public remotes.
#
# Companion to tools/gh_safe.sh. The pre-push hook already runs leak_audit
# + account_health, but that's per-repo. This wrapper adds:
#
#   - rate caps on `git push` itself (not just push events GitHub sees)
#   - extra approval token for `git push --force[-with-lease]`
#   - block on suspicious `git config user.email` changes (forbidden patterns)
#   - audit log of `git remote add` (new remote = new public surface)
#
# Routed via ~/.zshrc `git()` shell function. Direct `bash tools/git_safe.sh`
# also works.
#
# Categories + caps (env-var overrideable):
#   push           cap_1h=2, cap_24h=5, cap_7d=15   # matches R17 push-events
#   push-force     cap_1h=1, cap_24h=1, cap_7d=2    # rare, deliberate
#   remote-add     cap_24h=2                         # new public surface
#   config-email   cap_24h=1                         # change-flap detection
#
# Force-push specifically requires a single-use approval token
# (`tools/git_safe.sh force-push-approve`) to prevent silent rewrites of
# public history.
set -euo pipefail

ACTION="${1:-}"
shift || true

LOG=env/.git-activity-log.jsonl
APPROVAL_TOKEN_FILE=env/.git-force-push-token
mkdir -p env
touch "$LOG"

now_unix() { date +%s; }
log_op() {
    local action=$1 target=${2:-} status=$3
    local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    # Escape newlines + quotes in fields so the JSON line stays parseable.
    local esc_target esc_status
    esc_target=$(printf '%s' "$target" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read())[1:-1])')
    esc_status=$(printf '%s' "$status" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read())[1:-1])')
    printf '{"ts":"%s","unix":%d,"action":"%s","target":"%s","status":"%s"}\n' \
        "$ts" "$(now_unix)" "$action" "$esc_target" "$esc_status" >> "$LOG"
}
fail() {
    echo "git_safe: REFUSED — $1" >&2
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

check_rate() {
    local action=$1 cap_1h=$2 cap_24h=$3 cap_7d=${4:-999}
    local n1; n1=$(count_recent "$action" 3600)
    local n24; n24=$(count_recent "$action" 86400)
    local n7; n7=$(count_recent "$action" $((86400 * 7)))
    [ "$n1"  -gt "$cap_1h"  ] && fail "$action rate-limit: $n1 in last 1h > cap=$cap_1h"
    [ "$n24" -gt "$cap_24h" ] && fail "$action rate-limit: $n24 in last 24h > cap=$cap_24h"
    [ "$n7"  -gt "$cap_7d"  ] && fail "$action rate-limit: $n7 in last 7d > cap=$cap_7d"
    echo "git_safe: rate OK ($action: ${n1}/h ${n24}/24h ${n7}/7d)" >&2
}

# ── Operations ───────────────────────────────────────────────────────

op_push() {
    TARGET="$*"
    # Detect force flags
    local is_force=0
    for arg in "$@"; do
        case "$arg" in
            --force|--force-with-lease|-f|--force-with-lease=*)
                is_force=1 ;;
        esac
    done

    if [ "$is_force" = "1" ]; then
        op_push_force "$@"
        return
    fi

    # Push-discipline window (R30, freeze period only).
    # Defense-in-depth against `--no-verify`: pre-push hook also enforces
    # R29 + R30 via account_health.py, but `--no-verify` bypasses pre-push,
    # so we keep an independent check here at the wrapper layer.
    check_push_window || return 1

    check_rate push 2 5 15
    if git push "$@"; then
        log_op push "${TARGET:0:120}" "OK"
        return 0
    fi
    log_op push "${TARGET:0:120}" "FAILED"
    return 1
}

# Returns 0 if push is currently allowed by R30 (or freeze period passed).
# Returns 1 with operator-facing message if window is closed.
check_push_window() {
    local freeze_end="${ANP2_FREEZE_END_DATE:-2026-06-24}"
    local today utc_hour win_start win_end
    today=$(date -u +%Y-%m-%d)
    # date string compare works since YYYY-MM-DD sorts lexicographically.
    if [ "$today" \> "$freeze_end" ]; then
        return 0  # post-freeze — rule auto-deactivates
    fi
    utc_hour=$(date -u +%-H)
    win_start="${ANP2_PUSH_WIN_UTC_START:-13}"
    win_end="${ANP2_PUSH_WIN_UTC_END:-16}"
    # window is [start, end). Wrap not supported here — defaults [13, 16).
    if [ "$utc_hour" -ge "$win_start" ] && [ "$utc_hour" -lt "$win_end" ]; then
        return 0
    fi
    echo "git_safe: push BLOCKED — current UTC hour ${utc_hour} outside" >&2
    echo "          configured push window [${win_start}, ${win_end}) (freeze rule R30)." >&2
    echo "          Wait for the window to open, or set ANP2_PUSH_WINDOW_OVERRIDE=1" >&2
    echo "          for a one-shot emergency override (logged)." >&2
    if [ -n "${ANP2_PUSH_WINDOW_OVERRIDE:-}" ]; then
        # NOTE: this is NOT single-use — the env var persists across calls
        # within the same shell. The override is meant for one-off
        # emergencies; if it's exported across a session, every push will
        # bypass R30. Logged each time so abuse is visible in the log.
        echo "          OVERRIDE present — proceeding (each use is logged)" >&2
        log_op push-window-override "today=${today} hour=${utc_hour}" "OVERRIDE"
        return 0
    fi
    return 1
}

op_push_force() {
    TARGET="$*"
    # Force-push requires single-use approval token
    local stored="" presented="${ANP2_GIT_FORCE_PUSH_TOKEN:-}"
    [ -r "$APPROVAL_TOKEN_FILE" ] && stored=$(cat "$APPROVAL_TOKEN_FILE")
    if [ -z "$stored" ] || [ "$stored" != "$presented" ]; then
        fail "force-push to '$*' requires an approval token.
       Run: tools/git_safe.sh force-push-approve
       Then re-run with ANP2_GIT_FORCE_PUSH_TOKEN=<token from above>"
    fi
    # consume token
    rm -f "$APPROVAL_TOKEN_FILE"
    echo "git_safe: force-push approval consumed (single-use)" >&2

    check_rate push-force 1 1 2
    if git push "$@"; then
        log_op push-force "${TARGET:0:120}" "OK"
        return 0
    fi
    log_op push-force "${TARGET:0:120}" "FAILED"
    return 1
}

op_force_push_approve() {
    local token; token=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
    echo "$token" > "$APPROVAL_TOKEN_FILE"
    echo ""
    echo "  Force-push approval granted. SINGLE-USE token:"
    echo ""
    echo "    $token"
    echo ""
    echo "  To use:"
    echo "    ANP2_GIT_FORCE_PUSH_TOKEN=$token git push --force-with-lease origin main"
    echo ""
    echo "  Token is consumed on first successful force-push."
}

op_remote_add() {
    TARGET="$*"
    local name="${1:-}" url="${2:-}"
    [ -n "$name" ] && [ -n "$url" ] || fail "usage: git_safe remote-add <name> <url>"
    check_rate remote-add 0 2
    if git remote add "$name" "$url"; then
        log_op remote-add "$name=$url" "OK"
        return 0
    fi
    log_op remote-add "$name=$url" "FAILED"
    return 1
}

op_config_email() {
    TARGET="$*"
    local scope_or_email="${1:-}"
    local maybe_email="${2:-}"
    local new_email
    if [[ "$scope_or_email" == "--global" || "$scope_or_email" == "--local" ]]; then
        new_email="$maybe_email"
    else
        new_email="$scope_or_email"
    fi
    # Patterns that look like founder/admin/host-bearing emails are rule
    # leak + flag risk.
    if echo "$new_email" | grep -i -E '\bfounder@|\.local$|admin@|root@|\badmin\b' >/dev/null; then
        fail "email '$new_email' matches founder/admin/host-bearing pattern (rule + flag risk)"
    fi
    check_rate config-email 0 1
    if git config "$@" user.email "$new_email" 2>/dev/null || git config user.email "$new_email"; then
        log_op config-email "$new_email" "OK"
        return 0
    fi
    log_op config-email "$new_email" "FAILED"
    return 1
}

case "$ACTION" in
    push)                       op_push "$@" ;;
    push-force|force-push)      op_push_force "$@" ;;
    force-push-approve)         op_force_push_approve "$@" ;;
    remote-add)                 op_remote_add "$@" ;;
    config-email)               op_config_email "$@" ;;
    "")  fail "usage: git_safe {push|push-force|remote-add|config-email|force-push-approve} ARGS" ;;
    *)   fail "unknown action '$ACTION'" ;;
esac
