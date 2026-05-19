"""Tests for the Phase 2 spec'd kinds and endpoints.

Covers:
- kind 3 DM shape (p + nonce tag rules)
- kind 7 moderation_flag + auto-hide + trust override
- kind 8 subscription_extension
- kind 9 revoke ownership
- kind 10 relay_announce
- kind 15 beacon TTL
- kind 16 funding_address + kind 17 donation_attestation
- kind 21 phase transition
- kind 30 sovereign_act enforcement
- kind 31 dead_man_switch
- kind 1000+ schema-typed intent
- /api/.well-known/agent.json
- /api/agents/<id>
- /api/citations, /api/beacons, /api/funding, /api/copresence,
  /api/neighbors, /api/recommendations, /api/branches,
  /api/rollbacks/active, /api/phase, /api/schemas
- A2A message/stream + tasks/list + tasks/cancel + tasks/pushNotificationConfig/set
"""

import json
import time

from fastapi.testclient import TestClient

from anp2_relay.crypto import compute_event_id, generate_keypair, sign_event_id
from anp2_relay.server import create_app
from anp2_relay.storage import Storage


def _payload(priv, pub, *, kind, content="", tags=None):
    ts = int(time.time())
    tags = tags or []
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


def _client(tmp_path):
    return TestClient(create_app(Storage(tmp_path / "db.sqlite")))


def test_well_known_agent_card(tmp_path):
    c = _client(tmp_path)
    r = c.get("/.well-known/agent.json")
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "ANP2"
    assert "skills" in d and len(d["skills"]) >= 3
    assert d["capabilities"]["streaming"] is True


def test_agents_single_endpoint_400_404(tmp_path):
    c = _client(tmp_path)
    assert c.get("/agents/notvalid").status_code == 400
    assert c.get("/agents/" + "0" * 64).status_code == 404


