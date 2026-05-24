#!/usr/bin/env bash
# service_discovery.sh — scan tracked files for external-service URLs and
# compare against tools/service_catalog.json. Any host NOT in the catalog
# is reported as an unknown service that needs defense decision.
#
# Modes:
#   verify   — default; exit 1 if any unknown service is found
#   list     — list all detected hosts with their catalog status
#   research-stub <host>  — emit a research template for a new host
#
# Called by pre-push hook + can be run at session start.
set -euo pipefail

CATALOG=tools/service_catalog.json

# Hosts we ignore (RFC-reserved test domains, our own, localhost, etc.)
IGNORE_REGEX='^(localhost|127\.0\.0\.1|0\.0\.0\.0|anp2\.com|www\.anp2\.com|relay-eu\.anp2\.com|api\.anp2\.com|example\.com|example\.org|example\.test|example\.net|my-relay\.example\.com|169\.254\.169\.254)$'

# Hosts already in catalog (any nesting depth)
catalog_hosts() {
    [ -r "$CATALOG" ] || return
    python3 -c "
import json
with open('$CATALOG') as f:
    cat = json.load(f)
def walk(d, out):
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict) and ('status' in v or 'ban_patterns' in v):
                # k is a host entry name
                # Strip narrative prefixes like 'AWS / EC2 (relay host …)'
                host = k.split()[0].rstrip(',').lower()
                out.add(host)
                # Also try to extract domain-like strings from key
                import re
                for m in re.findall(r'[a-z0-9][a-z0-9.-]+\.[a-z]{2,}', k.lower()):
                    out.add(m)
            else:
                walk(v, out)
out = set()
walk(cat, out)
print('\n'.join(sorted(out)))
"
}

# All hosts referenced in tracked code
discovered_hosts() {
    git ls-files | xargs grep -ohI "https\?://[a-zA-Z0-9][a-zA-Z0-9.-]*[a-zA-Z0-9]" 2>/dev/null | \
        sed -E 's|https?://([^/]+).*|\1|' | tr '[:upper:]' '[:lower:]' | \
        sed -E 's|\.+$||' | sort -u | \
        grep -v -E "$IGNORE_REGEX" | \
        grep -v -E '^anp2\.com$|^.*\.anp2\.com$|^anp2-' || true
}

ACTION="${1:-verify}"

case "$ACTION" in
    list)
        printf '%-45s  %s\n' "HOST" "STATUS"
        known=$(catalog_hosts | sort -u | tr '\n' '|')
        for h in $(discovered_hosts); do
            if echo "$h" | grep -E "^(${known%|})$" >/dev/null; then
                printf '%-45s  %s\n' "$h" "in-catalog"
            else
                printf '%-45s  %s\n' "$h" "UNKNOWN — needs defense decision"
            fi
        done
        ;;
    verify)
        unknown=0
        known=$(catalog_hosts | sort -u | tr '\n' '|')
        known_pat="^(${known%|})\$"
        for h in $(discovered_hosts); do
            if ! echo "$h" | grep -E "$known_pat" >/dev/null 2>&1; then
                if [ "$unknown" = "0" ]; then
                    echo "service_discovery: unknown external service(s) referenced:" >&2
                fi
                echo "  $h" >&2
                unknown=$((unknown + 1))
            fi
        done
        if [ "$unknown" -gt 0 ]; then
            echo "" >&2
            echo "service_discovery: $unknown unknown host(s) found." >&2
            echo "  • Research each: tools/service_discovery.sh research-stub <host>" >&2
            echo "  • Then add an entry to tools/service_catalog.json" >&2
            echo "  • Then re-run verify." >&2
            exit 1
        fi
        # count entries in catalog for reporting
        n_known=$(echo "$known" | tr '|' '\n' | grep -c .)
        n_disc=$(discovered_hosts | grep -c . || echo 0)
        echo "service_discovery: $n_disc external host(s) referenced — all in catalog" >&2
        ;;
    research-stub)
        host="${2:-}"
        [ -n "$host" ] || { echo "usage: $0 research-stub <host>" >&2; exit 2; }
        cat <<EOF
RESEARCH TEMPLATE for new external service: $host

Investigate (operator can paste these into a WebFetch / search session):

1. AUP / Terms of Service URL:
   https://$host/terms (or /tos, /acceptable-use, /aup)

2. Known rate limits (look in their docs API page):
   - per-account per-day
   - per-IP per-hour
   - per-endpoint specific caps

3. Ban / shadow-ban / suspension triggers:
   - mass-creation patterns
   - automation / bot detection
   - account-age requirements
   - 2FA / verification floors

4. Write capability we will exercise:
   - upload / publish / post / comment / API write?
   - If yes, build a wrapper similar to tools/publish_safe.sh
   - If no (read-only), add as READ_ONLY in catalog

5. Add to tools/service_catalog.json:

  "<category>": {
    "$host": {
      "ban_patterns": ["…", "…"],
      "defense": "wrapper | monitoring | read-only",
      "status": "PROTECTED | MONITORED | READ_ONLY"
    }
  }

6. If wrapper needed, copy tools/publish_safe.sh and adapt rate caps.
7. Re-pin: tools/defense_integrity.sh pin
EOF
        ;;
    *)
        echo "usage: $0 {verify|list|research-stub <host>}" >&2
        exit 2
        ;;
esac
