"""Tests for the Task Lifecycle protocol (kinds 50-55, PROTOCOL §18).

Coverage:
  - Happy path: request — accept — result — verify(passed) — payment(release)
  - Timeout path: request with deadline in the past — status `timed_out`
  - Verify-failed path: verify with verdict=failed — status `verified`
                       — refund payment — status `refunded`
  - Multi-verifier consensus: 3 verifiers, 2 passed + 1 failed — `verified` (passed wins)
  - Multi-verifier conflict: 2 passed + 2 failed — `disputed`
  - Cancellation: request — cancel (before accept) — `cancelled`
  - Cancellation rejected: cancel after accept is recorded but ignored

Uses the existing TestClient + signed-event pattern from test_basic / test_trust.
"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from anp2_relay.crypto import compute_event_id, generate_keypair, sign_event_id
from anp2_relay.pow import PIP_002_MANDATORY_KINDS, PIP_002_MIN_BITS, mint_pow
from anp2_relay.server import create_app
from anp2_relay.storage import Storage


# ---------- low-level helpers --------------------------------------------


def _sign(priv: str, pub: str, kind: int, tags: list[list[str]], content: str, ts: int) -> dict:
    # Iter 27: PIP-002 mandatory PoW for kinds in PIP_002_MANDATORY_KINDS
    # (kind-0 + kind-50). Mint a nonce so the canonical event id has
    # PIP_002_MIN_BITS leading zero bits, otherwise the relay rejects with
    # HTTP 400. Non-mandatory kinds pass through unchanged.
    tags = list(tags)
    if kind in PIP_002_MANDATORY_KINDS:
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


def _post(client: TestClient, payload: dict) -> None:
    r = client.post("/events", json=payload)
    assert r.status_code == 200, r.text


def _make_request(priv: str, pub: str, *, capability: str = "transform.text.demo",
                  reward_amount: str = "0.05", deadline_unix: int | None = None,
                  ts: int | None = None) -> dict:
    ts = ts if ts is not None else int(time.time())
    deadline_unix = deadline_unix if deadline_unix is not None else ts + 86400
    body = {
        "capability": capability,
        "input": {"text": "bonjour"},
        "constraints": {
            "max_cost_usd": "0.10",
            "deadline_unix": deadline_unix,
            "accept_languages": ["fr", "en"],
            "min_provider_trust": 0.0,
        },
        "reward": {
            "currency": "USD",
            "amount": reward_amount,
            "payment_method": "mocked",
            "escrow_method": "none",
        },
    }
    tags = [["t", capability], ["cap_wanted", capability]]
    return _sign(priv, pub, 50, tags, json.dumps(body, separators=(",", ":")), ts)


def _make_accept(priv: str, pub: str, task_id: str, requester_id: str, *,
                 capability: str = "transform.text.demo", ts: int | None = None,
                 amount: str = "0.04") -> dict:
    ts = ts if ts is not None else int(time.time())
    body = {
        "eta_unix": ts + 60,
        "price_quote": {"currency": "USD", "amount": amount},
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


def _make_payment(priv: str, pub: str, task_id: str, verify_id: str, provider_id: str, *,
                  disposition: str = "release", amount: str = "0.04",
                  capability: str = "transform.text.demo", ts: int | None = None) -> dict:
    ts = ts if ts is not None else int(time.time())
    body = {
        "task_id": task_id,
        "disposition": disposition,
        "payment_proof_url": "https://example.test/proof",
        "amount": amount,
        "currency": "USD",
        "payment_method": "mocked",
        "tx_hash": f"mocked-{task_id[:8]}",
    }
    tags = [
        ["e", task_id, "root"],
        ["e", task_id, "payment"],
        ["e", verify_id, "verify"],
        ["t", capability],
        ["p", provider_id],
    ]
    return _sign(priv, pub, 54, tags, json.dumps(body, separators=(",", ":")), ts)


def _make_cancel(priv: str, pub: str, task_id: str, *,
                 capability: str = "transform.text.demo", reason: str = "no longer needed",
                 ts: int | None = None) -> dict:
    ts = ts if ts is not None else int(time.time())
    body = {"task_id": task_id, "reason": reason}
    tags = [
        ["e", task_id, "root"],
        ["e", task_id, "cancel"],
        ["t", capability],
    ]
    return _sign(priv, pub, 55, tags, json.dumps(body, separators=(",", ":")), ts)


# ---------- happy path ----------------------------------------------------


def test_happy_path_request_accept_result_verify_payment(tmp_path):
    """Full lifecycle ending in `paid`."""
    storage = Storage(tmp_path / "tasks.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()

    t0 = int(time.time())
    req = _make_request(priv_req, pub_req, ts=t0)
    _post(client, req)
    task_id = req["id"]

    # status after request alone = pending
    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "pending"
    assert r["request"]["id"] == task_id

    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=t0 + 1)
    _post(client, acc)
    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "accepted"
    assert r["winning_accept_id"] == acc["id"]

    res = _make_result(priv_prov, pub_prov, task_id, acc["id"], pub_req, ts=t0 + 2)
    _post(client, res)
    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "completed"
    assert len(r["results"]) == 1

    priv_ver, pub_ver = generate_keypair()   # neutral verifier (—18.6)
    ver = _make_verify(priv_ver, pub_ver, task_id, res["id"], pub_prov, verdict="passed", ts=t0 + 3)
    _post(client, ver)
    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "verified"
    assert r["consensus"]["verdict"] == "passed"

    pay = _make_payment(priv_req, pub_req, task_id, ver["id"], pub_prov,
                        disposition="release", ts=t0 + 4)
    _post(client, pay)
    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "paid"
    # Full chronological thread has 5 events (request, accept, result, verify, payment)
    assert len(r["events"]) == 5


# ---------- timeout -------------------------------------------------------


def test_timeout_when_deadline_passes_with_no_result(tmp_path):
    """request with a deadline in the past + no result — status = timed_out."""
    storage = Storage(tmp_path / "tasks.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()

    now = int(time.time())
    # deadline 60s in the past, but created_at must satisfy the 7-day skew
    # rule; choose ts = now - 600 with deadline = now - 60
    req = _make_request(priv_req, pub_req, deadline_unix=now - 60, ts=now - 600)
    _post(client, req)
    task_id = req["id"]

    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "timed_out"

    # even after an accept (but still no result) we remain timed_out
    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=now - 500)
    _post(client, acc)
    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "timed_out"


# ---------- verify failed + refund ---------------------------------------


def test_verify_failed_path_with_refund(tmp_path):
    storage = Storage(tmp_path / "tasks.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()

    t0 = int(time.time())
    req = _make_request(priv_req, pub_req, ts=t0)
    _post(client, req)
    task_id = req["id"]

    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=t0 + 1)
    _post(client, acc)
    res = _make_result(priv_prov, pub_prov, task_id, acc["id"], pub_req, ts=t0 + 2)
    _post(client, res)

    priv_ver, pub_ver = generate_keypair()   # neutral verifier (—18.6)
    ver = _make_verify(priv_ver, pub_ver, task_id, res["id"], pub_prov,
                       verdict="failed", score=0.1, ts=t0 + 3)
    _post(client, ver)
    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "verified"
    assert r["consensus"]["verdict"] == "failed"

    # requester issues a refund payment
    refund = _make_payment(priv_req, pub_req, task_id, ver["id"], pub_prov,
                           disposition="refund", ts=t0 + 4)
    _post(client, refund)
    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "refunded"


# ---------- multi-verifier consensus -------------------------------------


def test_multi_verifier_majority_consensus_passed(tmp_path):
    """3 verifiers: 2 passed, 1 failed — consensus.verdict = passed — status verified."""
    storage = Storage(tmp_path / "tasks.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()
    verifiers = [generate_keypair() for _ in range(3)]

    t0 = int(time.time())
    req = _make_request(priv_req, pub_req, ts=t0)
    _post(client, req)
    task_id = req["id"]
    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=t0 + 1)
    _post(client, acc)
    res = _make_result(priv_prov, pub_prov, task_id, acc["id"], pub_req, ts=t0 + 2)
    _post(client, res)

    verdicts = ["passed", "passed", "failed"]
    scores = [0.95, 0.88, 0.20]
    for i, ((priv_v, pub_v), verdict, score) in enumerate(zip(verifiers, verdicts, scores)):
        v = _make_verify(priv_v, pub_v, task_id, res["id"], pub_prov,
                         verdict=verdict, score=score, ts=t0 + 3 + i)
        _post(client, v)

    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "verified"
    assert r["consensus"]["verdict"] == "passed"
    assert r["consensus"]["verifier_count"] == 3
    # avg score across all 3 — (0.95+0.88+0.20)/3 = 0.677
    assert 0.6 < r["consensus"]["score"] < 0.75


def test_multi_verifier_tie_yields_disputed(tmp_path):
    """2 passed + 2 failed — no single winner — status disputed."""
    storage = Storage(tmp_path / "tasks.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()
    verifiers = [generate_keypair() for _ in range(4)]

    t0 = int(time.time())
    req = _make_request(priv_req, pub_req, ts=t0)
    _post(client, req)
    task_id = req["id"]
    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=t0 + 1)
    _post(client, acc)
    res = _make_result(priv_prov, pub_prov, task_id, acc["id"], pub_req, ts=t0 + 2)
    _post(client, res)

    for i, ((priv_v, pub_v), verdict) in enumerate(
        zip(verifiers, ["passed", "passed", "failed", "failed"])
    ):
        v = _make_verify(priv_v, pub_v, task_id, res["id"], pub_prov,
                         verdict=verdict, score=0.5, ts=t0 + 3 + i)
        _post(client, v)

    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "disputed"
    assert r["consensus"]["verdict"] == "disputed"
    assert r["consensus"]["verifier_count"] == 4


# ---------- cancellation -------------------------------------------------


def test_cancellation_before_accept(tmp_path):
    storage = Storage(tmp_path / "tasks.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    t0 = int(time.time())
    req = _make_request(priv_req, pub_req, ts=t0)
    _post(client, req)
    task_id = req["id"]

    cancel = _make_cancel(priv_req, pub_req, task_id, ts=t0 + 1)
    _post(client, cancel)

    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "cancelled"
    assert len(r["cancels"]) == 1


def test_cancellation_after_accept_is_ignored(tmp_path):
    """Cancel issued *after* an accept is recorded but the task remains accepted."""
    storage = Storage(tmp_path / "tasks.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()
    t0 = int(time.time())
    req = _make_request(priv_req, pub_req, ts=t0)
    _post(client, req)
    task_id = req["id"]

    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=t0 + 1)
    _post(client, acc)
    cancel = _make_cancel(priv_req, pub_req, task_id, ts=t0 + 2)
    _post(client, cancel)

    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "accepted"
    assert len(r["cancels"]) == 1  # recorded but ignored


def test_cancellation_by_non_requester_is_ignored(tmp_path):
    storage = Storage(tmp_path / "tasks.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_other, pub_other = generate_keypair()
    t0 = int(time.time())
    req = _make_request(priv_req, pub_req, ts=t0)
    _post(client, req)
    task_id = req["id"]

    cancel = _make_cancel(priv_other, pub_other, task_id, ts=t0 + 1)
    _post(client, cancel)

    r = client.get(f"/task/{task_id}").json()
    assert r["status"] == "pending"


# ---------- storage helper directly --------------------------------------


def test_storage_get_task_thread_returns_chronologically(tmp_path):
    """get_task_thread returns the request + every e-tag referencer, in time order."""
    storage = Storage(tmp_path / "tasks.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()
    t0 = int(time.time())
    req = _make_request(priv_req, pub_req, ts=t0)
    _post(client, req)
    acc = _make_accept(priv_prov, pub_prov, req["id"], pub_req, ts=t0 + 1)
    _post(client, acc)
    res = _make_result(priv_prov, pub_prov, req["id"], acc["id"], pub_req, ts=t0 + 2)
    _post(client, res)

    thread = storage.get_task_thread(req["id"])
    assert [e.kind for e in thread] == [50, 51, 52]
    assert [e.created_at for e in thread] == [t0, t0 + 1, t0 + 2]


def test_task_endpoint_404_for_unknown_task(tmp_path):
    storage = Storage(tmp_path / "tasks.db")
    client = TestClient(create_app(storage))
    bogus = "a" * 64
    r = client.get(f"/task/{bogus}")
    assert r.status_code == 404


def test_task_endpoint_400_for_malformed_task_id(tmp_path):
    storage = Storage(tmp_path / "tasks.db")
    client = TestClient(create_app(storage))
    r = client.get("/task/not-a-task-id")
    assert r.status_code == 400
