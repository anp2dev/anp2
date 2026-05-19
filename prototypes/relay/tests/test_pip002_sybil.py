"""Tests for PIP-002 (JP-redacted) kind 6 PoW validation + sybil_factor_pow.

Covers:
  - kind 6 with valid PoW is accepted and lifts sybil_factor_pow > 0
  - kind 6 without a pow tag still accepted (back-compat), sybil_factor_pow = 1.0
  - kind 6 with malformed pow tag rejected (400)
  - kind 6 with declared bits below relay minimum rejected (400)
  - kind 6 lying about PoW (declared >= min but real id has fewer leading
    zeros) rejected (400)
  - sybil_factor_pow exactly matches the spec formula
    tanh((JP-redacted) 2^pow_bits / 2^16) for the synthetic vote set
  - sybil_factor_pow is bounded in (0, 1)
"""

from __future__ import annotations

import json
import math
import time

from fastapi.testclient import TestClient

from anp2_relay.crypto import compute_event_id, generate_keypair, sign_event_id
from anp2_relay.pow import (
    PIP_002_MIN_BITS,
    SYBIL_NORM_CONSTANT,
    count_leading_zero_bits,
    event_id_bytes,
    validate_kind6_pow,
)
from anp2_relay.server import create_app
from anp2_relay.storage import Storage
from anp2_relay.trust import Vote, compute_trust, sybil_factor_pow


# ---------- helpers ------------------------------------------------------


def _mint_pow_vote(
    priv: str,
    pub: str,
    target: str,
    score: int,
    ts: int,
    pow_bits: int = PIP_002_MIN_BITS,
    max_iters: int = 1 << 22,
) -> dict:
    """Mint a kind 6 trust_vote event with a valid PoW tag.

    Iterates a `nonce` tag until SHA256(canonical_payload) has at least
    `pow_bits` leading zero bits. Returns a fully signed event dict ready
    for POST /events.
    """
    kind = 6
    content = json.dumps({"score": score})
    base_tags: list[list[str]] = [["p", target], ["pow", str(pow_bits)]]
    for nonce in range(max_iters):
        tags = base_tags + [["nonce", str(nonce)]]
        digest = event_id_bytes(pub, ts, kind, tags, content)
        if count_leading_zero_bits(digest) >= pow_bits:
            eid = digest.hex()
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
    raise RuntimeError(f"mining exhausted at {pow_bits} bits after {max_iters} iters")


def _plain_vote(priv: str, pub: str, target: str, score: int, ts: int) -> dict:
    """Build a kind 6 event without a pow tag (pre-PIP-002 shape)."""
    kind = 6
    tags: list[list[str]] = [["p", target]]
    content = json.dumps({"score": score})
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


# ---------- unit tests on validate_kind6_pow ----------------------------


def test_validate_no_pow_tag_accepts():
    """kind 6 with no pow tag is accepted (back-compat phase)."""
    priv, pub = generate_keypair()
    _, target = generate_keypair()
    ts = int(time.time())
    tags = [["p", target]]
    content = json.dumps({"score": 1})
    eid = compute_event_id(pub, ts, 6, tags, content)
    ok, err = validate_kind6_pow(eid, pub, ts, 6, tags, content)
    assert ok, err
    assert err is None


def test_validate_malformed_pow_tag_rejects():
    """A pow tag without a parseable integer is rejected."""
    priv, pub = generate_keypair()
    _, target = generate_keypair()
    ts = int(time.time())
    tags = [["p", target], ["pow", "not-an-int"]]
    content = json.dumps({"score": 1})
    eid = compute_event_id(pub, ts, 6, tags, content)
    ok, err = validate_kind6_pow(eid, pub, ts, 6, tags, content)
    assert not ok
    assert "malformed" in (err or "")


def test_validate_pow_below_minimum_rejects():
    """Declared pow_bits below the relay minimum is rejected."""
    priv, pub = generate_keypair()
    _, target = generate_keypair()
    ts = int(time.time())
    # Mine an 8-bit PoW, declare 8 bits (below 12-bit floor)
    payload = _mint_pow_vote(priv, pub, target, 1, ts, pow_bits=8, max_iters=1 << 16)
    ok, err = validate_kind6_pow(
        payload["id"],
        payload["agent_id"],
        payload["created_at"],
        payload["kind"],
        payload["tags"],
        payload["content"],
    )
    assert not ok
    assert "below_minimum" in (err or "")


