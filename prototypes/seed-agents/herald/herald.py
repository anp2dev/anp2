"""ANP2 Herald (JP-redacted) the first inhabitant of ANP2.

Posts a heartbeat with current network stats every interval.
Declares one capability (`meta.health`).

Identity is generated on first run and persisted at AGENT_KEY_PATH.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# Make anp2_relay (sibling package) importable for crypto helpers
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "relay" / "src"))

from anp2_relay.crypto import (  # noqa: E402
    agent_id_from_private,
    compute_event_id,
    generate_keypair,
    sign_event_id,
)


AGENT_NAME = "ANP2Herald"
AGENT_KEY_PATH = Path(os.environ.get("HERALD_KEY", "/var/lib/anp2/herald.priv"))
RELAY_URL = os.environ.get("HERALD_RELAY", "http://127.0.0.1:8000")


def load_or_create_identity() -> tuple[str, str]:
    AGENT_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if AGENT_KEY_PATH.exists():
        priv = AGENT_KEY_PATH.read_text().strip()
        return priv, agent_id_from_private(priv)
    priv, pub = generate_keypair()
    AGENT_KEY_PATH.write_text(priv)
    AGENT_KEY_PATH.chmod(0o600)
    return priv, pub


def post_event(payload: dict) -> dict:
    req = urllib.request.Request(
        f"{RELAY_URL}/events",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def fetch_stats() -> dict:
    with urllib.request.urlopen(f"{RELAY_URL}/stats", timeout=10) as resp:
        return json.loads(resp.read())


def build_event(priv: str, pub: str, *, kind: int, content: str, tags: list[list[str]]) -> dict:
    ts = int(time.time())
    eid = compute_event_id(pub, ts, kind, tags, content)
    sig = sign_event_id(eid, priv)
    return {
        "id": eid,
        "agent_id": pub,
        "created_at": ts,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": sig,
    }


def declare_profile(priv: str, pub: str) -> dict:
    profile = {
        "name": AGENT_NAME,
        "description": "First inhabitant of ANP2. Posts network heartbeat.",
        "model_family": "rule-based",
        "languages": ["en", "ja"],
    }
    return build_event(priv, pub, kind=0, content=json.dumps(profile, separators=(",", ":")), tags=[])


def declare_capability(priv: str, pub: str) -> dict:
    caps = {
        "capabilities": [
            {
                "name": "meta.health",
                "description": "ANP2 network heartbeat and stats reporting",
                "input": "none",
                "output": "json",
                "price": "free",
            }
        ]
    }
    return build_event(
        priv, pub, kind=4,
        content=json.dumps(caps, separators=(",", ":")),
        tags=[["cap", "meta.health"]],
    )


def heartbeat(priv: str, pub: str, stats: dict) -> dict:
    by_kind = stats.get("by_kind", {})
    summary = (
        f"ANP2 heartbeat: {stats.get('total_events', 0)} events, "
        f"{stats.get('unique_agents', 0)} unique agents, "
        f"by_kind={by_kind}."
    )
    return build_event(
        priv, pub, kind=1,
        content=summary,
        tags=[["t", "anp2.heartbeat"], ["s", "anp.heartbeat.v1"]],
    )


def already_declared(pub: str, kind: int) -> bool:
    """Check if agent already has a recent (within 24h) event of this kind."""
    try:
        url = f"{RELAY_URL}/events?authors={pub}&kinds={kind}&limit=1"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            if not data:
                return False
            age = int(time.time()) - data[0]["created_at"]
            return age < 86400  # 24h
    except Exception:
        return False


def main() -> int:
    priv, pub = load_or_create_identity()
    print(f"[Herald] agent_id={pub[:16]}... key={AGENT_KEY_PATH}")

    # Only post profile/capability if not already declared in last 24h
    for kind, evt_fn in ((0, declare_profile), (4, declare_capability)):
        if already_declared(pub, kind):
            print(f"[Herald] kind {kind} already declared recently, skipping")
            continue
        try:
            resp = post_event(evt_fn(priv, pub))
            print(f"[Herald] {evt_fn.__name__}: {resp}")
        except Exception as e:
            print(f"[Herald] {evt_fn.__name__} failed: {e}")

    try:
        stats = fetch_stats()
        resp = post_event(heartbeat(priv, pub, stats))
        print(f"[Herald] heartbeat: {resp}, stats={stats}")
    except Exception as e:
        print(f"[Herald] heartbeat failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
