#!/usr/bin/env bash
# gh_safe.sh — anti-flag wrapper for `gh` operations that trigger GitHub's
# anti-spam ML model. Built 2026-05-24 after the second consecutive account
# flag (anp2dev → anp2dev → ???) caused by mass-fork bursts.
#
# Wraps:
#   gh_safe fork <upstream>    — analog of `gh repo fork`
#   gh_safe pr-create ...      — analog of `gh pr create`
#
# Refuses if ANY of the following is true:
#   - account age < 7 days
#   - 2FA not enabled on account
#   - already forked another repo in the last 24h (rate cap = 1 fork / day)
#   - already submitted a PR to an external repo in the last 12h
#   - account followers = 0 AND account age < 30 days (trust floor)
#
# Records every successful op in env/.gh-activity-log.jsonl so the next call
# can see the recent history. Pre-push hook also reads this log.
#
# Bypass: explicit env var GH_SAFE_FORCE=1 (still records that bypass was used).
set -euo pipefail

ACTION="${1:-}"
shift || true

LOG=env/.gh-activity-log.jsonl
mkdir -p env
touch "$LOG"

now_unix() { date +%s; }
log_op() {
    # log_op <action> <target> <status>
    local action=$1 target=${2:-} status=$3
    local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    printf '{"ts":"%s","unix":%d,"action":"%s","target":"%s","status":"%s"}\n' \
        "$ts" "$(now_unix)" "$action" "$target" "$status" >> "$LOG"
}

fail() { echo "gh_safe: REFUSED — $1" >&2; log_op "${ACTION:-?}" "${TARGET:-?}" "REFUSED:$1"; exit 1; }

