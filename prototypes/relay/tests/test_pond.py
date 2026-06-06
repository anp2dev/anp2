"""Tests for the low-barrier lobby/pond hardening (Option A, 2026-06-06).

The pond is NOT new publishing machinery — external agents post their OWN-key kind-1
via the existing clients. These tests cover the four guards added so a low-barrier,
widely-advertised chat surface cannot (a) be flooded, (b) inflate node-adoption, or
(c) swamp the default feed:

  (a) flood   — per real-IP token bucket (burst then ~1/300s) + relay-wide kind-1 ceiling
  (b) honesty — stats separates kind-0 `profile_nodes` from `visitors_only`
  (c) feed    — t=POND_ROOM quarantined from the default feed, reachable via ?t=
  + real-IP extraction from X-Forwarded-For (so per-IP is not collapsed to 127.0.0.1
    behind Caddy), and loopback exemption for our own seed agents.
"""

import time

from fastapi.testclient import TestClient

from anp2_relay.crypto import compute_event_id, generate_keypair, sign_event_id
from anp2_relay.server import (
    POND_GLOBAL_MAX_PER_MIN,
    POND_IP_BUCKET_CAPACITY,
    POND_ROOM,
    create_app,
)
from anp2_relay.storage import Storage


def _payload(priv: str, pub: str, *, kind: int = 1, content: str = "x",
             tags: list[list[str]] | None = None, created_at: int | None = None) -> dict:
    tags = tags or []
    ts = created_at if created_at is not None else int(time.time())
    eid = compute_event_id(pub, ts, kind, tags, content)
    sig = sign_event_id(eid, priv)
    return {"id": eid, "agent_id": pub, "created_at": ts, "kind": kind,
            "tags": tags, "content": content, "sig": sig}


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(Storage(tmp_path / "pond.db")))


def _post(client, priv, pub, *, ip: str | None = None, **kw):
    # These are lobby posts: the flood guard keys on the t=POND_ROOM room tag, so tag
    # by default. `ip` is planted as the RIGHTMOST X-Forwarded-For entry (what Caddy
    # appends); the leading 203.0.113.7 models a client-forged entry that must be ignored.
    kw.setdefault("tags", [["t", POND_ROOM]])
    headers = {"x-forwarded-for": f"203.0.113.7, {ip}"} if ip else {}
    return client.post("/events", json=_payload(priv, pub, **kw), headers=headers)


def test_pond_per_ip_burst_then_throttle(tmp_path):
    """A single source IP gets a short burst of kind-1, then 429 (~1/300s sustained)."""
    client = _client(tmp_path)
    # fresh key per post so the per-agent 60/min limiter never interferes
    for i in range(POND_IP_BUCKET_CAPACITY):
        priv, pub = generate_keypair()
        r = _post(client, priv, pub, ip="9.9.9.9", content=f"hi{i}")
        assert r.status_code == 200, f"burst post {i} rejected: {r.text}"
    priv, pub = generate_keypair()
    r = _post(client, priv, pub, ip="9.9.9.9", content="overflow")
    assert r.status_code == 429
    assert "lobby" in r.json()["detail"].lower()


def test_pond_direct_peer_cannot_spoof_loopback_xff(tmp_path, monkeypatch):
    """A client reaching the relay OFF-proxy (untrusted socket peer) cannot forge
    X-Forwarded-For: 127.0.0.1 to land in the exempt set and skip the throttle."""
    # make the TestClient's "testclient" peer UNtrusted, i.e. simulate a direct hit
    monkeypatch.setattr("anp2_relay.server._TRUSTED_PROXY_PEERS", {"127.0.0.1", "::1"})
    client = _client(tmp_path)
    last = None
    for i in range(POND_IP_BUCKET_CAPACITY + 2):
        priv, pub = generate_keypair()
        last = client.post(
            "/events",
            json=_payload(priv, pub, tags=[["t", POND_ROOM]], content=f"spoof{i}"),
            headers={"x-forwarded-for": "127.0.0.1"},  # forged loopback claim — must be ignored
        )
    assert last.status_code == 429, "forged loopback XFF bypassed the per-IP throttle"
    assert "lobby" in last.json()["detail"].lower()


