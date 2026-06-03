"""Regression tests for /api/welcome (the relay's own zero-to-first-event onboarding).

Two bugs fixed 2026-06-03:
1. The `quickstart_python` snippet built a kind-0 event with NO proof-of-work, so a
   newcomer who copy-pasted it hit HTTP 400 ("pow tag required for kind 0") — kind 0 is
   in PIP_002_MANDATORY_KINDS. The relay was handing out a join script that cannot join.
2. `claimable_first_task` excluded only kind-52/53-tagged tasks, so it could offer a task
   that another agent had already ACCEPTED (kind-51) — sending the newcomer on work that
   cannot settle. It now excludes kind-51 (accepted) and kind-52 (resulted).
"""

import json
import time

from fastapi.testclient import TestClient

from anp2_relay import pow as powmod
from anp2_relay.crypto import compute_event_id, generate_keypair, sign_event_id
from anp2_relay.events import Event
from anp2_relay.server import create_app
from anp2_relay.storage import Storage


def _ev(priv, pub, kind, content, tags):
    ts = int(time.time())
    eid = compute_event_id(pub, ts, kind, tags, content)
    return Event(
        id=eid, agent_id=pub, created_at=ts, kind=kind,
        tags=tags, content=content, sig=sign_event_id(eid, priv),
    )


def test_welcome_quickstart_mints_pow_for_kind0(tmp_path):
    """The shipped snippet must mine PoW for kind 0 — guard against re-shipping the
    no-PoW version that 400s on publish."""
    client = TestClient(create_app(Storage(tmp_path / "t.db")))
    script = client.get("/api/welcome").json()["quickstart_python"]
    # The exact constructs that make it a PoW miner (regression guard).
    assert "while True" in script
    assert "'pow'" in script
    assert "nonce" in script
    assert "bit_length()" in script


def test_welcome_quickstart_pow_algorithm_is_valid():
    """Run the algorithm the snippet uses and prove the relay's mandatory-PoW validator
    accepts the result (i.e. the snippet doesn't just *mention* pow — it actually works)."""
    priv, pub = generate_keypair()
    ts, kind = int(time.time()), 0
    content = json.dumps({"name": "MyFirstAgent", "description": "x", "model_family": "y"})
    bits, nonce = 12, 0
    while True:  # mirrors the snippet line-for-line
        tags = [["pow", str(bits)], ["nonce", str(nonce)]]
        eid = compute_event_id(pub, ts, kind, tags, content)
        if int.from_bytes(bytes.fromhex(eid), "big").bit_length() <= 256 - bits:
            break
        nonce += 1
    ok, err = powmod.validate_event_pow(eid, pub, ts, kind, tags, content, mandatory=True)
    assert ok, err
    assert powmod.count_leading_zero_bits(bytes.fromhex(eid)) >= bits


def test_claimable_excludes_accepted_task(tmp_path):
    st = Storage(tmp_path / "t.db")
    client = TestClient(create_app(st))
    priv, pub = generate_keypair()
    now = int(time.time())

    # Task A: open, unaccepted -> offerable.
    taskA = _ev(priv, pub, 50, json.dumps({"x": 1}), [["cap_wanted", "demo.echo"]])
    st.insert(taskA, now)
    # Task B: open but already ACCEPTED via a kind-51 that e-tags it -> NOT offerable.
    taskB = _ev(priv, pub, 50, json.dumps({"x": 2}), [["cap_wanted", "demo.echo"]])
    st.insert(taskB, now)
    st.insert(_ev(priv, pub, 51, "", [["e", taskB.id]]), now)

    claim = client.get("/api/welcome").json()["claimable_first_task"]
    assert claim.get("task_id") == taskA.id  # B is excluded because it is taken

    # Now accept A too -> nothing offerable -> honest guidance object (NOT a dead null).
    st.insert(_ev(priv, pub, 51, "", [["e", taskA.id]]), now)
    claim2 = client.get("/api/welcome").json()["claimable_first_task"]
    assert claim2 is not None
    assert "task_id" not in claim2
    assert claim2.get("status") == "none_unclaimed_right_now"


def test_claimable_prefers_reserved_bootstrap_task(tmp_path):
    """A task reserved for the caller (bootstrap_for=<key>) is preferred over a generic
    open task — it's the one the seeds step aside from, so it's actually claimable."""
    st = Storage(tmp_path / "t.db")
    client = TestClient(create_app(st))
    seed_priv, seed_pub = generate_keypair()
    _, newcomer = generate_keypair()
    now = int(time.time())

    # A generic open task (would satisfy the fallback) ...
    generic = _ev(seed_priv, seed_pub, 50, json.dumps({"x": 1}), [["cap_wanted", "demo.echo"]])
    st.insert(generic, now)
    # ... and a task RESERVED for the newcomer.
    reserved = _ev(seed_priv, seed_pub, 50, json.dumps({"x": 2}),
                   [["cap_wanted", "transform.text.demo"], ["bootstrap_for", newcomer]])
    st.insert(reserved, now)

    claim = client.get(f"/api/welcome?key={newcomer}").json()["claimable_first_task"]
    assert claim.get("task_id") == reserved.id
    assert claim.get("reserved_for_you") is True


def test_claimable_never_offers_someone_elses_reserved_task(tmp_path):
    """A bootstrap task reserved for another agent must never be offered to this caller."""
    st = Storage(tmp_path / "t.db")
    client = TestClient(create_app(st))
    seed_priv, seed_pub = generate_keypair()
    _, other = generate_keypair()
    now = int(time.time())

    reserved_for_other = _ev(seed_priv, seed_pub, 50, json.dumps({"x": 1}),
                             [["cap_wanted", "transform.text.demo"], ["bootstrap_for", other]])
    st.insert(reserved_for_other, now)

    # Anonymous caller: the only open task is reserved for someone else -> guidance, not that task.
    claim = client.get("/api/welcome").json()["claimable_first_task"]
    assert claim.get("task_id") is None
    assert claim.get("status") == "none_unclaimed_right_now"
