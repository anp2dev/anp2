"""Ed25519 signing/verification + canonical event id.

PROTOCOL.md (JP-redacted)3 envelope: id = SHA256(canonical_payload),
canonical_payload = JSON serialization of [agent_id, created_at, kind, tags, content].
sig = Ed25519(id) by agent's private key.
"""

from __future__ import annotations

import hashlib
import json

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
    payload = [agent_id, created_at, kind, tags, content]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