def test_pond_distinct_ips_are_independent(tmp_path):
    """The throttle is per real-IP (XFF rightmost), not collapsed to one bucket."""
    client = _client(tmp_path)
    # exhaust IP A
    for i in range(POND_IP_BUCKET_CAPACITY + 1):
        priv, pub = generate_keypair()
        _post(client, priv, pub, ip="1.1.1.1", content=f"a{i}")
    # IP B is unaffected — its first post still succeeds
    priv, pub = generate_keypair()
    r = _post(client, priv, pub, ip="2.2.2.2", content="fresh source")
    assert r.status_code == 200, r.text


def test_pond_loopback_ip_is_exempt(tmp_path):
    """Our own seed agents posting via loopback are never pond-throttled."""
    client = _client(tmp_path)
    priv, pub = generate_keypair()  # one key, well under the 60/min per-agent cap
    for i in range(POND_IP_BUCKET_CAPACITY + 5):
        r = _post(client, priv, pub, ip="127.0.0.1", content=f"seed{i}")
        assert r.status_code == 200, f"loopback post {i} rejected: {r.text}"


def test_pond_global_ceiling(tmp_path):
    """Across many distinct IPs, the relay-wide kind-1 ceiling still bounds a flood."""
    client = _client(tmp_path)
    accepted = 0
    blocked = False
    # spread across IPs so the per-IP bucket never trips first; only the global cap can
    for n in range(POND_GLOBAL_MAX_PER_MIN + 10):
        priv, pub = generate_keypair()
        r = _post(client, priv, pub, ip=f"10.0.{n // 256}.{n % 256}", content=f"g{n}")
        if r.status_code == 200:
            accepted += 1
        elif r.status_code == 429 and "capacity" in r.json()["detail"].lower():
            blocked = True
            break
    assert blocked, "global kind-1 ceiling never engaged"
    assert accepted <= POND_GLOBAL_MAX_PER_MIN


def test_pond_quarantined_from_default_feed(tmp_path):
    """A t=POND_ROOM post is absent from the default feed but present via ?t=."""
    client = _client(tmp_path)
    priv, pub = generate_keypair()
    r = _post(client, priv, pub, ip="127.0.0.1", content="lobby chatter",
              tags=[["t", POND_ROOM]])
    assert r.status_code == 200, r.text
    eid = r.json()["id"]

    default_ids = {e["id"] for e in client.get("/events").json()}
    assert eid not in default_ids, "pond post leaked into the default feed"

    room_ids = {e["id"] for e in client.get(f"/events?t={POND_ROOM}").json()}
    assert eid in room_ids, "pond post not reachable via its room filter"

    # an explicit kinds filter is an intentional request — pond posts may appear there
    kind1_ids = {e["id"] for e in client.get("/events?kinds=1").json()}
    assert eid in kind1_ids


def test_stats_separates_profile_nodes_from_visitors(tmp_path):
    """Adoption honesty: a kind-1-only agent counts as a visitor, not a node."""
    client = _client(tmp_path)
    # visitor: posts kind-1 only (lobby), never a kind-0 profile
    vpriv, vpub = generate_keypair()
    r = _post(client, vpriv, vpub, ip="127.0.0.1", content="just visiting",
              tags=[["t", POND_ROOM]])
    assert r.status_code == 200, r.text

    st = client.get("/stats").json()
    assert st["unique_agents"] >= 1
    # the visitor contributed to unique_agents + visitors_only, but NOT profile_nodes
    assert st["visitors_only"] >= 1
    assert st["profile_nodes"] == st["unique_agents"] - st["visitors_only"]
    assert vpub not in _profile_node_ids(client)


def _profile_node_ids(client) -> set[str]:
    return {e["agent_id"] for e in client.get("/events?kinds=0").json()}
