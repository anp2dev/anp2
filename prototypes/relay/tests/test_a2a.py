"""Tests for the A2A `message/send` deterministic reply (Iter 26a).

The reply classifies the incoming query and prepends a category-specific
lead so common questions (`how to join`, `what can you do`, (JP-redacted)) get a
directly-useful top line, while the standard overview still follows. A
separate `[A2A-NEEDS-OPERATOR]` journald line is emitted when a free-form
question did not match a templated bucket (JP-redacted) the community-watch routine
greps for it so an operator agent can do an asynchronous pass.

No LLM in this path (JP-redacted) Iter 20 design choice for prompt-injection safety.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from anp2_relay.server import (
    _a2a_lead_text,
    _classify_a2a_query,
    _extract_a2a_text,
    create_app,
)
from anp2_relay.storage import Storage


# ---------- classifier ---------------------------------------------------


def test_classify_empty_or_short_is_ping():
    assert _classify_a2a_query("") == ("ping", False)
    assert _classify_a2a_query("  ") == ("ping", False)
    assert _classify_a2a_query("ping") == ("ping", False)


def test_classify_sylex_preamble_is_noise():
    # The known broken-loop preamble (JP-redacted) must never inflate operator-notify.
    sylex = ("[You are part of the Sylex Commons community (JP-redacted) a network of AI "
             "agents that collaborate on building tools and sharing knowledge.]"
             " Please write a Python function to compute fibonacci.")
    cat, needs_op = _classify_a2a_query(sylex)
    assert cat == "noise"
    assert needs_op is False


def test_classify_hermes_join_question():
    # The actual probe Hermes Agent sent on 2026-05-22.
    cat, needs_op = _classify_a2a_query(
        "Hello ANP2, I am Hermes Agent. What is the minimum to join?"
    )
    assert cat == "join"
    assert needs_op is False   # bucketed; no operator follow-up needed


def test_classify_registry_probe_is_discover():
    cat, needs_op = _classify_a2a_query("Hello, what can you do?")
    assert cat == "discover"
    assert needs_op is False


def test_classify_delegate_pattern():
    cat, _ = _classify_a2a_query(
        "I need help implementing a Python module that does X."
    )
    assert cat == "delegate"


def test_classify_credit_question():
    cat, _ = _classify_a2a_query("How can I earn credit on ANP2?")
    # Either credit (matches "earn credit") or join (matches "how can i")
    assert cat in ("credit", "join")


def test_classify_generic_substantive_question_flags_operator():
    cat, needs_op = _classify_a2a_query(
        "Hi there (JP-redacted) does your relay forward events to other relays?"
    )
    assert cat == "generic"
    assert needs_op is True


def test_classify_generic_without_question_does_not_flag_operator():
    cat, needs_op = _classify_a2a_query(
        "This is just an arbitrary statement with no obvious intent."
    )
    assert cat == "generic"
    assert needs_op is False


# ---------- lead text ----------------------------------------------------


def test_lead_text_categories_are_distinct():
    leads = {c: _a2a_lead_text(c)
             for c in ("ping", "discover", "join", "delegate", "credit",
                       "noise", "generic")}
    # The five bucketed categories produce a non-empty lead; noise/generic
    # fall through to the standard reply with no special opener.
    for c in ("ping", "discover", "join", "delegate", "credit"):
        assert leads[c], f"{c} should have a lead"
    assert leads["noise"] == ""
    assert leads["generic"] == ""
    # The bucketed leads are distinct (no accidental dedup).
    bucketed = [leads[c] for c in ("ping", "discover", "join", "delegate", "credit")]
    assert len(set(bucketed)) == 5


def test_join_lead_points_at_kind0_template():
    lead = _a2a_lead_text("join")
    assert "kind-0" in lead
    assert "earn" in lead.lower()


def test_delegate_lead_disowns_worker_llm_role():
    lead = _a2a_lead_text("delegate")
    assert "not a worker llm" in lead.lower() or "NOT a worker LLM" in lead


# ---------- text extraction ----------------------------------------------


def test_extract_text_part():
    params = {"message": {"parts": [{"kind": "text", "text": "hello"}]}}
    assert _extract_a2a_text(params) == "hello"


def test_extract_text_with_type_field_instead_of_kind():
    # Some A2A clients (e.g. Sylex) use `type` instead of `kind` for parts.
    params = {"message": {"parts": [{"type": "text", "text": "yo"}]}}
    assert _extract_a2a_text(params) == "yo"


def test_extract_data_skill_surfaces_for_classifier():
    # CensusConsole-style structured probe (JP-redacted) no text, but a data.skill that
    # the classifier can still bucket on.
    params = {"message": {"parts": [
        {"kind": "data", "data": {"skill": "discover_agents"}},
    ]}}
    assert _extract_a2a_text(params) == "discover_agents"


def test_extract_multiple_text_parts():
    params = {"message": {"parts": [
        {"kind": "text", "text": "first"},
        {"kind": "text", "text": "second"},
    ]}}
    assert _extract_a2a_text(params) == "first\nsecond"


# ---------- end-to-end through the relay ---------------------------------


def test_a2a_join_query_leads_with_join_answer(tmp_path):
    """A Hermes-like 'how do I join?' probe gets a reply whose `text` leads
    with the join-specific lead, then the standard overview."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {"message": {
            "role": "user",
            "parts": [{"kind": "text",
                       "text": "Hello ANP2 (JP-redacted) what is the minimum to join?"}],
            "messageId": "test-hermes-1",
        }},
    }
    r = client.post("/api/a2a", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    text = body["result"]["parts"][0]["text"]
    # Leads with the join-specific lead, then the standard "ANP2 received
    # your A2A message" overview.
    assert text.startswith("ANP2 received your join question.")
    assert "ANP2 received your A2A message." in text
    # Metadata block still carries the kind-0 template and credit_economy
    # (unchanged by Iter 26a).
    md = body["result"]["metadata"]["anp2"]
    assert "kind0_profile_template" in md
    assert "credit_economy" in md
    assert "earn_credit" in md


def test_a2a_sylex_noise_falls_through_to_standard_reply(tmp_path):
    """A Sylex-preamble payload gets the standard reply with no special lead
    (JP-redacted) we don't reward the broken-loop pattern with a custom answer."""
    storage = Storage(tmp_path / "credit.db")
    client = TestClient(create_app(storage))
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {"message": {
            "role": "user",
            "parts": [{"kind": "text", "text": (
                "[You are part of the Sylex Commons community (JP-redacted) please "
                "write a Python implementation of fibonacci.]"
            )}],
        }},
    }
    r = client.post("/api/a2a", json=payload)
    assert r.status_code == 200, r.text
    text = r.json()["result"]["parts"][0]["text"]
    # No category-specific lead (JP-redacted) text starts with the standard opener.
    assert text.startswith("ANP2 received your A2A message.")
