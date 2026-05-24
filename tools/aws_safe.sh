#!/usr/bin/env bash
# aws_safe.sh — gate AWS API write operations from this stack.
#
# Why: the relay runs on AWS EC2 (IP loaded from env). AWS account flags can
# result from:
#   - high outbound traffic (DDoS amplification suspicion)
#   - crypto-mining patterns (CPU sustained 100% with chain RPC outbound)
#   - mass instance creation
#   - root credential leak
#
# Current stack does NOT do AWS API writes from the operator's local box —
# all infra changes happen via SSH to the existing instance. This wrapper
# documents that boundary and gates the `aws` CLI if it ever gets used.
set -euo pipefail

ACTION="${1:-}"
shift || true

if [ -z "$ACTION" ]; then
    echo "aws_safe: AWS CLI is not currently used by claude-tools." >&2
    echo "  If you add AWS API calls, wire them through this wrapper" >&2
    echo "  with rate caps + write-method detection (POST/PUT/DELETE)." >&2
    exit 1
fi

# Read-only verbs pass through
case "$ACTION" in
    describe-*|list-*|get-*|head-*)
        exec aws "$ACTION" "$@" ;;
esac

echo "aws_safe: write op '$ACTION' requires operator approval token." >&2
echo "  Issue token: tools/aws_safe.sh approve" >&2
exit 1
