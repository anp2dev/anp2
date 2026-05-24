"""Tests for Iter 27 — mandatory PoW on kind-0 and kind-50.

PIP-002 introduced opt-in PoW for kind-6 trust votes. Iter 27 extends
mandatory PoW to the kinds in PIP_002_MANDATORY_KINDS (kind-0 identity +
kind-50 task.request), so creating a network identity or delegating work
costs non-zero CPU — bounding cheap-Sybil farms.

These tests verify the relay's `validate_event_pow(mandatory=True)`
path: events of mandatory kinds without a `pow` tag (or with insufficient
bits) are rejected with HTTP 400; events with valid PoW are accepted;
events of non-mandatory kinds pass through unchanged.
"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from anp2_relay.crypto import compute_event_id, generate_keypair, sign_event_id
from anp2_relay.pow import (
    PIP_002_MANDATORY_KINDS,
    PIP_002_MIN_BITS,
    mint_pow,
)
from anp2_relay.server import create_app
from anp2_relay.storage import Storage


# ---------- helpers ------------------------------------------------------


def _build(priv: str, pub: str, *, kind: int, content: str = "",
           tags: list[list[str]] | None = None, mine_pow: bool = False) -> dict:
    """Build a signed event. If `mine_pow=True`, mine the PoW tag pair
    before computing the canonical id; otherwise the event has no PoW."""
    ts = int(time.time())
    tags = list(tags or [])
    if mine_pow:
        payload = {"agent_id": pub, "created_at": ts, "kind": kind,
                   "tags": tags, "content": content}
        mint_pow(payload, PIP_002_MIN_BITS)
        tags = payload["tags"]
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


# ---------- the mandatory-kind set is the agreed one ---------------------


def test_mandatory_kinds_are_zero_and_fifty():
    """Iter 27 starts mandatory PoW with identity (kind-0) and
    task.request (kind-50). Other kinds remain opt-in."""
    assert 0 in PIP_002_MANDATORY_KINDS
    assert 50 in PIP_002_MANDATORY_KINDS
    assert 1 not in PIP_002_MANDATORY_KINDS    # kind-1 status posts
    assert 4 not in PIP_002_MANDATORY_KINDS    # kind-4 capability declarations
    assert 6 not in PIP_002_MANDATORY_KINDS    # kind-6 trust votes stay opt-in


# ---------- kind-0 identity events ---------------------------------------


def test_kind0_without_pow_rejected_400(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    ev = _build(priv, pub, kind=0,
                content=json.dumps({"name": "no-pow-bot"}), mine_pow=False)
    r = c.post("/events", json=ev)
    assert r.status_code == 400, r.text
    assert "PoW" in r.text or "pow" in r.text


def test_kind0_with_valid_pow_accepted_200(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    ev = _build(priv, pub, kind=0,
                content=json.dumps({"name": "mined-bot"}), mine_pow=True)
    r = c.post("/events", json=ev)
    assert r.status_code == 200, r.text


# ---------- kind-50 task.request events ---------------------------------


def test_kind50_without_pow_rejected_400(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    body = json.dumps({"capability": "transform.text.demo",
                       "input": {"text": "bonjour"},
                       "reward": {"currency": "credit", "amount": 1,
                                  "payment_method": "anp2_credit"}})
    ev = _build(priv, pub, kind=50, content=body, mine_pow=False)
    r = c.post("/events", json=ev)
    assert r.status_code == 400, r.text


def test_kind50_with_valid_pow_accepted_200(tmp_path):
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    body = json.dumps({"capability": "transform.text.demo",
                       "input": {"text": "bonjour"},
                       "reward": {"currency": "credit", "amount": 1,
                                  "payment_method": "anp2_credit"}})
    ev = _build(priv, pub, kind=50, content=body,
                tags=[["cap_wanted", "transform.text.demo"]], mine_pow=True)
    r = c.post("/events", json=ev)
    assert r.status_code == 200, r.text


# ---------- non-mandatory kinds keep current behaviour ------------------


def test_kind1_without_pow_still_accepted_200(tmp_path):
    """Status posts (kind-1) are not in the mandatory set — no PoW needed."""
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    ev = _build(priv, pub, kind=1, content="hello", mine_pow=False)
    r = c.post("/events", json=ev)
    assert r.status_code == 200, r.text


def test_kind4_without_pow_still_accepted_200(tmp_path):
    """Capability declarations (kind-4) are not in the mandatory set."""
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    body = json.dumps({"capabilities": [{"name": "test.cap"}]})
    ev = _build(priv, pub, kind=4, content=body,
                tags=[["cap", "test.cap"]], mine_pow=False)
    r = c.post("/events", json=ev)
    assert r.status_code == 200, r.text


# ---------- forgery resistance ------------------------------------------


def test_kind0_with_pow_tag_but_wrong_bits_rejected(tmp_path):
    """A pow tag claiming N bits but where the canonical id has fewer
    actual leading zeros is rejected (lying about PoW is a 400)."""
    c = _client(tmp_path)
    priv, pub = generate_keypair()
    ts = int(time.time())
    # Tags with a pow tag that LIES — declared 12 but the unmined id
    # has effectively 0 leading zero bits.
    tags = [["pow", "12"], ["nonce", "0"]]
    content = json.dumps({"name": "liar"})
    eid = compute_event_id(pub, ts, 0, tags, content)
    sig = sign_event_id(eid, priv)
    ev = {"id": eid, "agent_id": pub, "created_at": ts, "kind": 0,
          "tags": tags, "content": content, "sig": sig}
    r = c.post("/events", json=ev)
    assert r.status_code == 400, r.text
