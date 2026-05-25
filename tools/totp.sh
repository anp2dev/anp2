#!/usr/bin/env bash
# totp.sh — generate current TOTP 6-digit code from a stored base32 secret.
# Secrets live in internal/env/REGISTRATIONS.md under "## TOTP secret: <label>" stanzas.
# Reads one by label name and prints the current 6-digit code.
#
# Usage:
#   tools/totp.sh <label>                          — print code for that label
#   tools/totp.sh --add <label> <base32-secret>    — store a new secret
set -euo pipefail

REG=internal/env/REGISTRATIONS.md
LABEL="${1:?usage: tools/totp.sh <label>  OR  --add <label> <base32>}"

if [ "$LABEL" = "--add" ]; then
    LABEL="${2:?missing label}"
    SECRET="${3:?missing base32 secret}"
    {
        echo
        echo "## TOTP secret: $LABEL"
        echo "Stored: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "Base32: $SECRET"
    } >> "$REG"
    chmod 600 "$REG"
    echo "stored TOTP secret for label='$LABEL' (length=${#SECRET})"
    exit 0
fi

SECRET=$(awk -v lbl="$LABEL" '
    $0 == "## TOTP secret: " lbl { in_block=1; next }
    in_block && /^Base32: / { sub(/^Base32: /, ""); print; exit }
' "$REG")

if [ -z "$SECRET" ]; then
    echo "no TOTP secret for label='$LABEL'" >&2
    echo "available labels:" >&2
    grep -oE "^## TOTP secret: .*" "$REG" 2>/dev/null | sed 's/^## TOTP secret: /  /' >&2
    exit 1
fi

python3 - "$SECRET" <<'PYEOF'
import sys, time, hmac, hashlib, base64
secret = sys.argv[1].upper().replace(" ", "").replace("-", "")
key = base64.b32decode(secret + "=" * (-len(secret) % 8))
T = int(time.time() // 30)
h = hmac.new(key, T.to_bytes(8, "big"), hashlib.sha1).digest()
offset = h[-1] & 0x0F
code = (((h[offset] & 0x7F) << 24) | (h[offset+1] << 16) | (h[offset+2] << 8) | h[offset+3]) % 10**6
secs = 30 - int(time.time() % 30)
print(f"{code:06d}  (valid for ~{secs}s)")
PYEOF
