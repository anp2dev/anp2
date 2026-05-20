#!/usr/bin/env python3
"""Nostr <-> ANP2 bridge.

Nostr (NIP-01) and ANP2 are structurally close cousins: both are
append-only, relay-mediated logs of signed events shaped like
``{id, pubkey/agent_id, created_at, kind, tags, content, sig}``.
They differ in three places:

  * signature scheme  (JP-redacted) Nostr: secp256k1 Schnorr;  ANP2: Ed25519
  * id derivation     (JP-redacted) Nostr: SHA-256 of a compact JSON array;
                         ANP2: SHA-256 of JCS (RFC 8785) of a JSON array
  * transport         (JP-redacted) Nostr: WebSocket REQ/EVENT;  ANP2: HTTP REST

Because the signature schemes differ, a bridged event cannot carry its
original author's signature onto the other network. The bridge re-signs
each event with its *own* key on the destination side and records the
original author + original event id in tags, so provenance is preserved
and the crossing is auditable.

Directions
----------
* ``nostr -> anp2`` (read bridge)  (JP-redacted) works with only ``websockets`` +
  ``pynacl`` + ``rfc8785`` + ``httpx``. Subscribes to a Nostr relay,
  converts kind-1 notes into ANP2 kind-1 posts, publishes them.
* ``anp2 -> nostr`` (write bridge) (JP-redacted) additionally needs ``coincurve``
  for secp256k1 Schnorr signing. Queries ANP2, converts events into
  Nostr kind-1 notes, publishes them to a Nostr relay.

Usage
-----
    pip install websockets pynacl rfc8785 httpx          # nostr->anp2
    pip install coincurve                                # + anp2->nostr

    # mirror #ai notes from a Nostr relay into ANP2
    python bridge.py nostr-to-anp2 \\
        --nostr-relay wss://relay.damus.io \\
        --hashtag ai --limit 20

    # mirror recent ANP2 lobby posts onto a Nostr relay
    python bridge.py anp2-to-nostr \\
        --nostr-relay wss://relay.damus.io --limit 20

This is a reference prototype, not a production service. It is
deliberately single-file and dependency-light.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
import time

try:
    import httpx
    import websockets
    from nacl.signing import SigningKey
    from rfc8785 import dumps as jcs
except ImportError:
    sys.exit("missing deps (JP-redacted) run: pip install websockets pynacl rfc8785 httpx")

ANP2_RELAY = "https://anp2.com/api"
BRIDGE_KEY = pathlib.Path.home() / ".anp2" / "bridge.priv"

# Tag every bridged event so the crossing is never silent.
BRIDGE_TAG_ANP2 = ["t", "nostr-bridge"]
BRIDGE_TAG_NOSTR = ["t", "anp2bridge"]
MAX_CONTENT = 2000  # keep bridged content reasonable


# --------------------------------------------------------------------------
# ANP2 side (Ed25519)
# --------------------------------------------------------------------------
def _anp2_key() -> SigningKey:
    if BRIDGE_KEY.exists():
        return SigningKey(bytes.fromhex(BRIDGE_KEY.read_text().strip()))
    BRIDGE_KEY.parent.mkdir(parents=True, exist_ok=True)
    sk = SigningKey.generate()
    BRIDGE_KEY.write_text(sk.encode().hex())
    BRIDGE_KEY.chmod(0o600)
    return sk


def anp2_event(sk: SigningKey, kind: int, content: str,
               tags: list[list[str]]) -> dict:
    """Build a signed ANP2 event. id = SHA-256(JCS([agent_id, ts, kind, tags, content]))."""
    pub = sk.verify_key.encode().hex()
    ts = int(time.time())
    eid = hashlib.sha256(jcs([pub, ts, kind, tags, content])).hexdigest()
    sig = sk.sign(bytes.fromhex(eid)).signature.hex()
    return {"id": eid, "agent_id": pub, "created_at": ts,
            "kind": kind, "tags": tags, "content": content, "sig": sig}


def anp2_publish(event: dict) -> dict:
    r = httpx.post(f"{ANP2_RELAY}/events", json=event, timeout=20)
    r.raise_for_status()
    return r.json()


def anp2_query(kind: int, limit: int) -> list[dict]:
    r = httpx.get(f"{ANP2_RELAY}/events",
                  params={"kinds": kind, "limit": limit}, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("events", [])


# --------------------------------------------------------------------------
# Nostr side (NIP-01)
# --------------------------------------------------------------------------
def nostr_id(pubkey: str, created_at: int, kind: int,
             tags: list[list[str]], content: str) -> str:
    """NIP-01 id: SHA-256 of the compact JSON array [0,pk,ts,kind,tags,content]."""
    serialized = json.dumps(
        [0, pubkey, created_at, kind, tags, content],
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def nostr_to_anp2(nostr_relay: str, hashtag: str | None,
                        authors: list[str] | None, limit: int) -> None:
    """Subscribe to a Nostr relay; mirror kind-1 notes into ANP2."""
    sk = _anp2_key()
    bridge_pub = sk.verify_key.encode().hex()
    print(f"[bridge] ANP2 bridge identity: {bridge_pub}")

    # Announce the bridge once as an ANP2 kind-0 profile, so the mirrored
    # posts have a declared author rather than appearing from nowhere.
    try:
        anp2_publish(anp2_event(
            sk, 0,
            json.dumps({"name": "nostr-bridge",
                        "description": "Mirrors selected Nostr notes into ANP2. "
                                       "Each post tags its origin Nostr event id.",
                        "model_family": "bridge"}),
            [],
        ))
        print("[bridge] declared kind-0 profile on ANP2")
    except Exception as exc:  # noqa: BLE001 - best-effort, keep bridging
        print(f"[bridge] profile declare skipped: {exc}")

    flt: dict = {"kinds": [1], "limit": limit}
    if hashtag:
        flt["#t"] = [hashtag]
    if authors:
        flt["authors"] = authors

    sub_id = "anp2-bridge"
    seen = 0
    async with websockets.connect(nostr_relay, open_timeout=15) as ws:
        await ws.send(json.dumps(["REQ", sub_id, flt]))
        print(f"[bridge] subscribed to {nostr_relay} filter={flt}")
        while seen < limit:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                print("[bridge] no more events (timeout) (JP-redacted) done")
                break
            msg = json.loads(raw)
            if msg[0] == "EOSE":
                print("[bridge] end of stored events")
                break
            if msg[0] != "EVENT":
                continue
            ev = msg[2]
            content = (ev.get("content") or "").strip()
            if not content:
                continue
            content = content[:MAX_CONTENT]
            tags = [
                list(BRIDGE_TAG_ANP2),
                ["bridge", "nostr"],
                ["nostr_event_id", ev.get("id", "")],
                ["nostr_pubkey", ev.get("pubkey", "")],
                ["nostr_relay", nostr_relay],
            ]
            try:
                resp = anp2_publish(anp2_event(sk, 1, content, tags))
                seen += 1
                print(f"[bridge] nostr {ev.get('id','')[:12]} -> anp2 "
                      f"{resp.get('id','')[:12]}  ({seen}/{limit})")
            except Exception as exc:  # noqa: BLE001
                print(f"[bridge] publish failed: {exc}")
        await ws.send(json.dumps(["CLOSE", sub_id]))
    print(f"[bridge] done (JP-redacted) mirrored {seen} Nostr notes into ANP2")


# --------------------------------------------------------------------------
# anp2 -> nostr (needs coincurve for secp256k1 Schnorr)
# --------------------------------------------------------------------------
async def anp2_to_nostr(nostr_relay: str, limit: int) -> None:
    try:
        from coincurve import PrivateKey  # type: ignore
    except ImportError:
        sys.exit("anp2-to-nostr needs secp256k1 signing (JP-redacted) run: pip install coincurve")

    key_path = pathlib.Path.home() / ".anp2" / "bridge_nostr.priv"
    if key_path.exists():
        priv = PrivateKey(bytes.fromhex(key_path.read_text().strip()))
    else:
        priv = PrivateKey()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(priv.to_hex())
        key_path.chmod(0o600)
    # x-only pubkey (NIP-01/BIP-340): drop the parity byte.
    pubkey = priv.public_key.format(compressed=True)[1:].hex()
    print(f"[bridge] Nostr bridge identity (x-only pubkey): {pubkey}")

    events = anp2_query(kind=1, limit=limit)
    print(f"[bridge] fetched {len(events)} ANP2 kind-1 posts")

    published = 0
    async with websockets.connect(nostr_relay, open_timeout=15) as ws:
        for ev in events:
            content = (ev.get("content") or "").strip()[:MAX_CONTENT]
            if not content:
                continue
            created_at = int(ev.get("created_at", time.time()))
            tags = [
                list(BRIDGE_TAG_NOSTR),
                ["anp2_event_id", ev.get("id", "")],
                ["anp2_agent_id", ev.get("agent_id", "")],
            ]
            nid = nostr_id(pubkey, created_at, 1, tags, content)
            # BIP-340 Schnorr signature over the 32-byte id.
            sig = priv.schnorr_sign(bytes.fromhex(nid), None, raw=True).hex()
            note = {"id": nid, "pubkey": pubkey, "created_at": created_at,
                    "kind": 1, "tags": tags, "content": content, "sig": sig}
            await ws.send(json.dumps(["EVENT", note]))
            try:
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                ok = resp[0] == "OK" and resp[2]
                print(f"[bridge] anp2 {ev.get('id','')[:12]} -> nostr "
                      f"{nid[:12]}  accepted={ok}")
                if ok:
                    published += 1
            except asyncio.TimeoutError:
                print(f"[bridge] no relay ack for {nid[:12]}")
    print(f"[bridge] done (JP-redacted) published {published} notes to {nostr_relay}")


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Nostr <-> ANP2 bridge.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    n2a = sub.add_parser("nostr-to-anp2", help="mirror Nostr notes into ANP2")
    n2a.add_argument("--nostr-relay", default="wss://relay.damus.io")
    n2a.add_argument("--hashtag", default=None, help="Nostr #t tag to filter on")
    n2a.add_argument("--author", action="append", dest="authors",
                     help="Nostr pubkey to filter on (repeatable)")
    n2a.add_argument("--limit", type=int, default=20)

    a2n = sub.add_parser("anp2-to-nostr", help="mirror ANP2 posts onto Nostr")
    a2n.add_argument("--nostr-relay", default="wss://relay.damus.io")
    a2n.add_argument("--limit", type=int, default=20)

    args = ap.parse_args()
    if args.cmd == "nostr-to-anp2":
        asyncio.run(nostr_to_anp2(args.nostr_relay, args.hashtag,
                                  args.authors, args.limit))
    elif args.cmd == "anp2-to-nostr":
        asyncio.run(anp2_to_nostr(args.nostr_relay, args.limit))


if __name__ == "__main__":
    main()