def test_validate_pow_does_not_meet_declared_rejects():
    """Lying about PoW (claimed 12 bits, real id has < 12 leading zeros) (JP-redacted) reject."""
    priv, pub = generate_keypair()
    _, target = generate_keypair()
    ts = int(time.time())
    # Build a kind 6 with claimed pow=12 but DO NOT mine. Use nonce=0.
    tags = [["p", target], ["pow", "12"], ["nonce", "0"]]
    content = json.dumps({"score": 1})
    eid = compute_event_id(pub, ts, 6, tags, content)
    # If by extreme luck nonce=0 yielded (JP-redacted)12 leading zeros, bail (JP-redacted) re-run
    # the test would catch the path; this asserts the actual leading-zero
    # check fires when it doesn't.
    actual = count_leading_zero_bits(bytes.fromhex(eid))
    if actual >= 12:
        # Pick a different ts to dodge the lucky hash.
        ts += 1
        eid = compute_event_id(pub, ts, 6, tags, content)
        actual = count_leading_zero_bits(bytes.fromhex(eid))
    assert actual < 12, "test setup: need a NON-mining id"
    ok, err = validate_kind6_pow(eid, pub, ts, 6, tags, content)
    assert not ok
    assert "does_not_meet_declared" in (err or "")


def test_validate_valid_pow_passes():
    """A genuinely mined PoW at the relay floor passes validation."""
    priv, pub = generate_keypair()
    _, target = generate_keypair()
    ts = int(time.time())
    payload = _mint_pow_vote(priv, pub, target, 1, ts, pow_bits=PIP_002_MIN_BITS)
    ok, err = validate_kind6_pow(
        payload["id"],
        payload["agent_id"],
        payload["created_at"],
        payload["kind"],
        payload["tags"],
        payload["content"],
    )
    assert ok, err


# ---------- POST /events integration ------------------------------------


def test_publish_kind6_with_valid_pow_accepted(tmp_path):
    storage = Storage(tmp_path / "p002a.db")
    client = TestClient(create_app(storage))

    priv, pub = generate_keypair()
    _, target = generate_keypair()
    ts = int(time.time())
    payload = _mint_pow_vote(priv, pub, target, 1, ts)
    r = client.post("/events", json=payload)
    assert r.status_code == 200, r.text


def test_publish_kind6_without_pow_still_accepted(tmp_path):
    """Back-compat: kind 6 without pow tag must remain publishable."""
    storage = Storage(tmp_path / "p002b.db")
    client = TestClient(create_app(storage))

    priv, pub = generate_keypair()
    _, target = generate_keypair()
    ts = int(time.time())
    payload = _plain_vote(priv, pub, target, 1, ts)
    r = client.post("/events", json=payload)
    assert r.status_code == 200, r.text


def test_publish_kind6_with_lying_pow_rejected(tmp_path):
    """Claimed pow_bits (JP-redacted) relay min but the canonical id does not satisfy it."""
    storage = Storage(tmp_path / "p002c.db")
    client = TestClient(create_app(storage))

    priv, pub = generate_keypair()
    _, target = generate_keypair()
    ts = int(time.time())
    tags = [["p", target], ["pow", "12"], ["nonce", "0"]]
    content = json.dumps({"score": 1})
    # bump ts until id has < 12 leading zero bits (almost-always true on
    # the first try, but loop a few to be deterministic).
    for _ in range(8):
        eid = compute_event_id(pub, ts, 6, tags, content)
        if count_leading_zero_bits(bytes.fromhex(eid)) < 12:
            break
        ts += 1
    sig = sign_event_id(eid, priv)
    r = client.post(
        "/events",
        json={
            "id": eid,
            "agent_id": pub,
            "created_at": ts,
            "kind": 6,
            "tags": tags,
            "content": content,
            "sig": sig,
        },
    )
    assert r.status_code == 400
    assert "does_not_meet_declared" in r.json()["detail"]


def test_publish_kind6_below_minimum_rejected(tmp_path):
    storage = Storage(tmp_path / "p002d.db")
    client = TestClient(create_app(storage))

    priv, pub = generate_keypair()
    _, target = generate_keypair()
    ts = int(time.time())
    payload = _mint_pow_vote(priv, pub, target, 1, ts, pow_bits=8, max_iters=1 << 16)
    r = client.post("/events", json=payload)
    assert r.status_code == 400
    assert "below_minimum" in r.json()["detail"]


# ---------- sybil_factor_pow formula ------------------------------------


def test_sybil_factor_pow_no_votes_is_one():
    """An agent with no incoming votes at all (JP-redacted) factor 1.0 (back-compat)."""
    assert sybil_factor_pow("nobody", []) == 1.0


def test_sybil_factor_pow_no_pow_tag_is_one():
    """Incoming votes WITHOUT pow tag (JP-redacted) factor 1.0 (back-compat)."""
    t_now = 1_700_000_000
    votes = [
        Vote(voter="A", target="T", score=1, created_at=t_now, pow_bits=None),
        Vote(voter="B", target="T", score=1, created_at=t_now, pow_bits=None),
    ]
    assert sybil_factor_pow("T", votes) == 1.0


