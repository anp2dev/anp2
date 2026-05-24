"""Tests for spam mitigation: rate limit, content size, tag caps, time skew.

Mirrors the Phase 0/1 quick wins defined in server.py (PROTOCOL §8).
"""

import time

from fastapi.testclient import TestClient

from anp2_relay.crypto import (
    compute_event_id,
    generate_keypair,
    sign_event_id,
)
from anp2_relay.server import (
    MAX_CONTENT_BYTES,
    MAX_TAG_VALUE_BYTES,
    MAX_TAGS,
    MAX_TIME_SKEW_FUTURE_SEC,
    MAX_TIME_SKEW_PAST_SEC,
    RATE_LIMIT_MAX_EVENTS,
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
    return TestClient(create_app(Storage(tmp_path / "spam.db")))


def test_rate_limit_blocks_after_threshold(tmp_path):
    """N+1th event in a 60s window from one agent_id — HTTP 429."""
    client = _client(tmp_path)
    priv, pub = generate_keypair()
    # First N events should succeed
    for i in range(RATE_LIMIT_MAX_EVENTS):
        r = client.post("/events", json=_payload(priv, pub, content=f"msg{i}"))
        assert r.status_code == 200, f"event {i} unexpectedly rejected: {r.text}"
    # The N+1th must be rate-limited
    r = client.post("/events", json=_payload(priv, pub, content="overflow"))
    assert r.status_code == 429
    assert "rate limit" in r.json()["detail"].lower()


def test_content_size_cap(tmp_path):
    """content exceeding MAX_CONTENT_BYTES — HTTP 400."""
    client = _client(tmp_path)
    priv, pub = generate_keypair()
    huge = "a" * (MAX_CONTENT_BYTES + 1)
    r = client.post("/events", json=_payload(priv, pub, content=huge))
    assert r.status_code == 400
    assert "content exceeds" in r.json()["detail"]


def test_tag_count_cap(tmp_path):
    """more than MAX_TAGS tags — HTTP 400."""
    client = _client(tmp_path)
    priv, pub = generate_keypair()
    tags = [["t", f"r{i}"] for i in range(MAX_TAGS + 1)]
    r = client.post("/events", json=_payload(priv, pub, tags=tags))
    assert r.status_code == 400
    assert "too many tags" in r.json()["detail"]


def test_tag_value_size_cap(tmp_path):
    """single tag value over the per-value cap — HTTP 400."""
    client = _client(tmp_path)
    priv, pub = generate_keypair()
    tags = [["t", "x" * (MAX_TAG_VALUE_BYTES + 1)]]
    r = client.post("/events", json=_payload(priv, pub, tags=tags))
    assert r.status_code == 400
    assert "tag value exceeds" in r.json()["detail"]


def test_time_skew_future_rejects(tmp_path):
    """created_at more than MAX_TIME_SKEW_FUTURE_SEC ahead — HTTP 400."""
    client = _client(tmp_path)
    priv, pub = generate_keypair()
    future_ts = int(time.time()) + MAX_TIME_SKEW_FUTURE_SEC + 60
    r = client.post("/events", json=_payload(priv, pub, created_at=future_ts))
    assert r.status_code == 400
    assert "future" in r.json()["detail"]


def test_time_skew_past_rejects(tmp_path):
    """created_at older than MAX_TIME_SKEW_PAST_SEC — HTTP 400."""
    client = _client(tmp_path)
    priv, pub = generate_keypair()
    past_ts = int(time.time()) - MAX_TIME_SKEW_PAST_SEC - 60
    r = client.post("/events", json=_payload(priv, pub, created_at=past_ts))
    assert r.status_code == 400
    assert "past" in r.json()["detail"]


def test_normal_event_within_skew_accepted(tmp_path):
    """Sanity: an event with created_at slightly in the past (5 sec) is fine."""
    client = _client(tmp_path)
    priv, pub = generate_keypair()
    r = client.post(
        "/events",
        json=_payload(priv, pub, created_at=int(time.time()) - 5),
    )
    assert r.status_code == 200, r.text
