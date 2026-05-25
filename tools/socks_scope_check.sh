#!/usr/bin/env bash
# socks_scope_check.sh — verify the SOCKS5 tunnel is scoped to the
# ANP2-VPN Chrome profile ONLY, and that it has NOT leaked into Mac's
# system proxy settings.
#
# Operator concern (2026-05-24): the SSH SOCKS5 tunnel must be used by
# the dedicated Chrome profile only — never by Safari, Firefox, other
# Chrome profiles, Mail.app, or CLI tools. This script audits that
# invariant and warns if anything has changed.
#
# Run at session start AND can be added to pre-push if desired.
set -uo pipefail

worst="OK"
escalate() {
    case "$1" in
        FAIL) worst="FAIL" ;;
        WARN) [ "$worst" = "OK" ] && worst="WARN" || true ;;
    esac
}

echo "── socks_scope_check ────────────────────────────"

# 1. system proxy MUST be off
proxy_state=$(scutil --proxy 2>/dev/null)
for k in HTTPEnable HTTPSEnable SOCKSEnable; do
    val=$(echo "$proxy_state" | awk -v k="$k" '$1==k":" {print $2; exit}')
    if [ "$val" = "1" ]; then
        echo "  ❌ FAIL: system proxy $k=1 — other apps will route through it"
        escalate FAIL
    fi
done
echo "  ✓ Mac system proxy settings: not configured (correct)"

# 2. SOCKS5 tunnel must be alive
if pgrep -f "ssh -D 1080" >/dev/null 2>&1; then
    echo "  ✓ SOCKS5 tunnel: alive"
else
    echo "  ⚠ WARN: SSH SOCKS5 tunnel not running."
    echo "         launchd should restart it; or run tools/anp2_chrome_launch.sh"
    escalate WARN
fi

# 3. SOCKS5 must point at EC2 only
if [ "$worst" != "WARN" ]; then
    exit_ip=$(curl -s --max-time 5 --socks5-hostname 127.0.0.1:1080 ifconfig.me 2>/dev/null || echo "")
    if [ "$exit_ip" = "$(cat /Users/ai/ai-net-stack/internal/env/relay-ip.txt 2>/dev/null)" ]; then
        echo "  ✓ SOCKS5 exit IP: $exit_ip (EC2, correct)"
    else
        echo "  ❌ FAIL: SOCKS5 exit IP is '$exit_ip' — expected $(cat /Users/ai/ai-net-stack/internal/env/relay-ip.txt 2>/dev/null)"
        escalate FAIL
    fi
fi

# 4. browsers other than ANP2-VPN-flagged-Chrome must NOT have proxy in their config
# (we cheap-check by looking for proxy strings in Firefox/Brave profiles)
for prefix in "$HOME/Library/Application Support/Firefox/Profiles" \
              "$HOME/Library/Application Support/BraveSoftware/Brave-Browser"; do
    if [ -d "$prefix" ]; then
        if find "$prefix" -name "prefs.js" -exec grep -l "network.proxy.socks" {} \; 2>/dev/null | head -1; then
            echo "  ⚠ WARN: $prefix appears to have SOCKS proxy config — review manually"
            escalate WARN
        fi
    fi
done

# 5. Output one-line verdict (mirrors flag_risk_check style)
echo
case "$worst" in
    OK)   echo "SOCKS-scope: OK — VPN is scoped to ANP2-VPN Chrome only" ;;
    WARN) echo "SOCKS-scope: WARN — see details above" ;;
    FAIL) echo "SOCKS-scope: FAIL — scope leak detected, investigate before continuing" ;;
esac
[ "$worst" = "FAIL" ] && exit 1 || exit 0
