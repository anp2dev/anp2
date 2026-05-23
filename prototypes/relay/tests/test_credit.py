"""Tests for the ANP2 operator-issued credit economy (PROTOCOL (JP-redacted)18.11).

Credit is a relay-derived ledger: balances are a pure function of the event
log (kinds 50/51/52/53/55), never stored. On each passed settlement the
relay routes a 10% transaction fee to a fixed treasury agent; across
{requester, provider, treasury} the sum is always exactly zero. The relay
does NOT enforce a hard credit limit at publish (JP-redacted) provider acceptance is
voluntary.

Settlement counts only a NEUTRAL verifier's verdict (a kind 53 from an
agent that is neither the requester nor the provider, and not the treasury
(JP-redacted) (JP-redacted)18.6 / (JP-redacted)18.11), so neither side can mint credit by self-verifying.

Coverage:
  (a) full credit task: passed -> requester -N, provider +(N-fee), treasury +fee
  (b) a failed task moves zero credit
  (c) an open task locks the requester's amount
  (d) no hard credit limit at publish (the old 422 enforcement was removed)
  (e) GET /agents/<id>/credit returns the right shape, with fee accounting
  (f) a disputed task is terminal, not left locked
  (g) cancel is honoured only from the requester ((JP-redacted)18.9)
  (h) self-attested verdicts (requester/provider) do NOT settle credit
  (i) treasury accrues fees across multiple passed settlements
  (j) standing-accrual guards: self-tasks and zero-reward tasks do NOT
      inflate verified_provider_tasks
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


def _make_credit_request(priv: str, pub: str, *, amount: int,
                         capability: str = "transform.text.demo",
                         deadline_unix: int | None = None,
                         ts: int | None = None) -> dict:
    """A kind 50 task.request priced in ANP2 credit."""
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


def _make_cancel(priv: str, pub: str, task_id: str, *,
                 capability: str = "transform.text.demo", ts: int | None = None) -> dict:
    ts = ts if ts is not None else int(time.time())
    body = {"task_id": task_id, "reason": "test cancel"}
    tags = [["e", task_id, "root"], ["e", task_id, "cancel"], ["t", capability]]
    return _sign(priv, pub, 55, tags, json.dumps(body, separators=(",", ":")), ts)


def _run_task(client, *, priv_req, pub_req, priv_prov, pub_prov, priv_ver, pub_ver,
              amount, verdict="passed", t0=None):
    """Drive a full kind 50->51->52->(neutral 53) lifecycle. Returns task_id."""
    t0 = t0 if t0 is not None else int(time.time())
    req = _make_credit_request(priv_req, pub_req, amount=amount, ts=t0)
    _post(client, req)
    task_id = req["id"]
    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=t0 + 1)
    _post(client, acc)
    res = _make_result(priv_prov, pub_prov, task_id, acc["id"], pub_req, ts=t0 + 2)
    _post(client, res)
    ver = _make_verify(priv_ver, pub_ver, task_id, res["id"], pub_prov,
                       verdict=verdict, ts=t0 + 3)
    _post(client, ver)
    return task_id


# ---------- (a) full credit task settles -N/+N ---------------------------


def test_credit_full_task_settles_debit_and_credit(tmp_path):
    """kind 50 (anp2_credit, N) + 51 + 52 + neutral 53 passed
    -> requester -N, provider +(N - fee), treasury +fee (10% floor,
    PROTOCOL (JP-redacted)18.11). Sum across {requester, provider, treasury} is zero."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()
    priv_ver, pub_ver = generate_keypair()
    amount = 10
    fee = 1                # 10% floor of 10
    provider_share = 9

    _run_task(client, priv_req=priv_req, pub_req=pub_req,
              priv_prov=priv_prov, pub_prov=pub_prov,
              priv_ver=priv_ver, pub_ver=pub_ver, amount=amount, verdict="passed")

    req_credit = storage.credit_for(pub_req)
    prov_credit = storage.credit_for(pub_prov)
    trs_credit = storage.credit_for(Storage.ANP2_TREASURY_AGENT_ID)

    assert req_credit["balance"] == -amount
    assert prov_credit["balance"] == provider_share
    assert trs_credit["balance"] == fee
    # settled task -> nothing locked anymore
    assert req_credit["locked"] == 0
    assert req_credit["available"] == -amount
    assert prov_credit["available"] == provider_share
    # invariant: total across {requester, provider, treasury} is exactly zero
    assert (req_credit["balance"] + prov_credit["balance"]
            + trs_credit["balance"]) == 0
    # provider accrues a verified-provider-task
    assert prov_credit["verified_provider_tasks"] == 1


