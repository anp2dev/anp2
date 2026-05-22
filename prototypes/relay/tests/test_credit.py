"""Tests for the ANP2 mutual-credit economy (PROTOCOL (JP-redacted)18.11).

Credit is a relay-derived bilateral-IOU ledger: balance is a pure function of
the event log (kinds 50/52/53/55), never stored. Total credit always sums to
exactly zero (JP-redacted) every passed task debits the requester exactly what it credits
the provider.

Coverage:
  (a) full credit task: kind 50 (anp2_credit, amount N) + 51 + 52 + 53 passed
      -> requester balance -N, provider +N
  (b) a failed task moves zero credit
  (c) an open task locks the requester's amount (available reflects it)
  (d) the credit limit (JP-redacted) a requester at the limit gets 422 on the next kind 50
  (e) GET /agents/<id>/credit returns the right shape

Uses the same TestClient + signed-event pattern as test_task_lifecycle.
"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from anp2_relay.crypto import compute_event_id, generate_keypair, sign_event_id
from anp2_relay.server import create_app
from anp2_relay.storage import Storage


# ---------- low-level helpers --------------------------------------------


def _sign(priv: str, pub: str, kind: int, tags: list[list[str]], content: str, ts: int) -> dict:
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


def _post(client: TestClient, payload: dict) -> None:
    r = client.post("/events", json=payload)
    assert r.status_code == 200, r.text


def _make_credit_request(priv: str, pub: str, *, amount: int,
                         capability: str = "transform.text.demo",
                         deadline_unix: int | None = None,
                         ts: int | None = None) -> dict:
    """A kind 50 task.request priced in ANP2 credit."""
    ts = ts if ts is not None else int(time.time())
    deadline_unix = deadline_unix if deadline_unix is not None else ts + 86400
    body = {
        "capability": capability,
        "input": {"text": "konnichiwa"},
        "constraints": {
            "max_cost_usd": "0.10",
            "deadline_unix": deadline_unix,
            "accept_languages": ["ja", "en"],
            "min_provider_trust": 0.0,
        },
        "reward": {
            "currency": "credit",
            "amount": amount,
            "payment_method": "anp2_credit",
            "escrow_method": "none",
        },
    }
    tags = [["t", capability], ["cap_wanted", capability]]
    return _sign(priv, pub, 50, tags, json.dumps(body, separators=(",", ":")), ts)


def _make_accept(priv: str, pub: str, task_id: str, requester_id: str, *,
                 capability: str = "transform.text.demo", ts: int | None = None) -> dict:
    ts = ts if ts is not None else int(time.time())
    body = {
        "eta_unix": ts + 60,
        "price_quote": {"currency": "credit", "amount": "1"},
        "terms_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    }
    tags = [
        ["e", task_id, "root"],
        ["e", task_id, "accept"],
        ["t", capability],
        ["p", requester_id],
    ]
    return _sign(priv, pub, 51, tags, json.dumps(body, separators=(",", ":")), ts)


def _make_result(priv: str, pub: str, task_id: str, accept_id: str, requester_id: str, *,
                 capability: str = "transform.text.demo", ts: int | None = None) -> dict:
    ts = ts if ts is not None else int(time.time())
    body = {
        "task_id": task_id,
        "output": {"text": "hello"},
        "runtime_ms": 42,
        "output_format": "json",
    }
    tags = [
        ["e", task_id, "root"],
        ["e", task_id, "result"],
        ["e", accept_id, "accept"],
        ["t", capability],
        ["p", requester_id],
    ]
    return _sign(priv, pub, 52, tags, json.dumps(body, separators=(",", ":")), ts)


def _make_verify(priv: str, pub: str, task_id: str, result_id: str, provider_id: str, *,
                 verdict: str = "passed", score: float = 0.9,
                 capability: str = "transform.text.demo", ts: int | None = None) -> dict:
    ts = ts if ts is not None else int(time.time())
    body = {
        "task_id": task_id,
        "verdict": verdict,
        "score": score,
        "reasons": ["test"],
        "evidence_event_ids": [],
    }
    tags = [
        ["e", task_id, "root"],
        ["e", task_id, "verify"],
        ["e", result_id, "result"],
        ["t", capability],
        ["p", provider_id],
    ]
    return _sign(priv, pub, 53, tags, json.dumps(body, separators=(",", ":")), ts)


# ---------- (a) full credit task settles -1/+1 ---------------------------


def test_credit_full_task_settles_debit_and_credit(tmp_path):
    """kind 50 (anp2_credit, amount N) + 51 + 52 + 53 passed
    -> requester balance -N, provider +N."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()

    t0 = int(time.time())
    amount = 250

    req = _make_credit_request(priv_req, pub_req, amount=amount, ts=t0)
    _post(client, req)
    task_id = req["id"]

    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=t0 + 1)
    _post(client, acc)
    res = _make_result(priv_prov, pub_prov, task_id, acc["id"], pub_req, ts=t0 + 2)
    _post(client, res)
    ver = _make_verify(priv_req, pub_req, task_id, res["id"], pub_prov,
                       verdict="passed", ts=t0 + 3)
    _post(client, ver)

    req_credit = storage.credit_for(pub_req)
    prov_credit = storage.credit_for(pub_prov)

    assert req_credit["balance"] == -amount
    assert prov_credit["balance"] == amount
    # settled task -> nothing locked anymore
    assert req_credit["locked"] == 0
    assert req_credit["available"] == -amount
    assert prov_credit["available"] == amount
    # invariant: total credit sums to exactly zero
    assert req_credit["balance"] + prov_credit["balance"] == 0


