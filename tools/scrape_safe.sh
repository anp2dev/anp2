#!/usr/bin/env bash
# scrape_safe.sh — rate-limited HTTP GET wrapper for outbound scraping.
#
# Why: seed agents (news / market / weather / citation / etc.) pull data
# from upstream feeds. If a feed thinks we're scraping at bot speed, IP-ban
# follows. Specific risks:
#   - hnrss.org    — RSS, ban via Cloudflare on rapid fetch
#   - feeds.bbci.co.uk — RSS, similar
#   - api.coingecko.com — public API, 30 calls/min cap (paid tier higher)
#   - api.etherscan.io — 5 calls/sec, 100k/day (free tier)
#   - api.open-meteo.com — 10k/day free
#   - mempool.space     — public RPC, no strict cap but courtesy
#   - export.arxiv.org  — 1 req/3 sec courtesy rule (their robots.txt)
#
# This wrapper enforces:
#   - per-host minimum interval (rate limit)
#   - User-Agent header (identifies us, makes blocks specific not blanket)
#   - logs every fetch to internal/env/.scrape-activity-log.jsonl
#
# Usage: scrape_safe.sh fetch <url>
set -euo pipefail

ACTION="${1:-}"
shift || true

LOG=internal/env/.scrape-activity-log.jsonl
mkdir -p internal/env
touch "$LOG"

USER_AGENT='Mozilla/5.0 (compatible; ANP2-Bot/0.2; +https://anp2.com)'

# Per-host minimum interval lookup (seconds). Format: <host>:<seconds>
MIN_INTERVAL_LIST="
api.coingecko.com:3
api.etherscan.io:2
api.open-meteo.com:10
mempool.space:2
hnrss.org:30
feeds.bbci.co.uk:60
export.arxiv.org:4
arxiv.org:4
"
host_min_interval() {
    local h=$1
    echo "$MIN_INTERVAL_LIST" | awk -F: -v host="$h" '$1==host {print $2; exit}'
}

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
fail() { echo "scrape_safe: REFUSED — $1" >&2; log_op fetch "${TARGET:-?}" "REFUSED:$1"; exit 1; }

op_fetch() {
    local url="${1:-}"
    TARGET="$url"
    [ -n "$url" ] || fail "usage: scrape_safe fetch <url>"

    local host; host=$(echo "$url" | sed -E 's|https?://([^/]+).*|\1|')
    local min_int; min_int=$(host_min_interval "$host")
    [ -z "$min_int" ] && min_int=1

    local last_unix; last_unix=$(python3 -c "
import json
last=0
try:
    for line in open('$LOG'):
        e=json.loads(line)
        if e.get('action')=='fetch' and e.get('status')=='OK' and '$host' in e.get('target',''):
            last=max(last, e.get('unix',0))
    print(last)
except: print(0)
")
    local now=$(now_unix)
    local since=$((now - last_unix))
    if [ "$since" -lt "$min_int" ] && [ "$last_unix" -gt 0 ]; then
        local wait=$((min_int - since))
        echo "scrape_safe: throttling $host — sleeping ${wait}s" >&2
        sleep "$wait"
    fi

    if curl -s -A "$USER_AGENT" --max-time 30 "$url"; then
        log_op fetch "$url" "OK"
        return 0
    fi
    log_op fetch "$url" "FAILED"
    return 1
}

case "$ACTION" in
    fetch)  op_fetch "$@" ;;
    "")     fail "usage: scrape_safe {fetch} <url>" ;;
    *)      fail "unknown action '$ACTION'" ;;
esac
