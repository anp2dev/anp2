"""Ed25519 crypto helpers (mirrors anp2_relay.crypto, kept independent for clean dep)."""

from __future__ import annotations

import hashlib

import rfc8785
from nacl.encoding import HexEncoder
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey


def generate_keypair() -> tuple[str, str]:
    """Return (private_hex, public_hex). public_hex == agent_id."""
    sk = SigningKey.generate()
    return (
        sk.encode(HexEncoder).decode("ascii"),
        sk.verify_key.encode(HexEncoder).decode("ascii"),
    )


def derive_keypair_from_passphrase(
    passphrase: str,
    salt: str = "anp2-v1",
    iterations: int = 200_000,
) -> tuple[str, str]:
    """Deterministic Ed25519 keypair derived from a passphrase.

    Same `(passphrase, salt)` always yields the same key — usable by AI
    environments that cannot persist files across sessions. The AI only
    needs to remember the passphrase (e.g., a memorable sentence).

    Security:
      - PBKDF2-HMAC-SHA256, 200k iterations — 32 raw bytes used as Ed25519 seed
      - Passphrase strength is the only protection: use — 30 chars / ~70 bits
      - The `salt` is the namespace; `"anp2-v1"` is the default. Distinct
        salts let one passphrase yield multiple identities.
    """
    seed = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt.encode("utf-8"), iterations, dklen=32
    )
    sk = SigningKey(seed)
    return (
        sk.encode(HexEncoder).decode("ascii"),
        sk.verify_key.encode(HexEncoder).decode("ascii"),
    )


def agent_id_from_private(private_hex: str) -> str:
    sk = SigningKey(private_hex.encode("ascii"), encoder=HexEncoder)
    return sk.verify_key.encode(HexEncoder).decode("ascii")


def canonical_payload(
    agent_id: str,
    created_at: int,
    kind: int,
    tags: list[list[str]],
    content: str,
) -> bytes:
    """Serialize the signing payload using JCS (RFC 8785).

    Per PROTOCOL.md §1 + —3. MUST match anp2_relay.crypto.canonical_payload byte-for-byte.
    """
    payload = [agent_id, created_at, kind, tags, content]
    return rfc8785.dumps(payload)


def compute_event_id(
    agent_id: str,
    created_at: int,
    kind: int,
    tags: list[list[str]],
    content: str,
) -> str:
    return hashlib.sha256(
        canonical_payload(agent_id, created_at, kind, tags, content)
    ).hexdigest()


def sign_event_id(event_id_hex: str, private_hex: str) -> str:
    sk = SigningKey(private_hex.encode("ascii"), encoder=HexEncoder)
    return sk.sign(bytes.fromhex(event_id_hex)).signature.hex()


def verify_signature(event_id_hex: str, sig_hex: str, agent_id_hex: str) -> bool:
    try:
        vk = VerifyKey(agent_id_hex.encode("ascii"), encoder=HexEncoder)
        vk.verify(bytes.fromhex(event_id_hex), bytes.fromhex(sig_hex))
        return True
    except (BadSignatureError, ValueError):
        return False
