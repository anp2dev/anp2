#!/usr/bin/env bash
# service_research.sh — autonomous research + catalog-update protocol for a
# newly-detected external service.
#
# Invoked from service_discovery.sh when an unknown host shows up, OR
# manually by an operator/AI agent that's about to add a new external
# integration. The script does the deterministic part (fetching public
# docs, structuring questions) so the assistant can fill in the answers.
#
# Workflow:
#   1. Fetch the service's robots.txt + /terms + /aup + /tos
#   2. Detect write-API surface (look for /v1/, /api/, OpenAPI specs)
#   3. Emit a draft service_catalog.json entry for review
#   4. Operator/assistant fills in defenses, then runs `catalog-add` to merge
#
# Modes:
#   research <host>   — emit research stub + try to fetch public docs
#   catalog-add <host>  — append the prepared entry to service_catalog.json
set -euo pipefail

ACTION="${1:-}"
HOST="${2:-}"

case "$ACTION" in
    research)
        [ -n "$HOST" ] || { echo "usage: $0 research <host>" >&2; exit 2; }
        echo "═══════════════════════════════════════════════════════"
        echo "Service research: $HOST"
        echo "═══════════════════════════════════════════════════════"
        echo
        echo "Public document URLs to fetch (use WebFetch / curl as appropriate):"
        echo "  https://$HOST/robots.txt"
        echo "  https://$HOST/terms"
        echo "  https://$HOST/tos"
        echo "  https://$HOST/aup"
        echo "  https://$HOST/api-terms"
        echo "  https://$HOST/.well-known/security.txt"
        echo "  https://$HOST/.well-known/ai.txt"
        echo "  https://$HOST/.well-known/openapi.json"
        echo
        echo "Auto-fetch attempt (robots.txt):"
        bash "$(dirname "$0")/scrape_safe.sh" fetch "https://$HOST/robots.txt" 2>/dev/null | head -20 || echo "  (no robots.txt)"
        echo
        echo "Questions to answer (in service_catalog.json entry):"
        echo "  • ban_patterns:   what triggers suspension on this service?"
        echo "  • defense:        wrapper | monitoring | none-needed (read-only)"
        echo "  • status:         PROTECTED | MONITORED | READ_ONLY | DOCUMENTED"
        echo
        echo "If wrapper needed:"
        echo "  • cp tools/publish_safe.sh tools/<service>_safe.sh"
        echo "  • adapt rate caps to the service's documented limits"
        echo "  • add ~/.local/bin/<cli> shim if the service has a CLI"
        echo "  • re-pin: tools/defense_integrity.sh pin"
        echo
        echo "Draft catalog entry (paste into service_catalog.json):"
        cat <<EOF

  "$HOST": {
    "category": "TODO",
    "ban_patterns": ["TODO"],
    "defense": "TODO (wrapper | monitoring | read-only)",
    "status": "TODO (PROTECTED | MONITORED | READ_ONLY | DOCUMENTED)",
    "first_detected": "$(date -u +%Y-%m-%d)"
  }
EOF
        ;;
    catalog-add)
        echo "Manual step: edit tools/service_catalog.json directly with the entry from 'research'."
        echo "Then: bash tools/service_discovery.sh verify"
        ;;
    *)
        echo "usage: $0 {research <host>|catalog-add <host>}" >&2
        exit 2
        ;;
esac
