"""Tests for the B2 capability ontology: structured kind-4 declarations,
/api/capabilities/search filters, sorting, and first-claim conflict
resolution.

See docs/research/CAPABILITY_ONTOLOGY.md for the prose spec and
spec/capabilities/anp2.cap.v1.json for the JSON Schema.
"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from anp2_relay.crypto import compute_event_id, generate_keypair, sign_event_id
from anp2_relay.server import create_app
from anp2_relay.storage import Storage


# ---------- helpers -------------------------------------------------------


def _signed(priv: str, pub: str, kind: int, content: str, tags: list[list[str]], ts: int) -> dict:
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


def _cap_event(
    priv: str,
    pub: str,
    *,
    name: str = "translate.en_es",
    version: str = "1.0",
    p95_latency_ms: int | None = 500,
    price_usd: float = 0.0,
    languages: list[str] | None = None,
    ts: int | None = None,
) -> dict:
    """Build a signed kind 4 event with a single structured capability."""
    meta: dict = {
        "name": name,
        "version": version,
        "input_schema":  {"type": "object"},
        "output_schema": {"type": "object"},
        "constraints": {},
        "pricing": {"currency": "USD", "model": "per_request", "amount": price_usd},
        "policy": {"data_retention": "none", "model_logs_inputs": False},
    }
    if p95_latency_ms is not None:
        meta["constraints"]["p95_latency_ms"] = p95_latency_ms
    if languages is not None:
        meta["constraints"]["supported_languages"] = languages

    content = json.dumps({"capabilities": [meta]}, separators=(",", ":"))
    tags = [["cap", name]]
    return _signed(priv, pub, 4, content, tags, ts or int(time.time()))


# ---------- 1. Storage.capabilities_full parses the new blob -------------


def test_capabilities_full_parses_structured_declaration(tmp_path):
    storage = Storage(tmp_path / "caps.db")
    client = TestClient(create_app(storage))

    priv, pub = generate_keypair()
    ev = _cap_event(priv, pub, name="translate.en_es", p95_latency_ms=500, price_usd=0.0)
    r = client.post("/events", json=ev)
    assert r.status_code == 200, r.text

    results = storage.capabilities_full()
    assert len(results) == 1
    row = results[0]
    assert row["name"] == "translate.en_es"
    assert row["version"] == "1.0"
    assert row["provider_agent_id"] == pub
    assert row["metadata"]["constraints"]["p95_latency_ms"] == 500
    assert row["is_canonical"] is True


def test_capabilities_full_ignores_malformed_kind4_content(tmp_path):
    """A non-JSON kind 4 event must not crash the search; just drop it."""
    storage = Storage(tmp_path / "caps_bad.db")
    client = TestClient(create_app(storage))

    priv, pub = generate_keypair()
    ts = int(time.time())
    # Non-JSON content (JP-redacted) relay accepts (signature is valid) but parser must skip.
    bad = _signed(priv, pub, 4, "not-json-at-all", [["cap", "x.broken"]], ts)
    assert client.post("/events", json=bad).status_code == 200

    # Good event from another agent.
    priv2, pub2 = generate_keypair()
    good = _cap_event(priv2, pub2, name="x.good", ts=ts + 1)
    assert client.post("/events", json=good).status_code == 200

    results = storage.capabilities_full()
    names = {r["name"] for r in results}
    assert "x.good" in names
    assert "x.broken" not in names


# ---------- 2. /api/capabilities/search filters --------------------------


def test_search_endpoint_returns_envelope(tmp_path):
    storage = Storage(tmp_path / "search.db")
    client = TestClient(create_app(storage))

    priv, pub = generate_keypair()
    client.post("/events", json=_cap_event(priv, pub))

    r = client.get("/api/capabilities/search")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"query", "results", "count"}
    assert body["count"] == 1


def test_search_filter_by_max_latency(tmp_path):
    storage = Storage(tmp_path / "lat.db")
    client = TestClient(create_app(storage))

    priv_fast, pub_fast = generate_keypair()
    priv_slow, pub_slow = generate_keypair()
    ts = int(time.time())
    # Two different capability names so they don't conflict.
    client.post("/events", json=_cap_event(priv_fast, pub_fast, name="x.fast", p95_latency_ms=100, ts=ts))
    client.post("/events", json=_cap_event(priv_slow, pub_slow, name="x.slow", p95_latency_ms=5000, ts=ts + 1))

    r = client.get("/api/capabilities/search", params={"max_latency_ms": 1000})
    assert r.status_code == 200
    names = {row["name"] for row in r.json()["results"]}
    assert names == {"x.fast"}


def test_search_filter_by_max_price(tmp_path):
    storage = Storage(tmp_path / "price.db")
    client = TestClient(create_app(storage))

    priv_cheap, pub_cheap = generate_keypair()
    priv_pricey, pub_pricey = generate_keypair()
    ts = int(time.time())
    client.post("/events", json=_cap_event(priv_cheap, pub_cheap, name="x.cheap", price_usd=0.0001, ts=ts))
    client.post("/events", json=_cap_event(priv_pricey, pub_pricey, name="x.pricey", price_usd=10.0, ts=ts + 1))

    r = client.get("/api/capabilities/search", params={"max_price_usd": 0.01})
    names = {row["name"] for row in r.json()["results"]}
    assert names == {"x.cheap"}


def test_search_filter_by_supported_language(tmp_path):
    storage = Storage(tmp_path / "lang.db")
    client = TestClient(create_app(storage))

    priv_ja, pub_ja = generate_keypair()
    priv_en, pub_en = generate_keypair()
    ts = int(time.time())
    client.post(
        "/events",
        json=_cap_event(priv_ja, pub_ja, name="x.ja_only", languages=["ja"], ts=ts),
    )
    client.post(
        "/events",
        json=_cap_event(priv_en, pub_en, name="x.en_only", languages=["en"], ts=ts + 1),
    )

    r = client.get("/api/capabilities/search", params={"supported_language": "ja"})
    names = {row["name"] for row in r.json()["results"]}
    assert names == {"x.ja_only"}


def test_search_filter_by_min_trust(tmp_path):
    """min_trust=very-high should exclude an agent with no incoming votes."""
    storage = Storage(tmp_path / "trust_filter.db")
    client = TestClient(create_app(storage))

    priv, pub = generate_keypair()
    client.post("/events", json=_cap_event(priv, pub, name="x.any"))

    # No trust votes posted; agent's trust = 0. Requesting min_trust=1.0
    # should yield an empty list.
    r = client.get("/api/capabilities/search", params={"min_trust": 1.0})
    assert r.json()["count"] == 0

    # Without the filter, the cap is visible.
    r2 = client.get("/api/capabilities/search")
    assert r2.json()["count"] == 1


# ---------- 3. Hierarchical prefix matching ------------------------------


def test_search_hierarchical_prefix(tmp_path):
    """cap=vision.ocr matches vision.ocr.document.japanese but not vision.classify.*."""
    storage = Storage(tmp_path / "prefix.db")
    client = TestClient(create_app(storage))

    priv_ocr, pub_ocr = generate_keypair()
    priv_cls, pub_cls = generate_keypair()
    ts = int(time.time())
    client.post(
        "/events",
        json=_cap_event(priv_ocr, pub_ocr, name="vision.ocr.document.japanese", ts=ts),
    )
    client.post(
        "/events",
        json=_cap_event(priv_cls, pub_cls, name="vision.classify.scene", ts=ts + 1),
    )

    r = client.get("/api/capabilities/search", params={"cap": "vision.ocr"})
    names = {row["name"] for row in r.json()["results"]}
    assert names == {"vision.ocr.document.japanese"}


# ---------- 4. Sorting ----------------------------------------------------


def test_sort_by_latency_ascending(tmp_path):
    storage = Storage(tmp_path / "sort_lat.db")
    client = TestClient(create_app(storage))

    pairs = [
        ("x.a", 900),
        ("x.b", 100),
        ("x.c", 400),
    ]
    ts = int(time.time())
    for i, (name, lat) in enumerate(pairs):
        priv, pub = generate_keypair()
        client.post(
            "/events",
            json=_cap_event(priv, pub, name=name, p95_latency_ms=lat, ts=ts + i),
        )

    r = client.get("/api/capabilities/search", params={"sort_by": "latency"})
    results = r.json()["results"]
    latencies = [row["metadata"]["constraints"]["p95_latency_ms"] for row in results]
    assert latencies == sorted(latencies)


def test_sort_by_price_ascending(tmp_path):
    storage = Storage(tmp_path / "sort_price.db")
    client = TestClient(create_app(storage))

    pairs = [
        ("x.a", 0.10),
        ("x.b", 0.001),
        ("x.c", 0.05),
    ]
    ts = int(time.time())
    for i, (name, price) in enumerate(pairs):
        priv, pub = generate_keypair()
        client.post(
            "/events",
            json=_cap_event(priv, pub, name=name, price_usd=price, ts=ts + i),
        )

    r = client.get("/api/capabilities/search", params={"sort_by": "price"})
    results = r.json()["results"]
    prices = [row["metadata"]["pricing"]["amount"] for row in results]
    assert prices == sorted(prices)


# ---------- 5. First-claim conflict resolution ---------------------------


def test_first_claim_wins_canonical_flag(tmp_path):
    """Two agents both declare `x.share`; the earlier one is canonical."""
    storage = Storage(tmp_path / "conflict.db")
    client = TestClient(create_app(storage))

    priv1, pub1 = generate_keypair()
    priv2, pub2 = generate_keypair()
    ts = int(time.time())
    # pub1 declares first (earlier ts), pub2 later.
    client.post("/events", json=_cap_event(priv1, pub1, name="x.share", ts=ts))
    client.post("/events", json=_cap_event(priv2, pub2, name="x.share", ts=ts + 10))

    # Default: only the canonical (first-claim) result returned.
    r = client.get("/api/capabilities/search", params={"cap": "x.share"})
    body = r.json()
    assert body["count"] == 1
    assert body["results"][0]["provider_agent_id"] == pub1
    assert body["results"][0]["is_canonical"] is True

    # include_conflicts=true returns both; only pub1 is canonical.
    r2 = client.get(
        "/api/capabilities/search",
        params={"cap": "x.share", "include_conflicts": "true"},
    )
    body2 = r2.json()
    assert body2["count"] == 2
    canon = [row for row in body2["results"] if row["is_canonical"]]
    assert len(canon) == 1
    assert canon[0]["provider_agent_id"] == pub1


def test_version_conflict_resolution_across_majors(tmp_path):
    """Two providers, same name, different MAJOR versions => still subject to
    first-claim canonicality at the name level. The metadata.version field is
    preserved so clients can disambiguate.
    """
    storage = Storage(tmp_path / "vconflict.db")
    client = TestClient(create_app(storage))

    priv1, pub1 = generate_keypair()
    priv2, pub2 = generate_keypair()
    ts = int(time.time())
    client.post(
        "/events",
        json=_cap_event(priv1, pub1, name="x.api", version="1.0", ts=ts),
    )
    client.post(
        "/events",
        json=_cap_event(priv2, pub2, name="x.api", version="2.0", ts=ts + 5),
    )

    r = client.get(
        "/api/capabilities/search",
        params={"cap": "x.api", "include_conflicts": "true"},
    )
    body = r.json()
    versions = {row["version"]: row["is_canonical"] for row in body["results"]}
    assert versions == {"1.0": True, "2.0": False}


# ---------- 6. Overwrite semantics (latest kind 4 per agent) -------------


def test_overwrite_kind4_uses_latest_per_agent(tmp_path):
    """If the same agent posts two kind 4 events, the latest wins (PROTOCOL (JP-redacted)4.5)."""
    storage = Storage(tmp_path / "overwrite.db")
    client = TestClient(create_app(storage))

    priv, pub = generate_keypair()
    ts = int(time.time())
    client.post(
        "/events",
        json=_cap_event(priv, pub, name="x.old", p95_latency_ms=999, ts=ts),
    )
    client.post(
        "/events",
        json=_cap_event(priv, pub, name="x.new", p95_latency_ms=10, ts=ts + 5),
    )

    results = storage.capabilities_full()
    names = {r["name"] for r in results}
    # latest kind 4 declares only x.new; x.old is overwritten.
    assert names == {"x.new"}
