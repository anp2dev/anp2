"""Server-side Proof-of-Work tag verification for ANP2.

Mirror of `anp2_client.pow` (see docs/research/POW_TAG_DESIGN.md). The
client mints a nonce so that the SHA256 of the JCS-canonical event payload
(i.e. the event `id`) has at least `pow_bits` leading zero bits. The relay
verifies the claim with a single SHA256 + leading-zero-count.

This module is consumed by:
  - `server.py` (JP-redacted) to validate kind 6 trust_vote events at publish time when
    they carry a `pow` tag (PIP-002 (JP-redacted)1).
  - `trust.py` (JP-redacted) to read the declared `pow_bits` per kind 6 vote and feed
    `sybil_factor(v) = tanh((JP-redacted) 2^pow_bits / NORM_CONSTANT)` (PIP-002 (JP-redacted)3).

Per PIP-002 the relay's current minimum is `PIP_002_MIN_BITS` (default 12).
A kind 6 event WITHOUT a `pow` tag remains accepted (backwards-compatible
with pre-PIP-002 voters) but contributes zero PoW work to `sybil_factor`.
A kind 6 event WITH a `pow` tag that does not actually meet the declared
bits is rejected at publish time (HTTP 400) (JP-redacted) lying about PoW is a hard
fail because honest miners pay the cost.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

import rfc8785

# Per PIP-002 (JP-redacted)2: phase-1 floor is 12 bits (~4096 hashes, ~10 ms on a CPU).
# A relay MAY raise this without a new PIP ((JP-redacted)2 scaling_policy); historical
# votes are NOT re-validated against a raised floor.
PIP_002_MIN_BITS = 12
PIP_002_MAX_BITS = 24

# tanh normalization. 2^16 = 65536 expected hashes (JP-redacted) "a few seconds of
# mining" per PIP-002 (JP-redacted)3. Convergence target: 10 honest medium-trust votes
# at the 12-bit floor land sybil_factor (JP-redacted) 0.7.
SYBIL_NORM_CONSTANT = 1 << 16


def count_leading_zero_bits(b: bytes) -> int:
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


def event_id_bytes(
    agent_id: str,
    created_at: int,
    kind: int,
    tags: list[list[str]],
    content: str,
) -> bytes:
    """SHA256(JCS(payload)) (JP-redacted) re-derives the canonical event id."""
    payload = [agent_id, created_at, kind, tags, content]
    return hashlib.sha256(rfc8785.dumps(payload)).digest()


def extract_pow_bits(tags: Iterable[list[str]]) -> int | None:
    """Return the declared `pow_bits` from a `["pow","<n>"]` tag, or None.

    Returns None when no pow tag exists or when the value is unparseable.
    A malformed pow tag is treated the same as a missing one for sybil_factor
    aggregation; the publish-time validator separately rejects malformed pow
    tags with HTTP 400 so they never reach the trust graph.
    """
    for t in tags:
        if t and len(t) >= 2 and t[0] == "pow":
            try:
                bits = int(t[1])
            except (ValueError, TypeError):
                return None
            if 0 <= bits <= 256:
                return bits
            return None
    return None


def validate_kind6_pow(
    event_id_hex: str,
    agent_id: str,
    created_at: int,
    kind: int,
    tags: list[list[str]],
    content: str,
    min_bits: int = PIP_002_MIN_BITS,
) -> tuple[bool, str | None]:
    """Validate a kind 6 event's PoW claim, if any.

    Returns (ok, error_detail). Semantics:
      - No `pow` tag present  (JP-redacted) (True, None). PoW is optional for kind 6 in
        the current phase; absent-PoW votes still propagate trust but
        contribute zero PoW work to `sybil_factor`.
      - Malformed `pow` tag   (JP-redacted) (False, "pow tag malformed").
      - Declared bits below relay minimum (JP-redacted) (False, "pow_below_minimum").
      - Declared bits above max          (JP-redacted) (False, "pow_above_max").
      - Declared bits OK but actual leading-zero count of the canonical id
        is below declared                (JP-redacted) (False, "pow_does_not_meet_declared").
      - All checks pass                  (JP-redacted) (True, None).

    The id is re-derived from the canonical payload to defeat forged pairs
    of (id, pow_tag) that don't actually correspond.
    """
    declared = extract_pow_bits(tags)
    if declared is None:
        # Distinguish "no pow tag" (allowed) from "malformed pow tag" (reject).
        has_pow_tag = any(t and len(t) >= 1 and t[0] == "pow" for t in tags)
        if has_pow_tag:
            return False, "pow tag malformed"
        return True, None

    if declared < min_bits:
        return False, f"pow_below_minimum (declared {declared}, min {min_bits})"
    if declared > PIP_002_MAX_BITS:
        return False, f"pow_above_max (declared {declared}, max {PIP_002_MAX_BITS})"

    derived = event_id_bytes(agent_id, created_at, kind, tags, content)
    # Consistency: declared id (event.id) must match the re-derived hash.
    if event_id_hex.lower() != derived.hex():
        return False, "pow id mismatch (event id != derived canonical hash)"

    actual = count_leading_zero_bits(derived)
    if actual < declared:
        return False, f"pow_does_not_meet_declared (claimed {declared} bits, found {actual})"
    return True, None