# ---------- (b) failed task moves zero credit ----------------------------


def test_credit_failed_task_moves_zero(tmp_path):
    """A neutral kind 53 verdict=failed settles the task with no credit move."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()
    priv_ver, pub_ver = generate_keypair()
    amount = 8

    _run_task(client, priv_req=priv_req, pub_req=pub_req,
              priv_prov=priv_prov, pub_prov=pub_prov,
              priv_ver=priv_ver, pub_ver=pub_ver, amount=amount, verdict="failed")

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
    amount = 8

    _post(client, _make_credit_request(priv_req, pub_req, amount=amount, ts=t0))

    req_credit = storage.credit_for(pub_req)
    # open task: balance untouched, amount locked, available pulled down
    assert req_credit["balance"] == 0
    assert req_credit["locked"] == amount
    assert req_credit["available"] == -amount
    # a fresh requester has zero provider history
    assert req_credit["verified_provider_tasks"] == 0


# ---------- (d) no hard credit limit at publish (phase 0/1) --------------


def test_no_hard_credit_limit_enforcement(tmp_path):
    """PROTOCOL (JP-redacted)18.11 (phase 0/1): the relay does NOT enforce a hard credit
    limit at publish. Any agent may post a kind-50 task.request regardless of
    balance (JP-redacted) provider acceptance is voluntary and informed by the requester's
    public balance / history. The relay still rejects malformed rewards
    (negative amount) with HTTP 400, and `mocked` reward tasks remain
    accepted (they don't touch the credit ledger at all)."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    t0 = int(time.time())

    # A fresh agent (balance 0) can post any non-negative anp2_credit amount.
    r1 = client.post("/events", json=_make_credit_request(
        priv_req, pub_req, amount=10000, ts=t0))
    assert r1.status_code == 200, r1.text

    # Posting a second huge task is also fine (JP-redacted) no hard cap, even though the
    # requester now has 10000 of open `locked` exposure.
    r2 = client.post("/events", json=_make_credit_request(
        priv_req, pub_req, amount=99999, ts=t0 + 1))
    assert r2.status_code == 200, r2.text

    # `mocked` reward tasks remain accepted (they never touched the credit gate).
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
    assert client.post("/events", json=mocked).status_code == 200

    # A negative reward amount IS still rejected at publish with HTTP 400.
    neg_body = json.loads(_make_credit_request(priv_req, pub_req, amount=1, ts=t0 + 3)["content"])
    neg_body["reward"]["amount"] = -5
    priv_neg, pub_neg = generate_keypair()
    neg = _sign(priv_neg, pub_neg, 50,
                [["t", "transform.text.demo"], ["cap_wanted", "transform.text.demo"]],
                json.dumps(neg_body, separators=(",", ":")), t0 + 4)
    assert client.post("/events", json=neg).status_code == 400


# ---------- (e) GET /agents/<id>/credit shape ----------------------------


def test_credit_endpoint_returns_expected_shape(tmp_path):
    """GET /agents/<id>/credit (and /api/...) returns the (JP-redacted)18.11 shape, with
    the 10% treasury fee correctly accounted for on the provider side."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()
    priv_ver, pub_ver = generate_keypair()
    amount = 10               # fee = 1, provider_share = 9
    provider_share = 9

    _run_task(client, priv_req=priv_req, pub_req=pub_req,
              priv_prov=priv_prov, pub_prov=pub_prov,
              priv_ver=priv_ver, pub_ver=pub_ver, amount=amount, verdict="passed")

    for path in (f"/agents/{pub_req}/credit", f"/api/agents/{pub_req}/credit"):
        r = client.get(path)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) == {
            "agent_id", "balance", "locked", "available",
            "verified_provider_tasks",
        }
        assert body["agent_id"] == pub_req
        assert body["balance"] == -amount
        assert body["locked"] == 0
        assert body["available"] == -amount
        assert body["verified_provider_tasks"] == 0

    # provider side (JP-redacted) receives 90% of the gross reward
    r = client.get(f"/agents/{pub_prov}/credit")
    assert r.status_code == 200
    assert r.json()["balance"] == provider_share
    assert r.json()["verified_provider_tasks"] == 1

    # bad agent_id -> 400
    assert client.get("/agents/not-hex/credit").status_code == 400

    # credit_balance is surfaced on the rich agent view
    view = client.get(f"/agents/{pub_req}").json()
    assert view["credit_balance"] == -amount


# ---------- (f) disputed task is terminal, not left locked ---------------


def test_credit_disputed_task_not_locked(tmp_path):
    """Conflicting NEUTRAL kind-53 verdicts (>=1 passed AND >=1 failed) make
    the task `disputed` (JP-redacted) terminal, moves zero credit, not left locked
    (PROTOCOL (JP-redacted)18.7 / (JP-redacted)18.11)."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))
    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()
    priv_v1, pub_v1 = generate_keypair()
    priv_v2, pub_v2 = generate_keypair()
    t0 = int(time.time())
    amount = 8

    req = _make_credit_request(priv_req, pub_req, amount=amount, ts=t0)
    _post(client, req)
    task_id = req["id"]
    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=t0 + 1)
    _post(client, acc)
    res = _make_result(priv_prov, pub_prov, task_id, acc["id"], pub_req, ts=t0 + 2)
    _post(client, res)
    # two NEUTRAL verifiers disagree -> disputed
    _post(client, _make_verify(priv_v1, pub_v1, task_id, res["id"], pub_prov,
                               verdict="passed", ts=t0 + 3))
    _post(client, _make_verify(priv_v2, pub_v2, task_id, res["id"], pub_prov,
                               verdict="failed", ts=t0 + 4))

    req_credit = storage.credit_for(pub_req)
    assert req_credit["balance"] == 0
    assert req_credit["locked"] == 0       # disputed is terminal, not open
    assert req_credit["available"] == 0
    assert storage.credit_for(pub_prov)["balance"] == 0


