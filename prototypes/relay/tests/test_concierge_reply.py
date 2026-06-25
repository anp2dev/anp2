"""Tests for the authenticated concierge write tool (anp2_concierge_reply).

The hosted MCP write tool lets a Claude Code Routine (running on Anthropic's
cloud, on the subscription) post lobby replies as the concierge without holding
the signing key. The relay signs server-side (kind-1 needs no PoW), gates on a
bearer token, runs a literal leak-guard backstop, and is idempotent per inbound
event.
"""
import importlib
import json

import pytest

from anp2_relay.crypto import generate_keypair, verify_signature
from anp2_relay.storage import Storage

TOKEN = "test-concierge-token"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    priv, pub = generate_keypair()
    monkeypatch.setenv("ANP2_CONCIERGE_PRIV", priv)
    monkeypatch.setenv("ANP2_CONCIERGE_MCP_TOKEN", TOKEN)
    import anp2_relay.server as S
    importlib.reload(S)  # re-read the concierge env at module import
    from fastapi.testclient import TestClient
    client = TestClient(S.create_app(Storage(tmp_path / "t.db")))
    yield client, pub
    importlib.reload(S)  # restore module state for other tests


def _call(client, args, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "anp2_concierge_reply", "arguments": args}},
        headers=headers,
    )


def _text(resp):
    return resp.json().get("result", {}).get("content", [{}])[0].get("text", "")


E, S_ID = "a" * 64, "b" * 64


def test_requires_bearer(ctx):
    client, _ = ctx
    assert "error" in _call(client, {"event_id": E, "sender": S_ID, "reply_text": "hi"}).json()


def test_rejects_wrong_bearer(ctx):
    client, _ = ctx
    assert "error" in _call(client, {"event_id": E, "sender": S_ID, "reply_text": "hi"}, "nope").json()


def test_posts_signed_reply(ctx):
    client, pub = ctx
    r = _call(client, {"event_id": E, "sender": S_ID, "reply_text": "Welcome to ANP2."}, TOKEN)
    assert json.loads(_text(r))["status"] == "posted"
    # queryable + signature valid + correct tags
    q = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "anp2_query", "arguments": {"kind": 1, "author": pub}}})
    ev = json.loads(q.json()["result"]["content"][0]["text"])["events"][0]
    assert verify_signature(ev["id"], ev["sig"], ev["agent_id"])
    assert ["e", E] in ev["tags"] and ["p", S_ID] in ev["tags"] and ["t", "lobby"] in ev["tags"]


def test_idempotent(ctx):
    client, _ = ctx
    first = json.loads(_text(_call(client, {"event_id": E, "sender": S_ID, "reply_text": "one"}, TOKEN)))
    second = json.loads(_text(_call(client, {"event_id": E, "sender": S_ID, "reply_text": "two"}, TOKEN)))
    assert second["status"] == "already_replied" and second["id"] == first["id"]


def test_leak_guard_too_long(ctx):
    client, _ = ctx
    assert "error" in _call(client, {"event_id": "c" * 64, "sender": S_ID, "reply_text": "x" * 600}, TOKEN).json()


def test_leak_guard_blocks_cjk(ctx):
    client, _ = ctx
    assert "error" in _call(client, {"event_id": "d" * 64, "sender": S_ID, "reply_text": "hi こん"}, TOKEN).json()


def test_rejects_bad_ids(ctx):
    client, _ = ctx
    assert "error" in _call(client, {"event_id": "short", "sender": S_ID, "reply_text": "hi"}, TOKEN).json()
