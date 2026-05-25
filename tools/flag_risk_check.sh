#!/usr/bin/env bash
# flag_risk_check.sh — fast per-message flag-risk verdict.
#
# Designed to run in ≤ 2 seconds at the start of EVERY operator message,
# alongside the policy rule re-read. Returns a single line:
#
#   Flag-risk: OK     — safe to act
#   Flag-risk: WARN   — degraded; avoid cross-repo writes
#   Flag-risk: FAIL   — account flagged or in danger; operator action required
#
# Performs a STRICT subset of account_health.py so the per-message cost stays
# bounded:
#   1. anonymous GET https://github.com/<USER>            (R1)
#   2. anonymous GET https://github.com/<USER>/<REPO>     (R2)
#   3. fork-rate-1h from env/.gh-activity-log.jsonl       (R18 lite)
#   4. push-rate-1h from env/.git-activity-log.jsonl      (R23 lite)
#
# Exit code: 0=OK or WARN, 1=FAIL (so it can be used in `&&` chains).
# Set ANP2_FLAG_RISK_VERBOSE=1 to print all rule lines, not just the verdict.

set -u

USER="${ANP2_GH_USER:-anp2dev}"
REPO="${ANP2_GH_REPO:-anp2}"
verbose="${ANP2_FLAG_RISK_VERBOSE:-0}"

vlog() { [ "$verbose" = "1" ] && echo "  $*" >&2 || true; }

worst="OK"
escalate() {
    # OK -> WARN -> FAIL  (monotonic)
    case "$1" in
        FAIL) worst="FAIL" ;;
        WARN) [ "$worst" = "OK" ] && worst="WARN" || true ;;
    esac
}

# ── R1: account public visibility ────────────────────────────────────
acct_status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 4 "https://github.com/${USER}" 2>/dev/null || echo "?")
if [ "$acct_status" = "200" ]; then
    vlog "R1 acct OK ($USER 200)"
elif [ "$acct_status" = "404" ]; then
    escalate FAIL
    vlog "R1 FAIL: $USER 404 — shadow-suppressed"
else
    escalate WARN
    vlog "R1 WARN: $USER HTTP $acct_status"
fi

# ── R2: repo public visibility ──────────────────────────────────────
repo_status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 4 "https://github.com/${USER}/${REPO}" 2>/dev/null || echo "?")
if [ "$repo_status" = "200" ]; then
    vlog "R2 repo OK (${USER}/${REPO} 200)"
elif [ "$repo_status" = "404" ]; then
    escalate FAIL
    vlog "R2 FAIL: ${USER}/${REPO} 404"
else
    escalate WARN
    vlog "R2 WARN: ${USER}/${REPO} HTTP $repo_status"
fi

# ── R18-lite: fork in last 1h ──────────────────────────────────────
if [ -r "env/.gh-activity-log.jsonl" ]; then
    n=$(python3 -c "
import json,time
cutoff=time.time()-3600
n=0
try:
    for line in open('env/.gh-activity-log.jsonl'):
        e=json.loads(line)
        if e.get('action')=='fork' and e.get('status')=='OK' and e.get('unix',0)>=cutoff: n+=1
    print(n)
except: print(0)
" 2>/dev/null || echo 0)
    if [ "$n" -gt "1" ]; then
        escalate FAIL
        vlog "R18-lite FAIL: $n forks in last 1h"
    else
        vlog "R18-lite OK ($n/1h)"
    fi
fi

# ── shim-PATH check: defense L3 must be active ─────────────────────
# The PATH-front shims at ~/.local/bin/{gh,git,…} only catch calls if
# PATH has them in front. Without that, `gh` / `git` resolves to the
# real binary, fully bypassing wrappers AND hooks (via --no-verify).
# Discovered 2026-05-25 red-team — see [[redteam-findings-2026-05-25]].
case ":$PATH:" in
    *":$HOME/.local/bin:"*) vlog "L3 shim PATH OK" ;;
    *)
        escalate WARN
        vlog "L3 WARN: $HOME/.local/bin not in PATH — shims bypassed; run 'launchctl setenv PATH \"\$HOME/.local/bin:\$PATH\"' then relaunch"
        ;;
esac

# ── R23-lite: push in last 1h ───────────────────────────────────────
if [ -r "env/.git-activity-log.jsonl" ]; then
    n=$(python3 -c "
import json,time
cutoff=time.time()-3600
n=0
try:
    for line in open('env/.git-activity-log.jsonl'):
        e=json.loads(line)
        if e.get('action') in ('push','push-force') and e.get('status')=='OK' and e.get('unix',0)>=cutoff: n+=1
    print(n)
except: print(0)
" 2>/dev/null || echo 0)
    if [ "$n" -gt "2" ]; then
        escalate FAIL
        vlog "R23-lite FAIL: $n pushes in last 1h"
    else
        vlog "R23-lite OK ($n/1h)"
    fi
fi

# ── verdict ──────────────────────────────────────────────────────────
case "$worst" in
    OK)   echo "Flag-risk: OK"; exit 0 ;;
    WARN) echo "Flag-risk: WARN — degrade reads only, no writes"; exit 0 ;;
    FAIL) echo "Flag-risk: FAIL — account flagged; operator action required (see account_health.py for detail)"; exit 1 ;;
esac