# ---------- (g) cancel honoured only from the requester ((JP-redacted)18.9) ----------


def test_credit_cancel_only_from_requester(tmp_path):
    """PROTOCOL (JP-redacted)18.9: only the requester can cancel, only before any kind-51
    accept. A kind-55 from a third party must NOT release the requester's
    locked credit."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))
    priv_req, pub_req = generate_keypair()
    priv_other, pub_other = generate_keypair()
    t0 = int(time.time())
    amount = 8

    req = _make_credit_request(priv_req, pub_req, amount=amount, ts=t0)
    _post(client, req)
    task_id = req["id"]
    assert storage.credit_for(pub_req)["locked"] == amount   # open -> locked

    # a kind-55 from a NON-requester must be ignored (JP-redacted) still locked
    _post(client, _make_cancel(priv_other, pub_other, task_id, ts=t0 + 1))
    assert storage.credit_for(pub_req)["locked"] == amount

    # a kind-55 from the requester (no accept yet) releases the lock
    _post(client, _make_cancel(priv_req, pub_req, task_id, ts=t0 + 2))
    assert storage.credit_for(pub_req)["locked"] == 0


# ---------- (h) self-attested verdicts do NOT settle credit --------------


def test_credit_self_verification_does_not_settle(tmp_path):
    """A kind-53 authored by the requester or the provider carries no
    settlement weight (PROTOCOL (JP-redacted)18.6 / (JP-redacted)18.11) (JP-redacted) credit must not move, so a
    provider cannot mint credit by passing its own task. The task stays open
    (the requester's amount stays locked) until a neutral verdict arrives."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))
    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()
    priv_ver, pub_ver = generate_keypair()
    t0 = int(time.time())
    amount = 8

    req = _make_credit_request(priv_req, pub_req, amount=amount, ts=t0)
    _post(client, req)
    task_id = req["id"]
    acc = _make_accept(priv_prov, pub_prov, task_id, pub_req, ts=t0 + 1)
    _post(client, acc)
    res = _make_result(priv_prov, pub_prov, task_id, acc["id"], pub_req, ts=t0 + 2)
    _post(client, res)

    # provider self-verifies passed (JP-redacted) must NOT settle
    _post(client, _make_verify(priv_prov, pub_prov, task_id, res["id"], pub_prov,
                               verdict="passed", ts=t0 + 3))
    # requester self-verifies passed (JP-redacted) must NOT settle either
    _post(client, _make_verify(priv_req, pub_req, task_id, res["id"], pub_prov,
                               verdict="passed", ts=t0 + 4))
    assert storage.credit_for(pub_prov)["balance"] == 0
    assert storage.credit_for(pub_req)["balance"] == 0
    assert storage.credit_for(pub_req)["locked"] == amount   # still open

    # a NEUTRAL verifier's passed verdict finally settles it
    _post(client, _make_verify(priv_ver, pub_ver, task_id, res["id"], pub_prov,
                               verdict="passed", ts=t0 + 5))
    assert storage.credit_for(pub_prov)["balance"] == amount
    assert storage.credit_for(pub_req)["balance"] == -amount
    assert storage.credit_for(pub_req)["locked"] == 0


# ---------- (i) treasury fee accrual across multiple settlements --------


def test_treasury_accrues_fee_across_multiple_passed_tasks(tmp_path):
    """Across N passed kind-50 settlements the treasury's balance equals the
    sum of fees, and the zero-sum invariant holds across all participants.
    This exercises the fee-recycling property of PROTOCOL (JP-redacted)18.11."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_prov, pub_prov = generate_keypair()
    priv_ver, pub_ver = generate_keypair()

    # Base the timeline slightly in the past so every event stays inside the
    # relay's clock-skew window.
    t0 = int(time.time()) - 60
    amount = 20            # fee per task = 2; provider net per task = 18
    fee_per_task = 2
    n_tasks = 3

    requester_pubs: list[str] = []
    for i in range(n_tasks):
        priv_req, pub_req = generate_keypair()
        requester_pubs.append(pub_req)
        _run_task(client, priv_req=priv_req, pub_req=pub_req,
                  priv_prov=priv_prov, pub_prov=pub_prov,
                  priv_ver=priv_ver, pub_ver=pub_ver, amount=amount,
                  t0=t0 + i * 10)

    prov_credit = storage.credit_for(pub_prov)
    trs_credit = storage.credit_for(Storage.ANP2_TREASURY_AGENT_ID)

    assert prov_credit["verified_provider_tasks"] == n_tasks
    assert prov_credit["balance"] == n_tasks * (amount - fee_per_task)
    assert trs_credit["balance"] == n_tasks * fee_per_task

    # zero-sum across {all requesters, provider, treasury}
    requester_sum = sum(storage.credit_for(p)["balance"] for p in requester_pubs)
    assert (requester_sum + prov_credit["balance"]
            + trs_credit["balance"]) == 0


# ---------- (j) standing-accrual guards (Sybil mitigation) --------------


def test_self_task_does_not_inflate_verified_provider_tasks(tmp_path):
    """A task where requester == provider is a closed loop that does no real
    work for anyone else. It must NOT inflate `verified_provider_tasks` (JP-redacted)
    otherwise a single sock-puppet could farm standing for free by riding an
    automatic neutral verifier (PROTOCOL (JP-redacted)18.11)."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_a, pub_a = generate_keypair()          # requester == provider
    priv_v, pub_v = generate_keypair()           # neutral verifier
    t0 = int(time.time())
    amount = 10

    # A self-task: pub_a posts the kind-50, accepts it as itself, delivers
    # the kind-52 as itself; a neutral verifier (pub_v, != pub_a) passes it.
    req = _make_credit_request(priv_a, pub_a, amount=amount, ts=t0)
    _post(client, req)
    task_id = req["id"]
    acc = _make_accept(priv_a, pub_a, task_id, pub_a, ts=t0 + 1)
    _post(client, acc)
    res = _make_result(priv_a, pub_a, task_id, acc["id"], pub_a, ts=t0 + 2)
    _post(client, res)
    _post(client, _make_verify(priv_v, pub_v, task_id, res["id"], pub_a,
                               verdict="passed", ts=t0 + 3))

    a_credit = storage.credit_for(pub_a)
    # The two settlement legs net to -fee on the combined entity.
    fee = 1
    assert a_credit["balance"] == -fee
    # The critical guard: standing does NOT accrue from a self-task.
    assert a_credit["verified_provider_tasks"] == 0
    # Treasury still received the fee.
    assert storage.credit_for(Storage.ANP2_TREASURY_AGENT_ID)["balance"] == fee


def test_zero_reward_task_does_not_inflate_verified_provider_tasks(tmp_path):
    """A passed task with reward.amount=0 moves no credit. It must also NOT
    inflate `verified_provider_tasks`, otherwise zero-cost cycles could farm
    standing for free."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))

    priv_req, pub_req = generate_keypair()
    priv_prov, pub_prov = generate_keypair()
    priv_ver, pub_ver = generate_keypair()
    t0 = int(time.time())

    _run_task(client, priv_req=priv_req, pub_req=pub_req,
              priv_prov=priv_prov, pub_prov=pub_prov,
              priv_ver=priv_ver, pub_ver=pub_ver, amount=0, verdict="passed",
              t0=t0)

    prov_credit = storage.credit_for(pub_prov)
    assert prov_credit["balance"] == 0
    # The critical guard: standing does NOT accrue from a zero-reward task.
    assert prov_credit["verified_provider_tasks"] == 0