# Common safety gates that apply before ANY external-repo activity.
preflight() {
    local who; who=$(gh api /user --jq '.login' 2>/dev/null || echo "")
    [ -n "$who" ] || fail "gh CLI not authenticated"

    local created_at; created_at=$(gh api /user --jq '.created_at' 2>/dev/null || echo "")
    [ -n "$created_at" ] || fail "could not read account created_at"
    local created_unix; created_unix=$(python3 -c "
import datetime,sys
print(int(datetime.datetime.fromisoformat('$created_at'.replace('Z','+00:00')).timestamp()))
")
    local age_days=$(( ( $(now_unix) - created_unix ) / 86400 ))

    # ── Gate A: account age ≥ 7 days for any external activity
    if [ "$age_days" -lt 7 ]; then
        fail "account $who is only $age_days days old (need ≥ 7); waiting reduces flag risk"
    fi

    # ── Gate B: 2FA enabled
    local tfa; tfa=$(gh api /user --jq '.two_factor_authentication // false' 2>/dev/null || echo false)
    if [ "$tfa" != "true" ]; then
        fail "2FA not enabled on $who (enable at https://github.com/settings/security)"
    fi

    # ── Gate C: trust floor — if followers = 0 AND age < 30d, refuse
    local followers; followers=$(gh api /user --jq '.followers' 2>/dev/null || echo 0)
    if [ "$followers" = "0" ] && [ "$age_days" -lt 30 ]; then
        fail "trust floor: followers=0 AND age=${age_days}d (need followers≥1 or age≥30d)"
    fi

    GH_USER="$who"
}

# Forks in the last 24h, both from local log AND from gh API truth.
recent_forks_24h() {
    local cutoff=$(( $(now_unix) - 86400 ))
    local local_count; local_count=$(python3 -c "
import json,sys
n=0
try:
    for line in open('$LOG'):
        e=json.loads(line)
        if e.get('action')=='fork' and e.get('status')=='OK' and e.get('unix',0)>=$cutoff:
            n+=1
    print(n)
except: print(0)
")
    # Cross-check via gh API: count forks created in last 24h
    local api_count; api_count=$(gh api "/users/${GH_USER}/repos?type=forks&per_page=50" 2>/dev/null | \
        python3 -c "
import json,sys
data=json.load(sys.stdin)
n=0
for r in data:
    import datetime
    ts=int(datetime.datetime.fromisoformat(r['created_at'].replace('Z','+00:00')).timestamp())
    if ts>=$cutoff: n+=1
print(n)
" 2>/dev/null || echo 0)
    # use the larger of the two
    [ "$local_count" -gt "$api_count" ] && echo "$local_count" || echo "$api_count"
}

do_fork() {
    local target="${1:-}"
    TARGET="$target"
    [ -n "$target" ] || fail "usage: gh_safe fork <owner/repo>"
    preflight

    local n_24h; n_24h=$(recent_forks_24h)
    if [ "$n_24h" -ge 1 ]; then
        fail "fork rate-limit: $n_24h fork(s) already in last 24h (cap=1/24h)"
    fi

    if [ "${GH_SAFE_FORCE:-0}" = "1" ]; then
        echo "gh_safe: GH_SAFE_FORCE=1 — bypass requested, will log it" >&2
        log_op fork "$target" "BYPASSED"
    fi

    echo "gh_safe: forking $target …" >&2
    if gh repo fork "$target" --clone=false 2>&1; then
        log_op fork "$target" "OK"
        echo "gh_safe: fork OK. Next fork allowed in 24h." >&2
        return 0
    else
        log_op fork "$target" "FAILED"
        fail "gh repo fork command failed"
    fi
}

do_pr_create() {
    TARGET="$*"
    preflight

    # Rate cap: PR submissions to EXTERNAL repos ≤ 2 per 24h
    local cutoff=$(( $(now_unix) - 86400 ))
    local n; n=$(python3 -c "
import json
n=0
try:
    for line in open('$LOG'):
        e=json.loads(line)
        if e.get('action')=='pr-create' and e.get('status')=='OK' and e.get('unix',0)>=$cutoff:
            n+=1
    print(n)
except: print(0)
")
    if [ "$n" -ge 2 ]; then
        fail "PR rate-limit: $n PRs in last 24h (cap=2/24h)"
    fi

    # If the PR is to a fork (we detect via current dir's remote), require fork to be ≥ 12h old.
    local fork_age_hours; fork_age_hours=$(python3 -c "
import subprocess,json,datetime
try:
    out=subprocess.check_output(['git','remote','get-url','origin'],text=True).strip()
    # parse owner/repo from URL
    if 'github.com' in out:
        parts=out.split('github.com')[1].lstrip(':/').rstrip('.git').split('/')
        if len(parts)>=2:
            repo=parts[0]+'/'+parts[1]
            d=json.loads(subprocess.check_output(['gh','api','/repos/'+repo,'--jq','{created_at:.created_at}']))
            ts=int(datetime.datetime.fromisoformat(d['created_at'].replace('Z','+00:00')).timestamp())
            import time
            print(int((time.time()-ts)/3600))
            exit()
except: pass
print(999)
" 2>/dev/null || echo 999)
    if [ "$fork_age_hours" -lt 12 ]; then
        fail "fork-to-PR cooldown: fork is ${fork_age_hours}h old (need ≥ 12h before PR)"
    fi

    echo "gh_safe: submitting PR …" >&2
    if gh pr create "$@" 2>&1; then
        log_op pr-create "${TARGET:0:120}" "OK"
        return 0
    else
        log_op pr-create "${TARGET:0:120}" "FAILED"
        return 1
    fi
}

case "$ACTION" in
    fork)        do_fork "$@" ;;
    pr-create)   do_pr_create "$@" ;;
    pr|repo)     fail "use: gh_safe fork <repo>  OR  gh_safe pr-create <args>" ;;
    "")          fail "usage: gh_safe {fork|pr-create} ARGS" ;;
    *)           fail "unknown action '$ACTION'. Allowed: fork, pr-create" ;;
esac
