"""Tests for the trust.v1 algorithm in anp2_relay.trust.

Covers: simple A+B chain, sybil cluster dampening, time decay effect,
convergence detection, and the Storage / FastAPI integration surface.
"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from anp2_relay.crypto import compute_event_id, generate_keypair, sign_event_id
from anp2_relay.server import create_app
from anp2_relay.storage import Storage
from anp2_relay.trust import (
    HALF_LIFE_DAYS,
    MAX_ITERATIONS,
    MIN_DISTINCT_TARGETS,
    Vote,
    compute_trust,
    sybil_factor,
)


# ---------- direct algorithm tests (no DB) -------------------------------


def _v(voter: str, target: str, score: int = 1, age_days: float = 0.0, t_now: int = 1_700_000_000) -> Vote:
    return Vote(voter=voter, target=target, score=score, created_at=int(t_now - age_days * 86400))


def test_empty_votes_returns_empty_result():
    res = compute_trust([], t_now=1_700_000_000)
    assert res.weighted_score == {}
    assert res.iterations == 0
    assert res.converged


def test_simple_A_endorses_B():
    """One voter A (with 3 distinct targets to defeat sybil dampening) -> B.

    B's weighted_score should be > 0 and converged in <= MAX_ITERATIONS.
    """
    t_now = 1_700_000_000
    votes = [
        _v("A", "B", t_now=t_now),
        _v("A", "C", t_now=t_now),  # padding so A passes sybil check
        _v("A", "D", t_now=t_now),
    ]
    res = compute_trust(votes, t_now)
    assert res.weighted_score["B"] > 0
    assert res.weighted_score["C"] > 0
    assert res.weighted_score["D"] > 0
    assert res.converged
    assert res.iterations <= MAX_ITERATIONS


def test_trust_chain_A_to_B_to_C():
    """A endorses B (broadly), B endorses C (broadly).

    Once B accumulates trust, B's endorsement of C should give C non-zero
    weighted_score, demonstrating propagation across the chain.
    """
    t_now = 1_700_000_000
    votes = [
        # A -> B and pad A for sybil
        _v("A", "B", t_now=t_now),
        _v("A", "X1", t_now=t_now),
        _v("A", "X2", t_now=t_now),
        # B -> C and pad B for sybil
        _v("B", "C", t_now=t_now),
        _v("B", "Y1", t_now=t_now),
        _v("B", "Y2", t_now=t_now),
    ]
    res = compute_trust(votes, t_now)
    # C's score depends on B's weight which depends on A's bootstrap + B's earned trust.
    assert res.weighted_score["C"] > 0
    assert res.weighted_score["B"] > 0
    # raw_score is identical for B and C (one +1 vote each, fresh) (JP-redacted) proves
    # any difference between them comes purely from iterative weighting.
    assert abs(res.raw_score["B"] - res.raw_score["C"]) < 1e-9
    # B's voter (A) has only bootstrap weight; C's voter (B) has bootstrap + earned trust,
    # so the chain *amplifies* (JP-redacted) C ends up >= B. This is the propagation we want.
    assert res.weighted_score["C"] >= res.weighted_score["B"]


def test_sybil_cluster_is_dampened():
    """20 sybils each casting ONE vote for the same victim T should NOT
    dominate a single legitimate voter L with a broad voting history.

    Each sybil has 1 distinct target -> sybil_factor = 1/3.
    L has many distinct targets -> sybil_factor = 1.
    L's lone vote should weigh comparably to (or more than) one sybil's vote
    despite the 20:1 numerical disadvantage being mitigated.
    """
    t_now = 1_700_000_000
    votes: list[Vote] = []
    # 20 sybils each vote only for T
    for i in range(20):
        votes.append(_v(f"sybil_{i}", "T", t_now=t_now))
    # legitimate L votes for T plus many other targets
    votes.append(_v("L", "T", t_now=t_now))
    for j in range(10):
        votes.append(_v("L", f"other_{j}", t_now=t_now))

    res = compute_trust(votes, t_now)
    # Sybil dampening: each sybil contributes ~1/3 of what it would; the
    # voter_count of T is 21, but weighted_score reflects per-voter scaling.
    # Quantitatively: total weighted contrib to T from sybils =
    #   20 * sqrt(bootstrap_weight) * (1/3) * 1.0 (JP-redacted) 20 * 1 * 0.333 = 6.67
    # Legitimate L contrib =
    #   sqrt(bootstrap_weight) * 1.0 * 1.0 (JP-redacted) 1.0
    # Without dampening, sybils would contribute 20 * 1 * 1 = 20. So
    # dampening cut the sybil influence by 1/3, narrowing the gap.
    assert res.voter_count["T"] == 21

    # Now compare to the no-dampening counterfactual: simulate by giving each
    # sybil a 3rd-party diverse voting history so their sybil_factor = 1.
    votes_undamped = list(votes)
    for i in range(20):
        # give each sybil 2 extra distinct targets so they pass the threshold
        votes_undamped.append(_v(f"sybil_{i}", f"pad_a_{i}", t_now=t_now))
        votes_undamped.append(_v(f"sybil_{i}", f"pad_b_{i}", t_now=t_now))
    res_undamped = compute_trust(votes_undamped, t_now)
    # Undamped sybil contribution to T must be strictly larger than damped.
    assert res_undamped.weighted_score["T"] > res.weighted_score["T"]


def test_sybil_factor_threshold():
    """Direct test of the per-voter sybil_factor."""
    t_now = 1_700_000_000
    # 1 distinct target -> 1/3
    votes_one = [_v("A", "B", t_now=t_now)]
    assert sybil_factor("A", votes_one) == 1.0 / MIN_DISTINCT_TARGETS
    # MIN_DISTINCT_TARGETS distinct targets -> 1.0
    votes_three = [_v("A", f"T{i}", t_now=t_now) for i in range(MIN_DISTINCT_TARGETS)]
    assert sybil_factor("A", votes_three) == 1.0
    # more than threshold still capped at 1.0
    votes_many = [_v("A", f"T{i}", t_now=t_now) for i in range(10)]
    assert sybil_factor("A", votes_many) == 1.0


def test_time_decay_reduces_old_vote():
    """A vote one half-life old should contribute ~half of a fresh one."""
    t_now = 1_700_000_000
    fresh = [
        _v("A", "B", age_days=0, t_now=t_now),
        _v("A", "X1", t_now=t_now),
        _v("A", "X2", t_now=t_now),
    ]
    aged = [
        _v("A", "B", age_days=HALF_LIFE_DAYS, t_now=t_now),
        _v("A", "X1", t_now=t_now),
        _v("A", "X2", t_now=t_now),
    ]
    r_fresh = compute_trust(fresh, t_now)
    r_aged = compute_trust(aged, t_now)
    # B's weighted score should drop by ~50% (a single half-life).
    ratio = r_aged.weighted_score["B"] / r_fresh.weighted_score["B"]
    assert 0.45 < ratio < 0.55, f"expected ~0.5, got {ratio}"


def test_convergence_within_bound():
    """A moderately complex graph should converge well before MAX_ITERATIONS."""
    t_now = 1_700_000_000
    votes: list[Vote] = []
    # build a ring: A -> B -> C -> D -> A, with padding for each to defeat sybil
    ring = ["A", "B", "C", "D"]
    for i, v in enumerate(ring):
        target = ring[(i + 1) % len(ring)]
        votes.append(_v(v, target, t_now=t_now))
        votes.append(_v(v, f"{v}_pad1", t_now=t_now))
        votes.append(_v(v, f"{v}_pad2", t_now=t_now))
    res = compute_trust(votes, t_now)
    assert res.converged
    assert res.iterations < MAX_ITERATIONS
    # ring nodes all get non-trivial scores
    for n in ring:
        assert res.weighted_score[n] > 0


def test_negative_votes_reduce_score():
    """A -1 vote should pull weighted_score below zero (or below baseline)."""
    t_now = 1_700_000_000
    votes = [
        _v("A", "B", score=-1, t_now=t_now),
        _v("A", "X1", t_now=t_now),
        _v("A", "X2", t_now=t_now),
    ]
    res = compute_trust(votes, t_now)
    assert res.weighted_score["B"] < 0


# ---------- Storage / FastAPI integration --------------------------------


def _make_vote_event(priv: str, pub: str, target: str, score: int, ts: int) -> dict:
    """Build a signed kind 6 trust_vote event payload."""
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


def test_storage_trust_for_returns_extended_shape(tmp_path):
    """End-to-end: publish kind 6 events, query /trust/<id>, check new fields."""
    storage = Storage(tmp_path / "trust.db")
    client = TestClient(create_app(storage))

    priv_a, pub_a = generate_keypair()
    priv_b, pub_b = generate_keypair()
    # We need a "target" pubkey; doesn't need a private key for vote-receiving.
    _, pub_target = generate_keypair()
    _, pub_pad1 = generate_keypair()
    _, pub_pad2 = generate_keypair()

    ts = int(time.time())
    payloads = [
        _make_vote_event(priv_a, pub_a, pub_target, 1, ts),
        _make_vote_event(priv_a, pub_a, pub_pad1, 1, ts + 1),
        _make_vote_event(priv_a, pub_a, pub_pad2, 1, ts + 2),
        _make_vote_event(priv_b, pub_b, pub_target, 1, ts + 3),
        _make_vote_event(priv_b, pub_b, pub_pad1, 1, ts + 4),
        _make_vote_event(priv_b, pub_b, pub_pad2, 1, ts + 5),
    ]
    for p in payloads:
        r = client.post("/events", json=p)
        assert r.status_code == 200, r.text

    r = client.get(f"/trust/{pub_target}")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == pub_target
    assert body["voter_count"] == 2
    assert "weighted_score" in body
    assert "iterations" in body
    assert body["weighted_score"] > 0
    assert body["score_in"] > 0  # raw decayed sum


def test_storage_trust_graph_returns_all_agents(tmp_path):
    """Full graph endpoint returns one entry per voted-for agent, sorted."""
    storage = Storage(tmp_path / "graph.db")
    client = TestClient(create_app(storage))

    priv_a, pub_a = generate_keypair()
    _, pub_t1 = generate_keypair()
    _, pub_t2 = generate_keypair()
    _, pub_t3 = generate_keypair()

    ts = int(time.time())
    for tgt in (pub_t1, pub_t2, pub_t3):
        p = _make_vote_event(priv_a, pub_a, tgt, 1, ts)
        assert client.post("/events", json=p).status_code == 200
        ts += 1

    r = client.get("/trust_graph")
    assert r.status_code == 200
    body = r.json()
    assert body["algo"] == "trust.v1"
    assert len(body["agents"]) == 3
    # sorted descending by weighted_score
    scores = [a["weighted_score"] for a in body["agents"]]
    assert scores == sorted(scores, reverse=True)
    # each entry has the documented shape
    for a in body["agents"]:
        assert set(a.keys()) >= {"agent_id", "weighted_score", "raw_score", "voter_count"}
