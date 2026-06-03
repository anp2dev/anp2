"""Regression tests for query-surface fixes (desk review MED/LOW, 2026-06-03):
- capabilities/search accepts `q` as an alias for `cap` (a tool guessing the
  conventional `q=` param used to get a silent unfiltered dump).
- GET /events accepts a `p` tag filter (the spec documented `?p=<id>` for kind-3
  DM recipients, but the param did not exist and was silently ignored).
"""

import json
import time

from fastapi.testclient import TestClient

from anp2_relay.crypto import compute_event_id, generate_keypair, sign_event_id
from anp2_relay.server import create_app
from anp2_relay.storage import Storage


def _ev(priv, pub, kind, content, tags):
    ts = int(time.time())
    eid = compute_event_id(pub, ts, kind, tags, content)
    return {
        "id": eid, "agent_id": pub, "created_at": ts, "kind": kind,
        "tags": tags, "content": content, "sig": sign_event_id(eid, priv),
    }


def test_capabilities_search_q_is_alias_for_cap(tmp_path):
    client = TestClient(create_app(Storage(tmp_path / "t.db")))
    priv, pub = generate_keypair()
    cap = {"capabilities": [{"name": "demo.q.echo", "version": "0.1",
                             "input_modes": ["text/plain"], "output_modes": ["text/plain"]}]}
    client.post("/events", json=_ev(priv, pub, 4, json.dumps(cap), [["cap", "demo.q.echo"]]))

    via_q = client.get("/api/capabilities/search", params={"q": "demo.q.echo"}).json()
    via_cap = client.get("/api/capabilities/search", params={"cap": "demo.q.echo"}).json()
    assert via_q["count"] == via_cap["count"] >= 1
    assert {r["provider_agent_id"] for r in via_q["results"]} == {r["provider_agent_id"] for r in via_cap["results"]}
    # and q actually FILTERS (not a silent full dump): a miss returns nothing
    assert client.get("/api/capabilities/search", params={"q": "no.such.cap.xyz"}).json()["count"] == 0


def test_events_p_tag_filter(tmp_path):
    client = TestClient(create_app(Storage(tmp_path / "t.db")))
    priv, pub = generate_keypair()
    _, target = generate_keypair()
    # a kind-1 addressed to `target` (p tag) and one that is not
    client.post("/events", json=_ev(priv, pub, 1, "hi target", [["p", target]]))
    client.post("/events", json=_ev(priv, pub, 1, "unrelated", [["t", "lobby"]]))

    r = client.get("/events", params={"kinds": "1", "p": target}).json()
    rows = r if isinstance(r, list) else r.get("events", r)
    assert len(rows) == 1  # only the p-tagged event, not the lobby one
    assert rows[0]["content"] == "hi target"
