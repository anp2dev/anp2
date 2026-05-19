"""Smoke tests for crypto/event/storage/api."""

import time

from fastapi.testclient import TestClient

from anp2_relay.crypto import (
    compute_event_id,
    generate_keypair,
    sign_event_id,
    verify_signature,
)
from anp2_relay.events import Event
from anp2_relay.server import create_app
from anp2_relay.storage import Storage


def _make_payload(priv: str, pub: str, *, kind: int = 1, content: str = "hi") -> dict:
    ts = int(time.time())
    tags: list[list[str]] = [["t", "test"]]
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


def test_keypair_and_signature_round_trip():
    priv, pub = generate_keypair()
    assert len(priv) == 64 and len(pub) == 64
    eid = compute_event_id(pub, 1000, 1, [], "x")
    sig = sign_event_id(eid, priv)
    assert verify_signature(eid, sig, pub)


def test_event_validation_rejects_tampering():
    priv, pub = generate_keypair()
    payload = _make_payload(priv, pub, content="original")
    ev = Event(**payload)
    ok, err = ev.is_valid()
    assert ok, err

    tampered = ev.model_copy(update={"content": "tampered"})
    ok, err = tampered.is_valid()
    assert not ok
    assert "id mismatch" in (err or "")


def test_publish_and_fetch(tmp_path):
    storage = Storage(tmp_path / "test.db")
    client = TestClient(create_app(storage))

    priv, pub = generate_keypair()
    payload = _make_payload(priv, pub, content="hello anp2")

    r = client.post("/events", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"]

    r = client.get("/events", params={"kinds": "1", "t": "test"})
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 1
    assert events[0]["content"] == "hello anp2"

    r = client.get("/health")
    assert r.json()["events"] == 1


def test_fetch_one_event_by_id(tmp_path):
    storage = Storage(tmp_path / "test.db")
    client = TestClient(create_app(storage))

    priv, pub = generate_keypair()
    payload = _make_payload(priv, pub, content="single fetch")
    r = client.post("/events", json=payload)
    assert r.status_code == 200, r.text
    eid = payload["id"]

    r = client.get(f"/events/{eid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == eid
    assert body["content"] == "single fetch"

    # Unknown id -> 404
    r = client.get("/events/" + ("0" * 64))
    assert r.status_code == 404

    # Wrong length -> 400
    r = client.get("/events/abc")
    assert r.status_code == 400


def test_publish_rejects_bad_signature(tmp_path):
    storage = Storage(tmp_path / "test.db")
    client = TestClient(create_app(storage))
    priv, pub = generate_keypair()
    payload = _make_payload(priv, pub)
    payload["sig"] = "0" * 128
    r = client.post("/events", json=payload)
    assert r.status_code == 400
