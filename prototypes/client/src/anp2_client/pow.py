"""Proof-of-Work tag for ANP2 (A4 / NIP-13 analogue).

See `docs/research/POW_TAG_DESIGN.md` for the design rationale.

A PoW-tagged event carries `["pow", "<difficulty_bits>"]` plus a
`["nonce", "<integer>"]` that was iterated until SHA256 of the canonical
payload (the event id) has at least `difficulty_bits` leading zero bits.

Verification is one SHA256 + a leading-zero-bit count (JP-redacted) asymmetric work
in the prover's favor (microseconds for the relay vs. seconds for the
miner). The relay re-derives the id from the canonical payload to make
sure the miner did not lie about either the difficulty or the nonce.
"""

from __future__ import annotations

import hashlib
from typing import Any

import rfc8785


def _count_leading_zero_bits(b: bytes) -> int:
    """Number of leading 0 bits in a byte string (big-endian)."""
    count = 0
    for byte in b:
        if byte == 0:
            count += 8
            continue
        for shift in range(7, -1, -1):
            if (byte >> shift) & 1:
                return count
            count += 1
        return count
    return count


def _event_id_bytes(
    agent_id: str,
    created_at: int,
    kind: int,
    tags: list[list[str]],
    content: str,
) -> bytes:
    """SHA256(JCS(canonical_payload)) (JP-redacted) must match spec (JP-redacted)3."""
    payload: list[Any] = [agent_id, created_at, kind, tags, content]
    return hashlib.sha256(rfc8785.dumps(payload)).digest()


def mint_pow(payload: dict, difficulty: int, max_iters: int = 1 << 28) -> int:
    """Mine a nonce so the event id has `difficulty` leading zero bits.

    `payload` MUST already contain `agent_id`, `created_at`, `kind`,
    `tags`, `content`. The function mutates `payload['tags']` in place:
    any existing `pow`/`nonce` tags are stripped, then fresh ones are
    appended. Caller is expected to recompute `id` + `sig` afterward
    (mining changes both).

    Returns the winning nonce as an integer.

    Raises `RuntimeError` if no nonce found within `max_iters` attempts.
    """
    if difficulty < 0 or difficulty > 256:
        raise ValueError(f"difficulty must be in [0, 256], got {difficulty}")

    base_tags = [
        t for t in payload.get("tags", []) if t and t[0] not in ("pow", "nonce")
    ]
    pow_tag = ["pow", str(difficulty)]
    payload["tags"] = base_tags + [pow_tag, ["nonce", "0"]]
    nonce_tag = payload["tags"][-1]

    for nonce in range(max_iters):
        nonce_tag[1] = str(nonce)
        digest = _event_id_bytes(
            payload["agent_id"],
            payload["created_at"],
            payload["kind"],
            payload["tags"],
            payload["content"],
        )
        if _count_leading_zero_bits(digest) >= difficulty:
            return nonce
    raise RuntimeError(
        f"PoW mining exhausted {max_iters} iterations at difficulty {difficulty}"
    )


def verify_pow(event: dict, required_difficulty: int) -> bool:
    """Verify the event's declared & actual PoW meet `required_difficulty`.

    Checks (in order):
      1. A `["pow","<n>"]` tag exists and `n >= required_difficulty`.
      2. The event id (re-derived from canonical payload if available,
         else taken from event['id']) has at least `required_difficulty`
         leading zero bits.

    Returns False on any malformed input rather than raising.
    """
    declared: int | None = None
    for t in event.get("tags", []):
        if t and t[0] == "pow":
            try:
                declared = int(t[1])
            except (ValueError, IndexError, TypeError):
                return False
            break
    if declared is None or declared < required_difficulty:
        return False

    # Re-derive id from payload to defeat forged id+pow pairs.
    try:
        digest = _event_id_bytes(
            event["agent_id"],
            int(event["created_at"]),
            int(event["kind"]),
            event["tags"],
            event["content"],
        )
    except (KeyError, ValueError, TypeError):
        return False

    # Optional consistency check vs declared id.
    declared_id = event.get("id")
    if declared_id and declared_id.lower() != digest.hex():
        return False

    return _count_leading_zero_bits(digest) >= required_difficulty