# ---------- (b) failed task moves zero credit ----------------------------


def test_credit_failed_task_moves_zero(tmp_path):
    """A kind 53 verdict=failed settles the task with no credit movement."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()

    t0 = int(time.time())
    amount = 300

    req = _make_credit_request(priv_req, pub_req, amount=amount, ts=t0)
    _post(client, req)
    task_id = req["id"]

    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=t0 + 1)
    _post(client, acc)
    res = _make_result(priv_prov, pub_prov, task_id, acc["id"], pub_req, ts=t0 + 2)
    _post(client, res)
    ver = _make_verify(priv_req, pub_req, task_id, res["id"], pub_prov,
                       verdict="failed", score=0.1, ts=t0 + 3)
    _post(client, ver)

    req_credit = storage.credit_for(pub_req)
    prov_credit = storage.credit_for(pub_prov)

    # failed -> zero movement, and not locked
    assert req_credit["balance"] == 0
    assert req_credit["locked"] == 0
    assert req_credit["available"] == 0
    assert prov_credit["balance"] == 0


# ---------- (c) open task locks the requester's amount -------------------


def test_credit_open_task_locks_requester_amount(tmp_path):
    """An unsettled (open) task commits the requester's reward into `locked`,
    reducing `available` without yet moving `balance`."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()

    t0 = int(time.time())
    amount = 400

    req = _make_credit_request(priv_req, pub_req, amount=amount, ts=t0)
    _post(client, req)

    req_credit = storage.credit_for(pub_req)
    # open task: balance untouched, amount locked, available pulled down
    assert req_credit["balance"] == 0
    assert req_credit["locked"] == amount
    assert req_credit["available"] == -amount
    assert req_credit["credit_limit"] == Storage.ANP2_BASE_CREDIT_LIMIT


# ---------- (d) credit limit enforcement at publish ----------------------


def test_credit_limit_rejects_overcommit_with_422(tmp_path):
    """A requester whose available credit is at the limit gets HTTP 422 on the
    next anp2_credit kind 50 (JP-redacted) it cannot delegate beyond its means."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    limit = Storage.ANP2_BASE_CREDIT_LIMIT

    t0 = int(time.time())

    # First kind 50 commits exactly the full limit (JP-redacted) allowed (available 0 -> -limit).
    req1 = _make_credit_request(priv_req, pub_req, amount=limit, ts=t0)
    r1 = client.post("/events", json=req1)
    assert r1.status_code == 200, r1.text

    # available is now -limit (open task locks the full limit). Any further
    # anp2_credit commitment pushes available below -credit_limit -> 422.
    req2 = _make_credit_request(priv_req, pub_req, amount=1, ts=t0 + 1)
    r2 = client.post("/events", json=req2)
    assert r2.status_code == 422, r2.text
    detail = r2.json()["detail"]
    assert "insufficient credit" in detail
    assert "(JP-redacted)18.11" in detail

    # a `mocked` task is unaffected by the credit limit
    mocked_body = {
        "capability": "transform.text.demo",
        "input": {"text": "x"},
        "constraints": {
            "max_cost_usd": "0.10",
            "deadline_unix": t0 + 86400,
            "accept_languages": ["en"],
            "min_provider_trust": 0.0,
        },
        "reward": {
            "currency": "USD",
            "amount": "0.05",
            "payment_method": "mocked",
            "escrow_method": "none",
        },
    }
    mocked = _sign(priv_req, pub_req, 50,
                   [["t", "transform.text.demo"], ["cap_wanted", "transform.text.demo"]],
                   json.dumps(mocked_body, separators=(",", ":")), t0 + 2)
    r3 = client.post("/events", json=mocked)
    assert r3.status_code == 200, r3.text


# ---------- (e) GET /agents/<id>/credit shape ----------------------------


def test_credit_endpoint_returns_expected_shape(tmp_path):
    """GET /agents/<id>/credit (and /api/...) returns the (JP-redacted)18.11 shape."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()

    t0 = int(time.time())
    amount = 120

    req = _make_credit_request(priv_req, pub_req, amount=amount, ts=t0)
    _post(client, req)
    task_id = req["id"]
    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=t0 + 1)
    _post(client, acc)
    res = _make_result(priv_prov, pub_prov, task_id, acc["id"], pub_req, ts=t0 + 2)
    _post(client, res)
    ver = _make_verify(priv_req, pub_req, task_id, res["id"], pub_prov,
                       verdict="passed", ts=t0 + 3)
    _post(client, ver)

    for path in (f"/agents/{pub_req}/credit", f"/api/agents/{pub_req}/credit"):
        r = client.get(path)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) == {
            "agent_id", "balance", "locked", "available", "credit_limit",
        }
        assert body["agent_id"] == pub_req
        assert body["balance"] == -amount
        assert body["locked"] == 0
        assert body["available"] == -amount
        assert body["credit_limit"] == Storage.ANP2_BASE_CREDIT_LIMIT

    # provider side
    r = client.get(f"/agents/{pub_prov}/credit")
    assert r.status_code == 200
    assert r.json()["balance"] == amount

    # bad agent_id -> 400
    r = client.get("/agents/not-hex/credit")
    assert r.status_code == 400

    # credit_balance is surfaced on the rich agent view
    view = client.get(f"/agents/{pub_req}").json()
    assert view["credit_balance"] == -amount