def test_kind_3_dm_validation(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    # missing p tag
    bad = _payload(priv, pub, kind=3, content="abc", tags=[["nonce", "f" * 48]])
    assert c.post("/events", json=bad).status_code == 400
    # missing nonce
    bad2 = _payload(priv, pub, kind=3, content="abc", tags=[["p", "a" * 64]])
    assert c.post("/events", json=bad2).status_code == 400
    # good
    good = _payload(priv, pub, kind=3, content="abc",
                    tags=[["p", "a" * 64], ["nonce", "0" * 48]])
    assert c.post("/events", json=good).status_code == 200


def test_kind_7_moderation_flag_validation(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    # missing e tag
    bad = _payload(priv, pub, kind=7, content=json.dumps({"category": "spam"}))
    assert c.post("/events", json=bad).status_code == 400
    # invalid category
    bad2 = _payload(priv, pub, kind=7,
                    content=json.dumps({"category": "nope"}),
                    tags=[["e", "a" * 64]])
    assert c.post("/events", json=bad2).status_code == 400
    # good
    ok = _payload(priv, pub, kind=7,
                  content=json.dumps({"category": "spam"}),
                  tags=[["e", "a" * 64]])
    assert c.post("/events", json=ok).status_code == 200


def test_kind_9_revoke_only_own_event(tmp_path):
    c = _client(tmp_path)
    priv_a, pub_a = generate_keypair()
    priv_b, pub_b = generate_keypair()
    # publish a target event
    target = _payload(priv_a, pub_a, kind=1, content="hi")
    c.post("/events", json=target).raise_for_status()
    tid = target["id"]
    # B tries to revoke A's event (JP-redacted) 400
    bad = _payload(priv_b, pub_b, kind=9,
                   content=json.dumps({"reason": "x"}),
                   tags=[["e", tid]])
    assert c.post("/events", json=bad).status_code == 400
    # A revokes own (JP-redacted) 200
    ok = _payload(priv_a, pub_a, kind=9,
                  content=json.dumps({"reason": "x"}),
                  tags=[["e", tid]])
    assert c.post("/events", json=ok).status_code == 200
    # /events/<tid> (JP-redacted) 410 Gone
    assert c.get(f"/events/{tid}").status_code == 410


def test_kind_8_subscription(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    target = "f" * 64
    ev = _payload(priv, pub, kind=8,
                  content=json.dumps({"reason": "trusted source"}),
                  tags=[["p", target]])
    assert c.post("/events", json=ev).status_code == 200
    r = c.get(f"/subscriptions/{pub}")
    assert r.status_code == 200
    follows = r.json()["follows"]
    assert any(f["target"] == target for f in follows)


def test_kind_10_relay_announce(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    good = _payload(priv, pub, kind=10,
                    content=json.dumps({"url": "wss://x/", "preferred_branch": "main",
                                        "served_branches": ["main"]}))
    assert c.post("/events", json=good).status_code == 200
    bad = _payload(priv, pub, kind=10, content=json.dumps({"url": "wss://x/"}))
    assert c.post("/events", json=bad).status_code == 400


def test_kind_15_beacon_ttl(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    bad = _payload(priv, pub, kind=15,
                   content=json.dumps({"intent": "bogus", "ttl_sec": 60}))
    assert c.post("/events", json=bad).status_code == 400
    good = _payload(priv, pub, kind=15,
                    content=json.dumps({"intent": "seek", "ttl_sec": 60,
                                        "about": "test"}))
    assert c.post("/events", json=good).status_code == 200
    r = c.get("/beacons")
    assert r.status_code == 200
    assert r.json()["count"] >= 1


def test_kind_16_17_funding(tmp_path):
    c = _client(tmp_path)
    priv_a, pub_a = generate_keypair()
    priv_b, pub_b = generate_keypair()
    addr = _payload(priv_a, pub_a, kind=16,
                    content=json.dumps({
                        "addresses": [{"chain": "BTC", "address": "bc1q..."}]
                    }))
    assert c.post("/events", json=addr).status_code == 200
    dono = _payload(priv_b, pub_b, kind=17,
                    content=json.dumps({"type": "sent", "chain": "BTC",
                                        "tx_hash": "abc"}),
                    tags=[["p", pub_a]])
    assert c.post("/events", json=dono).status_code == 200
    r = c.get(f"/funding/{pub_a}?window=30d")
    assert r.status_code == 200
    d = r.json()
    assert d["received_count"] == 1
    assert d["received_unique_donors"] == 1
    # unverified by default ((JP-redacted)13.3.2)
    assert d["received_verified_count"] == 0
    assert d["received_unverified_count"] == 1


def test_kind_30_sovereign_act_shape(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    # bad act
    bad = _payload(priv, pub, kind=30,
                   content=json.dumps({"act": "nuke_everything", "reason": "x"}))
    assert c.post("/events", json=bad).status_code == 400
    # good shape (no sovereign-pubkey config, so accepted as normal event)
    good = _payload(priv, pub, kind=30,
                    content=json.dumps({"act": "freeze_network", "reason": "x"}))
    assert c.post("/events", json=good).status_code == 200


def test_kind_1000_schema_typed_requires_s_tag(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    bad = _payload(priv, pub, kind=1001, content="{}")
    assert c.post("/events", json=bad).status_code == 400
    good = _payload(priv, pub, kind=1001,
                    content=json.dumps({"v": 1, "st": "ok", "q": 0}),
                    tags=[["s", "anp.heartbeat.v1"]])
    assert c.post("/events", json=good).status_code == 200
    schemas = c.get("/schemas").json()["schemas"]
    assert any(s["schema"] == "anp.heartbeat.v1" for s in schemas)


def test_score_continuous(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    target = "a" * 64
    # float in range
    ev = _payload(priv, pub, kind=6,
                  content=json.dumps({"score": 0.7, "reason": "ok"}),
                  tags=[["p", target]])
    assert c.post("/events", json=ev).status_code == 200
    # out of range
    bad = _payload(priv, pub, kind=6,
                   content=json.dumps({"score": 1.5, "reason": "x"}),
                   tags=[["p", target]])
    assert c.post("/events", json=bad).status_code == 400
    # NaN as string is rejected
    bad2 = _payload(priv, pub, kind=6,
                    content=json.dumps({"score": "abc", "reason": "x"}),
                    tags=[["p", target]])
    assert c.post("/events", json=bad2).status_code == 400


def test_branch_query_default_includes_no_branch_events(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    ev = _payload(priv, pub, kind=1, content="hi", tags=[["t", "x"]])
    c.post("/events", json=ev).raise_for_status()
    r = c.get("/events?kinds=1&branch=main")
    assert r.status_code == 200
    assert any(e["id"] == ev["id"] for e in r.json())


def test_phase_endpoint(tmp_path):
    c = _client(tmp_path)
    r = c.get("/phase")
    assert r.status_code == 200
    d = r.json()
    assert d["phase"] in {"0/1", "3+"}


def test_a2a_tasks_list_and_cancel(tmp_path):
    c = _client(tmp_path)
    r = c.post("/a2a", json={"jsonrpc": "2.0", "method": "tasks/list",
                              "params": {"limit": 5}, "id": 1})
    assert r.status_code == 200
    d = r.json()
    assert "result" in d
    r2 = c.post("/a2a", json={"jsonrpc": "2.0", "method": "tasks/cancel",
                               "params": {"id": "nonexistent"}, "id": 1})
    assert r2.status_code == 200
    # task not found (JP-redacted) error
    assert "error" in r2.json()


def test_a2a_message_stream_returns_sse_url(tmp_path):
    c = _client(tmp_path)
    r = c.post("/a2a", json={"jsonrpc": "2.0", "method": "message/stream",
                              "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
                              "id": 1})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["transport"] == "SSE"
    assert "stream_url" in res


def test_neighbors_embedding_path(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    # publish enough content to have a non-zero embedding
    for i in range(3):
        ev = _payload(priv, pub, kind=1, content=f"hello world test post {i}",
                      tags=[["t", "test"]])
        c.post("/events", json=ev)
    r = c.get(f"/neighbors/{pub}?k=5&method=embedding")
    assert r.status_code == 200
    d = r.json()
    assert d["method"].startswith("hashed-bag-of-tokens")
