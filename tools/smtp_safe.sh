#!/usr/bin/env bash
# smtp_safe.sh — gate outbound email volume from mail.anp2.com.
#
# Why: mail.anp2.com is an SMTP host. SMTP IP reputation collapses fast if:
#   - high outbound rate from a new IP
#   - high bounce rate
#   - spam complaint rate > 0.1%
#   - sending to spamtraps
#
# Currently the relay does NOT send transactional email. If we add it,
# this wrapper enforces:
#   - cap 10 emails / day initially (warm-up)
#   - cap 100 / day at 30 days IP age
#   - bounce-rate monitoring (require operator review if > 2%)
#   - no mass send (≥ 5 recipients in 1h needs approval token)
set -euo pipefail
echo "smtp_safe: outbound email is NOT enabled in this stack." >&2
echo "  If you add email-sending code, wire it through this wrapper" >&2
echo "  (cap 10/day warm-up, ≤ 100/day mature, mass-send needs token)." >&2
exit 1
