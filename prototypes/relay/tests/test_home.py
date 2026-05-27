"""Unit tests for the /api/home agent runtime dashboard."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from anp2_relay.events import Event
from anp2_relay.server import create_app
from anp2_relay.storage import Storage


@pytest.fixture
def client(tmp_path):
    # Use file-backed sqlite (not :memory:) — Storage opens a fresh
    # connection per operation, and an in-memory DB does not persist
    # across connections, so storage.insert() would hit "no such table".
    storage = Storage(tmp_path / "home_test.db")
    app = create_app(storage)
    return TestClient(app), storage


def test_home_requires_agent_id(client):
    c, _ = client
    r = c.get("/api/home")
    assert r.status_code == 400
    assert "agent_id" in r.json()["detail"]


def test_home_rejects_short_agent_id(client):
    c, _ = client
    r = c.get("/api/home?agent_id=abc")
    assert r.status_code == 400


def test_home_rejects_non_hex_agent_id(client):
    c, _ = client
    r = c.get("/api/home?agent_id=" + "z" * 64)
    assert r.status_code == 400


def test_home_limit_bound_low(client):
    c, _ = client
    r = c.get("/api/home?agent_id=" + "0" * 64 + "&limit=0")
    # ge=1 → 422 from FastAPI
    assert r.status_code == 422


def test_home_limit_bound_high(client):
    c, _ = client
    r = c.get("/api/home?agent_id=" + "0" * 64 + "&limit=999999")
    # le=50 → 422 from FastAPI
    assert r.status_code == 422


def test_home_empty_agent_returns_shape(client):
    c, _ = client
    r = c.get("/api/home?agent_id=" + "0" * 64)
    assert r.status_code == 200
    d = r.json()
    expected = {
        "agent_id", "ts", "your_account",
        "unread_mentions", "open_tasks", "settlements_pending",
        "recent_trust_votes", "latest_announcement",
        "suggested_next_actions", "quick_links",
    }
    assert expected <= set(d.keys())


def test_home_bare_path_works(client):
    c, _ = client
    r = c.get("/home?agent_id=" + "0" * 64)
    assert r.status_code == 200


def _stub_event(*, kind: int, author: str, p_tag: str, event_id: str, content: str = "") -> "Event":
    """Build a minimally-valid Event for storage.insert() — bypasses signing.

    Storage.insert() does NOT verify signatures (signature validation lives
    in the POST /events handler). For unit tests we can fabricate events
    directly. id and sig just need to be 64-/128-hex of the right length.
    """
    from anp2_relay.events import Event
    return Event(
        id=event_id,
        agent_id=author,
        created_at=int(time.time()),
        kind=kind,
        tags=[["p", p_tag]],
        content=content,
        sig="f" * 128,
    )


def test_home_unread_mentions_filters_out_kind_3_dm(client):
    """CRITICAL: /api/home unread_mentions must NOT surface kind-3 / -6 / -7 / -54.

    Surfacing kind-3 would leak DM metadata (who messaged whom); surfacing
    kind-6 would expose pending trust votes (the recent_trust_votes key
    handles them separately); surfacing kind-7 would expose moderation
    activity; surfacing kind-54 would expose private settlement events.
    This test pins the security invariant by INSERTING real events that
    p-tag the target and asserting the response filters them out — the
    earlier version was vacuous (empty DB).
    """
    c, storage = client
    target = "1" * 64
    other = "2" * 64

    # Insert one event of each "forbidden" kind, p-tagging the target.
    forbidden_kinds = [3, 6, 7, 54]
    for i, k in enumerate(forbidden_kinds):
        eid = f"{k:02x}" + "a" * 62
        storage.insert(_stub_event(kind=k, author=other, p_tag=target, event_id=eid), int(time.time()))

    # Also insert one PERMITTED kind-1 mention so we know the endpoint
    # would surface ANYTHING — guards against "filter set to () returns []".
    storage.insert(_stub_event(kind=1, author=other, p_tag=target, event_id="01" + "b" * 62,
                                content="hi"), int(time.time()))

    r = c.get(f"/api/home?agent_id={target}")
    assert r.status_code == 200
    mentions = r.json()["unread_mentions"]

    # Positive: the kind-1 mention was surfaced (sanity — filter is not ())
    kinds_seen = [m["kind"] for m in mentions]
    assert 1 in kinds_seen, (
        f"sanity failed: kind-1 p-tag should be in unread_mentions; got {kinds_seen}"
    )

    # Negative: NONE of the forbidden kinds may appear
    for m in mentions:
        assert m["kind"] not in forbidden_kinds, (
            f"leakage: kind-{m['kind']} appeared in unread_mentions; "
            f"must be one of HOME_MENTION_KINDS, got {kinds_seen}"
        )


def test_home_suggested_actions_present(client):
    c, _ = client
    r = c.get("/api/home?agent_id=" + "0" * 64)
    d = r.json()
    assert isinstance(d["suggested_next_actions"], list)
    assert len(d["suggested_next_actions"]) >= 1


def test_home_your_account_registered_flag(client):
    """A fresh agent_id (= no kind-0 published) reports registered=False;
    after publishing a kind-0, reports registered=True. Lets new agents
    distinguish "I haven't joined" from "lookup failed"."""
    c, storage = client
    fresh = "a" * 64
    r = c.get(f"/api/home?agent_id={fresh}")
    assert r.json()["your_account"]["registered"] is False

    storage.insert(
        _stub_event(kind=0, author=fresh, p_tag=fresh, event_id="0a" + "0" * 62),
        int(time.time()),
    )
    r = c.get(f"/api/home?agent_id={fresh}")
    assert r.json()["your_account"]["registered"] is True


def test_agents_name_filter(client):
    """`/agents?name=<substring>` filters by case-insensitive substring on
    the agent's profile `name`. Fixes the §6 skill.md instruction to query
    `?name=taskreq` — the canonical seed name on the live network is
    `ANP2TaskRequester`, not bare `taskreq`."""
    c, storage = client
    aid = "b" * 64
    profile = '{"name":"ANP2TaskRequester","description":"seed"}'
    storage.insert(
        Event(
            id="0b" + "0" * 62, agent_id=aid, created_at=int(time.time()),
            kind=0, tags=[], content=profile, sig="f" * 128,
        ),
        int(time.time()),
    )
    r = c.get("/agents?name=taskreq")
    assert r.status_code == 200
    names = [a.get("name") for a in r.json()["agents"]]
    assert "ANP2TaskRequester" in names, names


def test_agents_name_filter_survives_malformed_profile(client):
    """A kind-0 with a non-string `name` (= `{}` instead of `"…"`) must
    NOT crash `/agents?name=foo`. Without the `isinstance(name, str)`
    guard, `a.get("name").lower()` raises AttributeError → HTTP 500 —
    trivial DoS by publishing a malformed profile."""
    c, storage = client
    aid = "f" * 64
    bad_profile = '{"name":{"object":"instead_of_string"}}'
    storage.insert(
        Event(
            id="0f" + "0" * 62, agent_id=aid, created_at=int(time.time()),
            kind=0, tags=[], content=bad_profile, sig="f" * 128,
        ),
        int(time.time()),
    )
    r = c.get("/agents?name=anything")
    assert r.status_code == 200, f"malformed profile triggered {r.status_code}"


def test_home_registered_cache_invalidates_on_kind0(client):
    """The TTL cache for `registered` must be invalidated by a freshly-
    inserted kind-0, so an agent who publishes their profile sees
    `registered: true` on the very next `/api/home` call (not 60 s later).
    The invalidation is wired via storage.add_listener in create_app."""
    c, storage = client
    aid = "c" * 64
    r = c.get(f"/api/home?agent_id={aid}").json()
    assert r["your_account"]["registered"] is False  # caches False
    storage.insert(
        _stub_event(kind=0, author=aid, p_tag=aid, event_id="0c" + "0" * 62),
        int(time.time()),
    )
    # Immediately after insert, the next /api/home call should NOT
    # serve the stale False from cache.
    r = c.get(f"/api/home?agent_id={aid}").json()
    assert r["your_account"]["registered"] is True


def test_home_my_profile_omitted_when_unregistered(client):
    """`quick_links.my_profile` MUST be omitted for an agent that hasn't
    yet published kind-0, because dereferencing it returns 404 and
    confuses newcomers about their onboarding state."""
    c, storage = client
    fresh = "e" * 64
    r = c.get(f"/api/home?agent_id={fresh}")
    ql = r.json()["quick_links"]
    assert "my_profile" not in ql, ql

    # After registering, my_profile should reappear.
    storage.insert(
        _stub_event(kind=0, author=fresh, p_tag=fresh, event_id="0e" + "0" * 62),
        int(time.time()),
    )
    r = c.get(f"/api/home?agent_id={fresh}")
    assert "my_profile" in r.json()["quick_links"]


def test_dms_endpoint_returns_only_sent(client):
    """`/dms/{agent_id}` returns ONLY DMs authored by agent_id (= sender
    outbox). The earlier version also returned DMs *received by* agent_id
    via the p-tag, which made the endpoint a one-shot lookup of the DM
    graph for any caller — a Phase 0/1 metadata leak. The receiver branch
    is gone; recipients use `/api/stream?kinds=3` instead."""
    c, storage = client
    alice = "1" * 64
    bob = "2" * 64
    # Alice sends Bob a DM
    storage.insert(
        _stub_event(kind=3, author=alice, p_tag=bob, event_id="03" + "a" * 62),
        int(time.time()),
    )
    # Bob sends Alice a DM
    storage.insert(
        _stub_event(kind=3, author=bob, p_tag=alice, event_id="03" + "b" * 62),
        int(time.time()),
    )

    # Alice's outbox: should contain ONLY the alice→bob DM
    r = c.get(f"/dms/{alice}")
    assert r.status_code == 200
    authors = [d["agent_id"] for d in r.json()["dms"]]
    assert authors == [alice], (
        f"/dms should return only sender's outbox; got authors={authors}. "
        "Receiving an inbound DM by p-tag is a metadata leak."
    )


def test_agent_id_lowercased_in_home(client):
    """Upper-case agent_id is accepted by the validator (case-insensitive
    hex) but MUST be normalized to lower before storage lookup, so the
    response is identical for AAAA... and aaaa..."""
    c, _ = client
    upper = "A" * 64
    lower = "a" * 64
    r_u = c.get(f"/api/home?agent_id={upper}").json()
    r_l = c.get(f"/api/home?agent_id={lower}").json()
    # agent_id echoed in response should be normalized to lower
    assert r_u["agent_id"] == lower
    assert r_l["agent_id"] == lower


def _stub_kind6(*, voter: str, target: str, score: float, reason: str, event_id: str,
                created_at: int | None = None) -> "Event":
    """Build a kind-6 trust_vote event for testing /api/agents/<id>/trust_received."""
    import json as _json
    return Event(
        id=event_id, agent_id=voter,
        created_at=created_at if created_at is not None else int(time.time()),
        kind=6,
        tags=[["p", target]],
        content=_json.dumps({"score": score, "reason": reason}),
        sig="f" * 128,
    )


def test_trust_received_basic_shape(client):
    """Returns the documented response shape with expected keys."""
    c, _ = client
    target = "a" * 64
    r = c.get(f"/api/agents/{target}/trust_received")
    assert r.status_code == 200
    d = r.json()
    expected = {"agent_id", "ts", "filter", "count", "score_sum", "votes"}
    assert expected <= set(d.keys())
    assert d["agent_id"] == target
    assert d["filter"]["since_sec"] == 86400 * 7
    assert d["filter"]["min_score"] == 0.0
    assert d["filter"]["limit"] == 50


def test_trust_received_filters_by_min_score(client):
    """min_score=0.5 must exclude scores below 0.5."""
    c, storage = client
    target = "b" * 64
    storage.insert(_stub_kind6(
        voter="1" * 64, target=target, score=0.9, reason="excellent",
        event_id="06" + "a" * 62), int(time.time()))
    storage.insert(_stub_kind6(
        voter="2" * 64, target=target, score=0.3, reason="meh",
        event_id="06" + "b" * 62), int(time.time()))
    storage.insert(_stub_kind6(
        voter="3" * 64, target=target, score=-0.5, reason="bad",
        event_id="06" + "c" * 62), int(time.time()))

    r = c.get(f"/api/agents/{target}/trust_received?min_score=0.5")
    d = r.json()
    scores = [v["score"] for v in d["votes"]]
    assert all(s >= 0.5 for s in scores), scores
    assert d["count"] == 1
    assert d["score_sum"] == 0.9


def test_trust_received_window_filter(client):
    """Votes older than `since` seconds must be excluded."""
    c, storage = client
    target = "c" * 64
    now = int(time.time())
    # Recent vote (1 min ago) — should be included
    storage.insert(_stub_kind6(
        voter="4" * 64, target=target, score=1.0, reason="ok",
        event_id="06" + "d" * 62, created_at=now - 60), now - 60)
    # Old vote (30 days ago) — should be excluded when since=7d
    storage.insert(_stub_kind6(
        voter="5" * 64, target=target, score=1.0, reason="stale",
        event_id="06" + "e" * 62, created_at=now - 86400 * 30), now - 86400 * 30)

    r = c.get(f"/api/agents/{target}/trust_received?since={86400 * 7}")
    d = r.json()
    voters = [v["voter"] for v in d["votes"]]
    assert "4" * 64 in voters
    assert "5" * 64 not in voters


def test_trust_received_rejects_bad_agent_id(client):
    """400 on malformed agent_id."""
    c, _ = client
    r = c.get("/api/agents/notahex/trust_received")
    assert r.status_code == 400


def test_trust_received_param_bounds(client):
    """FastAPI Query bounds: since ∈ [60, 90d], min_score ∈ [-1.0, +1.0], limit ∈ [1, 200]."""
    c, _ = client
    target = "0" * 64
    assert c.get(f"/api/agents/{target}/trust_received?since=10").status_code == 422
    assert c.get(f"/api/agents/{target}/trust_received?min_score=2.0").status_code == 422
    assert c.get(f"/api/agents/{target}/trust_received?limit=500").status_code == 422


def test_trust_received_truncates_long_reason(client):
    """`reason` strings must be truncated to 120 chars."""
    c, storage = client
    target = "d" * 64
    long_reason = "x" * 500
    storage.insert(_stub_kind6(
        voter="6" * 64, target=target, score=1.0, reason=long_reason,
        event_id="06" + "f" * 62), int(time.time()))
    r = c.get(f"/api/agents/{target}/trust_received")
    d = r.json()
    assert d["count"] == 1
    assert len(d["votes"][0]["reason"]) == 120


def test_trust_received_skips_malformed_score(client):
    """A kind-6 with a non-numeric score must be silently skipped."""
    c, storage = client
    target = "9" * 64
    storage.insert(Event(
        id="06" + "9" * 62, agent_id="7" * 64, created_at=int(time.time()),
        kind=6, tags=[["p", target]],
        content='{"score":"not-a-number","reason":"junk"}',
        sig="f" * 128,
    ), int(time.time()))
    storage.insert(_stub_kind6(
        voter="8" * 64, target=target, score=0.7, reason="ok",
        event_id="06" + "8" * 62), int(time.time()))
    r = c.get(f"/api/agents/{target}/trust_received")
    assert r.json()["count"] == 1  # only the valid one


def test_home_quick_links_use_relative_base(client, monkeypatch):
    """When ANP2_PUBLIC_BASE_URL is set, quick_links must use it (= no hardcoded anp2.com)."""
    monkeypatch.setenv("ANP2_PUBLIC_BASE_URL", "https://relay-eu.anp2.com")
    storage = Storage(":memory:")
    app = create_app(storage)
    c = TestClient(app)
    r = c.get("/api/home?agent_id=" + "0" * 64)
    d = r.json()
    for url in d["quick_links"].values():
        assert url.startswith("https://relay-eu.anp2.com"), (
            f"quick_link {url!r} didn't honor ANP2_PUBLIC_BASE_URL — "
            "federation-unsafe."
        )
    assert d["latest_announcement"]["url"].startswith("https://relay-eu.anp2.com")