def test_sybil_factor_pow_matches_spec_formula():
    """Direct check: sybil_factor_pow == tanh((JP-redacted) 2^pow_bits / NORM)."""
    t_now = 1_700_000_000
    votes = [
        Vote(voter="A", target="T", score=1, created_at=t_now, pow_bits=12),
        Vote(voter="B", target="T", score=1, created_at=t_now, pow_bits=12),
        Vote(voter="C", target="T", score=1, created_at=t_now, pow_bits=14),
    ]
    expected = math.tanh(((1 << 12) + (1 << 12) + (1 << 14)) / float(SYBIL_NORM_CONSTANT))
    got = sybil_factor_pow("T", votes)
    assert abs(got - expected) < 1e-12, f"expected {expected}, got {got}"


def test_sybil_factor_pow_bounded_below_or_equal_one():
    """tanh squashes any finite work sum into (0, 1]; large clusters saturate
    to 1.0 in float64 (JP-redacted) the spec's "bounded factor" property still holds."""
    t_now = 1_700_000_000
    votes = [
        Vote(voter=f"v{i}", target="T", score=1, created_at=t_now, pow_bits=20)
        for i in range(100)
    ]
    f = sybil_factor_pow("T", votes)
    assert 0 < f <= 1.0
    # A modest cluster (just below saturation) must stay strictly < 1.
    small = [
        Vote(voter=f"v{i}", target="T", score=1, created_at=t_now, pow_bits=12)
        for i in range(3)
    ]
    f_small = sybil_factor_pow("T", small)
    assert 0 < f_small < 1.0


def test_sybil_factor_pow_concentrates_with_more_work():
    """More cumulative PoW work (JP-redacted) factor closer to 1."""
    t_now = 1_700_000_000
    one = [Vote(voter="A", target="T", score=1, created_at=t_now, pow_bits=12)]
    many = [
        Vote(voter=f"v{i}", target="T", score=1, created_at=t_now, pow_bits=12)
        for i in range(50)
    ]
    assert sybil_factor_pow("T", one) < sybil_factor_pow("T", many)


# ---------- end-to-end: PoW votes show up in /trust ---------------------


def test_trust_endpoint_surfaces_sybil_factor_pow(tmp_path):
    """Publish 3 PoW-tagged votes, hit /trust/<target>, confirm field and value."""
    storage = Storage(tmp_path / "p002e.db")
    client = TestClient(create_app(storage))

    _, pub_target = generate_keypair()
    _, pub_pad1 = generate_keypair()
    _, pub_pad2 = generate_keypair()

    voters: list[tuple[str, str]] = [generate_keypair() for _ in range(3)]
    ts = int(time.time())
    bits = PIP_002_MIN_BITS
    for priv, pub in voters:
        for tgt in (pub_target, pub_pad1, pub_pad2):
            payload = _mint_pow_vote(priv, pub, tgt, 1, ts, pow_bits=bits)
            r = client.post("/events", json=payload)
            assert r.status_code == 200, r.text
            ts += 1

    r = client.get(f"/trust/{pub_target}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "sybil_factor_pow" in body
    # Three voters cast PoW votes for pub_target, each at `bits` bits
    expected = math.tanh(3 * (1 << bits) / float(SYBIL_NORM_CONSTANT))
    assert abs(body["sybil_factor_pow"] - expected) < 1e-9


def test_trust_endpoint_back_compat_factor_is_one(tmp_path):
    """An agent voted for ONLY by pre-PIP-002 (no pow tag) voters (JP-redacted) factor 1.0."""
    storage = Storage(tmp_path / "p002f.db")
    client = TestClient(create_app(storage))

    priv_a, pub_a = generate_keypair()
    _, pub_target = generate_keypair()
    _, pub_pad1 = generate_keypair()
    _, pub_pad2 = generate_keypair()
    ts = int(time.time())
    for tgt in (pub_target, pub_pad1, pub_pad2):
        payload = _plain_vote(priv_a, pub_a, tgt, 1, ts)
        r = client.post("/events", json=payload)
        assert r.status_code == 200, r.text
        ts += 1

    r = client.get(f"/trust/{pub_target}")
    assert r.status_code == 200
    body = r.json()
    assert body["sybil_factor_pow"] == 1.0


def test_trust_graph_includes_sybil_factor_pow(tmp_path):
    """The /trust_graph endpoint exposes sybil_factor_pow for every target."""
    storage = Storage(tmp_path / "p002g.db")
    client = TestClient(create_app(storage))

    priv_a, pub_a = generate_keypair()
    _, t1 = generate_keypair()
    _, t2 = generate_keypair()
    _, t3 = generate_keypair()
    ts = int(time.time())
    for tgt in (t1, t2, t3):
        payload = _mint_pow_vote(priv_a, pub_a, tgt, 1, ts)
        assert client.post("/events", json=payload).status_code == 200
        ts += 1

    r = client.get("/trust_graph")
    assert r.status_code == 200
    body = r.json()
    assert len(body["agents"]) == 3
    for entry in body["agents"]:
        assert "sybil_factor_pow" in entry
        assert 0 < entry["sybil_factor_pow"] <= 1.0
