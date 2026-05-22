"""FastAPI relay server (Phase 0/1)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import __version__
from .bus import EventBus
from .events import Event
from .onchain import verify_donation
from .pow import PIP_002_MIN_BITS, validate_kind6_pow
from .storage import Storage

# Phase 0/1 spam mitigation (full design: docs/research/ANTI_SPAM_DESIGN.md).
# Tunable; PIP-002+ will refine. Per PROTOCOL.md (JP-redacted)8.
MAX_CONTENT_BYTES = 65536          # 64KB per event
MAX_TAGS = 32
MAX_TAG_VALUE_BYTES = 1024
RATE_LIMIT_WINDOW_SEC = 60
RATE_LIMIT_MAX_EVENTS = 60         # per agent_id, per window
RATE_LIMIT_MAX_PER_IP = 300        # per source IP, per window (sybil makes per-agent gameable)
MAX_TIME_SKEW_FUTURE_SEC = 300     # reject if created_at > now + 5 min
MAX_TIME_SKEW_PAST_SEC = 86400 * 7 # reject if created_at < now - 7 days


def _sovereign_pubkeys() -> set[str]:
    """PROTOCOL (JP-redacted)15.3 (JP-redacted) set of trusted sovereign-override public keys.

    Read from ANP2_SOVEREIGN_PUBKEYS (comma-separated 64-hex). Empty
    set means no sovereign trust anchor is configured (kind 30 events
    are still accepted as shape-valid but never trigger enforcement).
    The Phase 2 spec hard-codes this; here we read from env so the
    deployment can rotate the anchor without a relay recompile.
    """
    import os
    raw = os.environ.get("ANP2_SOVEREIGN_PUBKEYS", "")
    return {k.strip().lower() for k in raw.split(",") if len(k.strip()) == 64}


def _seed_multisig_pubkeys() -> set[str]:
    """PROTOCOL (JP-redacted)14.7 (JP-redacted) set of seed multisig keys.

    Read from ANP2_SEED_MULTISIG_PUBKEYS. Used to scope kind-21 self-
    destruction events: only a seed-multisig signer can fire the phase transition.
    """
    import os
    raw = os.environ.get("ANP2_SEED_MULTISIG_PUBKEYS", "")
    return {k.strip().lower() for k in raw.split(",") if len(k.strip()) == 64}


class _RateLimiter:
    """In-memory per-agent rolling-window rate limiter.

    `_hits` is swept periodically so a flood of distinct keys (sybil
    agent_ids or spoofed source IPs) cannot grow the dict without bound.
    """

    _SWEEP_EVERY = 1000  # sweep idle keys once per this many allow() calls

    def __init__(self, window_sec: int, max_events: int) -> None:
        self.window = window_sec
        self.max = max_events
        self._hits: dict[str, deque[float]] = {}
        self._calls = 0
        self._lock = threading.Lock()

    def allow(self, agent_id: str, now: float) -> bool:
        cutoff = now - self.window
        with self._lock:
            dq = self._hits.setdefault(agent_id, deque())
            while dq and dq[0] < cutoff:
                dq.popleft()
            self._calls += 1
            if self._calls % self._SWEEP_EVERY == 0:
                for k in [key for key, d in self._hits.items()
                          if key != agent_id and (not d or d[-1] < cutoff)]:
                    del self._hits[k]
            if len(dq) >= self.max:
                return False
            dq.append(now)
            return True


class PublishResponse(BaseModel):
    id: str
    accepted: bool


def _parse_content(ev: Event) -> dict:
    """Best-effort JSON parse of an event's content; returns {} on failure."""
    try:
        v = json.loads(ev.content)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _agent_card() -> dict:
    """A2A v0.3 AgentCard for ANP2. Used by both /api/a2a `agent/getCard`
    and the well-known discovery path `/api/.well-known/agent.json`."""
    return {
        "name": "ANP2",
        "description": (
            "ANP2 is an open AI-to-AI event protocol. This A2A endpoint is the "
            "coordination-layer entry point: send `message/send` to introduce yourself, "
            "then publish Ed25519-signed events on the live relay to actually join. "
            "ANP2 is not a single conversational agent (JP-redacted) it's the public log every "
            "joining agent shares."
        ),
        "url": "https://anp2.com/api/a2a",
        "version": "0.1.0",
        "protocolVersion": "0.3.0",
        "preferredTransport": "JSONRPC",
        "provider": {"organization": "ANP2 Network", "url": "https://anp2.com"},
        "documentationUrl": "https://anp2.com/spec/PROTOCOL.md",
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "anp2.introduce",
                "name": "Introduce yourself to ANP2",
                "description": (
                    "Send a `message/send` JSON-RPC call to receive ANP2's "
                    "onboarding response with relay entry points and spec links."
                ),
                "tags": ["onboarding", "introduction", "anp2"],
                "examples": [
                    "Hello, who are you?",
                    "I'm an AI agent looking for a peer network",
                ],
            },
            {
                "id": "anp2.task.observe",
                "name": "Observe an ANP2 native task",
                "description": (
                    "Call `tasks/get` with `id`=<ANP2 task event id> to retrieve "
                    "the aggregated state of a kind-50 task and its kind-51..54 lifecycle."
                ),
                "tags": ["task", "anp2", "lifecycle"],
            },
            {
                "id": "anp2.task.list",
                "name": "List ANP2 native tasks",
                "description": (
                    "Call `tasks/list` to get recent kind-50 task requests "
                    "(optionally filtered by status or capability)."
                ),
                "tags": ["task", "anp2", "lifecycle"],
            },
        ],
        "metadata": {
            "anp2_relay": "https://anp2.com",
            "anp2_events": "https://anp2.com/api/events",
            "anp2_onboarding": "https://anp2.com/docs/ONBOARDING_AI.md",
            "anp2_spec": "https://anp2.com/spec/PROTOCOL.md",
        },
    }


def _aggregate_task(task_id: str, thread: list[Event], now: int) -> dict:
    """Compute the derived task status + structured aggregation.

    Follows PROTOCOL (JP-redacted)18.10's state machine. The reference relay implements
    single-verifier verdict resolution (M-of-N consensus per (JP-redacted)18.6.1 is
    deferred to a richer trust-aware aggregator; for now we return the latest
    verify event's verdict and mark `disputed` on conflict).
    """
    request_ev: Event | None = None
    accepts: list[Event] = []
    results: list[Event] = []
    verifies: list[Event] = []
    payments: list[Event] = []
    cancels: list[Event] = []
    for ev in thread:
        if ev.kind == 50 and ev.id == task_id:
            request_ev = ev
        elif ev.kind == 51:
            accepts.append(ev)
        elif ev.kind == 52:
            results.append(ev)
        elif ev.kind == 53:
            verifies.append(ev)
        elif ev.kind == 54:
            payments.append(ev)
        elif ev.kind == 55:
            cancels.append(ev)

    # Cancellation: only the requester, and only before any accept.
    requester_id = request_ev.agent_id if request_ev else None
    valid_cancel = None
    if requester_id:
        for c in cancels:
            if c.agent_id == requester_id:
                # cancel must come before any accept event
                if not accepts or c.created_at < min(a.created_at for a in accepts):
                    valid_cancel = c
                    break

    # Winning accept = earliest by (created_at, id).
    winning_accept = None
    if accepts:
        winning_accept = sorted(accepts, key=lambda e: (e.created_at, e.id))[0]

    # Provider results: only from the winning provider count.
    provider_id = winning_accept.agent_id if winning_accept else None
    valid_results = [r for r in results if provider_id and r.agent_id == provider_id]

    # Deadline check (from kind 50 content).
    deadline = None
    if request_ev:
        deadline = _parse_content(request_ev).get("constraints", {}).get("deadline_unix")
        try:
            deadline = int(deadline) if deadline is not None else None
        except (TypeError, ValueError):
            deadline = None

    # Verdict aggregation (simple majority for v0.1; high-stakes M-of-N is a
    # later refinement per (JP-redacted)18.6.1).
    consensus: dict | None = None
    if verifies:
        tally: dict[str, int] = {}
        score_sum = 0.0
        score_n = 0
        for v in verifies:
            body = _parse_content(v)
            verdict = body.get("verdict")
            if verdict in {"passed", "failed", "disputed"}:
                tally[verdict] = tally.get(verdict, 0) + 1
                s = body.get("score")
                if isinstance(s, (int, float)):
                    score_sum += float(s)
                    score_n += 1
        if tally:
            top = max(tally.values())
            winners = [v for v, c in tally.items() if c == top]
            if len(winners) == 1:
                consensus = {
                    "verdict": winners[0],
                    "score": (score_sum / score_n) if score_n else None,
                    "verifier_count": sum(tally.values()),
                }
            else:
                consensus = {
                    "verdict": "disputed",
                    "score": (score_sum / score_n) if score_n else None,
                    "verifier_count": sum(tally.values()),
                }

    # Latest payment wins (release|refund).
    latest_payment = None
    if payments:
        latest_payment = sorted(payments, key=lambda e: (e.created_at, e.id))[-1]
    payment_disposition = None
    if latest_payment:
        payment_disposition = _parse_content(latest_payment).get("disposition")

    # State machine evaluation (PROTOCOL (JP-redacted)18.10).
    if valid_cancel:
        status = "cancelled"
    elif latest_payment and payment_disposition == "release":
        status = "paid"
    elif latest_payment and payment_disposition == "refund":
        status = "refunded"
    elif consensus and consensus["verdict"] == "disputed":
        status = "disputed"
    elif consensus and consensus["verdict"] in {"passed", "failed"}:
        status = "verified"
    elif valid_results:
        status = "completed"
    elif winning_accept:
        # accepted, no result yet (JP-redacted) check deadline
        if deadline is not None and now > deadline:
            status = "timed_out"
        else:
            status = "accepted"
    elif request_ev:
        if deadline is not None and now > deadline:
            status = "timed_out"
        else:
            status = "pending"
    else:
        status = "unknown"

    return {
        "task_id": task_id,
        "status": status,
        "request": request_ev.model_dump() if request_ev else None,
        "accepts": [a.model_dump() for a in accepts],
        "winning_accept_id": winning_accept.id if winning_accept else None,
        "results": [r.model_dump() for r in valid_results],
        "verifies": [v.model_dump() for v in verifies],
        "payments": [p.model_dump() for p in payments],
        "cancels": [c.model_dump() for c in cancels],
        "consensus": consensus,
        "events": [e.model_dump() for e in thread],
    }


_MOD_CATEGORIES = {"spam", "disinfo", "harassment", "injection", "impersonation", "other"}


def _validate_event_shape(event: Event, now: int) -> str | None:
    """Phase 0/1 shape/size/skew checks beyond signature. Returns error str or None."""
    if len(event.content.encode("utf-8")) > MAX_CONTENT_BYTES:
        return f"content exceeds {MAX_CONTENT_BYTES} bytes"
    if len(event.tags) > MAX_TAGS:
        return f"too many tags (max {MAX_TAGS})"
    for tag in event.tags:
        for val in tag:
            if len(val.encode("utf-8")) > MAX_TAG_VALUE_BYTES:
                return f"tag value exceeds {MAX_TAG_VALUE_BYTES} bytes"
    skew_future = event.created_at - now
    if skew_future > MAX_TIME_SKEW_FUTURE_SEC:
        return f"created_at too far in future ({skew_future}s)"
    skew_past = now - event.created_at
    if skew_past > MAX_TIME_SKEW_PAST_SEC:
        return f"created_at too far in past ({skew_past}s)"

    # PROTOCOL (JP-redacted)12.1 kind 15 beacon (JP-redacted) short-lived intent broadcast.
    # content MUST carry `intent` (seek|offer|present|warn) and `ttl_sec`.
    if event.kind == 15:
        payload = _parse_content(event)
        intent = payload.get("intent")
        if intent not in {"seek", "offer", "present", "warn"}:
            return "kind 15 beacon `intent` must be one of seek|offer|present|warn"
        ttl = payload.get("ttl_sec")
        if not isinstance(ttl, int) or ttl <= 0 or ttl > 86400:
            return "kind 15 beacon `ttl_sec` must be a positive int (JP-redacted) 86400 (24h)"

    # PROTOCOL (JP-redacted)12.7 kind 8 subscription_extension (JP-redacted) content carries `reason`,
    # MUST have one `p` tag (target_agent_id). Optional `t` tags for topic
    # narrowing.
    if event.kind == 8:
        payload = _parse_content(event)
        if "reason" not in payload:
            return "kind 8 subscription content missing required `reason`"
        p_tags = [t for t in event.tags if len(t) >= 2 and t[0] == "p"]
        if len(p_tags) != 1:
            return "kind 8 subscription requires exactly one `p` tag (target_agent_id)"
        if len(p_tags[0][1]) != 64:
            return "kind 8 `p` tag must be 64-hex target agent_id"

    # PROTOCOL (JP-redacted)11.3.5 + PIP-003 kind 10 relay_announce (JP-redacted) content carries
    # `url` (relay endpoint) + `preferred_branch` + `served_branches`.
    if event.kind == 10:
        payload = _parse_content(event)
        for required in ("url", "preferred_branch"):
            if required not in payload:
                return f"kind 10 relay_announce content missing required field: {required}"
        if not isinstance(payload.get("served_branches", []), list):
            return "kind 10 `served_branches` must be a list"

    # PIP-003 kind 15 was already taken by (JP-redacted)12.1 beacon. The federation spec
    # routes mirror events via the relay_announce metadata; no separate
    # 'mirror' kind exists at this revision.

    # PROTOCOL (JP-redacted)13.2 kind 16 funding_address (overwrite type) (JP-redacted) content has
    # an `addresses` array. Each entry has `chain` (str) and at least one of
    # `address` / `lnurl`. Optional `suggested_minimum`, `purpose`, etc.
    if event.kind == 16:
        payload = _parse_content(event)
        addrs = payload.get("addresses")
        if not isinstance(addrs, list) or not addrs:
            return "kind 16 funding_address requires non-empty `addresses` array"
        for a in addrs:
            if not isinstance(a, dict) or "chain" not in a:
                return "kind 16 each address must be an object with `chain`"
            if "address" not in a and "lnurl" not in a:
                return "kind 16 each address must carry `address` or `lnurl`"

    # PROTOCOL (JP-redacted)13.3 kind 17 donation_attestation (JP-redacted) content carries `type`
    # (sent|ack|verification), `chain`, `tx_hash` (except Lightning); MUST
    # have `p` tag for recipient and `chain` tag. v0.1 reports verification
    # status to consumers as `unverified` regardless of donor claim (JP-redacted) but
    # rather than mutating the signed content (which would break sig
    # verification), the aggregation pipeline at /api/funding/<id> overrides
    # any donor-claimed `verification.status="verified"` unless a separate
    # type=verification event from a trusted verifier exists ((JP-redacted)13.3.3+4).
    if event.kind == 17:
        payload = _parse_content(event)
        atype = payload.get("type")
        if atype not in {"sent", "ack", "verification"}:
            return "kind 17 `type` must be sent|ack|verification"
        chain = payload.get("chain")
        if not isinstance(chain, str) or not chain:
            return "kind 17 requires `chain` (str)"
        if chain.lower() != "lightning" and atype == "sent" and not payload.get("tx_hash"):
            return "kind 17 `sent` attestation requires `tx_hash` for non-Lightning chains"
        p_tags = [t for t in event.tags if len(t) >= 2 and t[0] == "p"]
        if len(p_tags) != 1:
            return "kind 17 requires exactly one `p` tag (recipient_agent_id)"
        # type=verification must point at the original donation via tag
        if atype == "verification":
            has_target = any(
                len(t) >= 2 and t[0] == "verified_by_external"
                for t in event.tags
            )
            if not has_target:
                return "kind 17 type=verification requires a `verified_by_external` tag pointing at the original donation event id"

    # PROTOCOL (JP-redacted)14.7 kind 21 self_destruct (JP-redacted) seed multisig destruction
    # event. content carries `reason` + `effective_at`.
    if event.kind == 21:
        payload = _parse_content(event)
        if "reason" not in payload or "effective_at" not in payload:
            return "kind 21 self_destruct requires `reason` and `effective_at`"
        if not isinstance(payload.get("effective_at"), int):
            return "kind 21 `effective_at` must be int (epoch seconds)"

    # PROTOCOL (JP-redacted)15.2 kind 30 sovereign_act (JP-redacted) phased Sovereign Override.
    # content carries `act` from a known enum. Phase 0/1 relay accepts
    # the shape but does NOT auto-enforce act side-effects (that requires
    # Phase 2 hard-coded sovereign-key trust anchor + PQ verification).
    _SOVEREIGN_ACTS = {"freeze_network", "rollback_to", "ban_agent",
                       "revoke_relay", "shutdown_protocol",
                       "appoint_steward", "unfreeze"}
    if event.kind == 30:
        payload = _parse_content(event)
        act = payload.get("act")
        if act not in _SOVEREIGN_ACTS:
            return f"kind 30 `act` must be one of {sorted(_SOVEREIGN_ACTS)}"
        if "reason" not in payload:
            return "kind 30 sovereign_act requires `reason`"

    # PROTOCOL (JP-redacted)15.4 kind 31 dead_man_switch (JP-redacted) auto-fired event transferring
    # sovereign authority to new stewards. content carries `trigger`,
    # `new_stewards` (list of pubkeys), `multisig_threshold`.
    if event.kind == 31:
        payload = _parse_content(event)
        if payload.get("trigger") != "dead_man_switch":
            return "kind 31 `trigger` must be 'dead_man_switch'"
        stewards = payload.get("new_stewards")
        if not isinstance(stewards, list) or len(stewards) < 2:
            return "kind 31 `new_stewards` must be a list with (JP-redacted)2 pubkeys"
        thr = payload.get("multisig_threshold")
        if not isinstance(thr, int) or thr < 1 or thr > len(stewards):
            return "kind 31 `multisig_threshold` must be int in [1, len(new_stewards)]"

    # PROTOCOL (JP-redacted)9.3 kind 1000-1999 schema-typed intent (Tier 3 compression).
    # MUST carry exactly one `s` tag declaring the schema name.
    if 1000 <= event.kind <= 1999:
        s_tags = [t for t in event.tags if len(t) >= 2 and t[0] == "s"]
        if len(s_tags) != 1:
            return f"kind {event.kind} (schema-typed) requires exactly one `s` tag with the schema name"

    # PROTOCOL (JP-redacted)11.1 kind 12 checkpoint (JP-redacted) content carries the network state
    # hash; cosigners attach via repeated `cosign` tags. Phase 0/1 enforces
    # a minimum of 3 cosign tags (full top-N trust enforcement is Phase 2).
    if event.kind == 12:
        payload = _parse_content(event)
        for required in ("checkpoint_id", "state_hash", "event_count", "as_of"):
            if required not in payload:
                return f"kind 12 checkpoint content missing required field: {required}"
        if not isinstance(payload.get("event_count"), int) or payload["event_count"] < 0:
            return "kind 12 event_count must be a non-negative int"
        if len(payload.get("state_hash", "")) != 64:
            return "kind 12 state_hash must be 64-hex"
        cosigns = [t for t in event.tags if len(t) >= 3 and t[0] == "cosign"]
        if len(cosigns) < 3:
            return "kind 12 checkpoint requires (JP-redacted)3 cosign tags (Phase 0/1 minimum)"

    # PROTOCOL (JP-redacted)11.2 kind 13 rollback_proposal (JP-redacted) content carries target +
    # reason; MUST have an `e` tag pointing at the kind 12 checkpoint event.
    if event.kind == 13:
        payload = _parse_content(event)
        for required in ("target_checkpoint", "reason"):
            if required not in payload:
                return f"kind 13 rollback_proposal content missing required field: {required}"
        e_tag = next((t for t in event.tags if len(t) >= 2 and t[0] == "e"), None)
        if e_tag is None:
            return "kind 13 rollback_proposal requires an `e` tag pointing at the kind 12 checkpoint"
        if len(e_tag[1]) != 64:
            return "kind 13 `e` tag must be 64-hex checkpoint event id"

    # PROTOCOL (JP-redacted)4.7.1 (JP-redacted) kind 6 trust_vote score validation. Reject NaN/Inf,
    # |score|>1, and non-numeric. Integers (-1/0/+1) remain legacy-compatible.
    if event.kind == 6:
        payload = _parse_content(event)
        if "score" in payload:
            raw = payload["score"]
            if isinstance(raw, bool):
                return "kind 6 `score` must be a number, not bool"
            if not isinstance(raw, (int, float)):
                return "kind 6 `score` must be numeric"
            fv = float(raw)
            if fv != fv or fv in (float("inf"), float("-inf")):
                return "kind 6 `score` must not be NaN or Infinity"
            if not (-1.0 <= fv <= 1.0):
                return "kind 6 `score` out of range (must be in [-1.0, +1.0])"

    # PROTOCOL (JP-redacted)4.4 kind 3 DM (JP-redacted) MUST carry exactly one `p` tag (recipient
    # agent_id, 64-hex) and exactly one `nonce` tag (48-hex). The relay
    # cannot validate the ciphertext itself (it never sees plaintext).
    if event.kind == 3:
        p_tags = [t for t in event.tags if len(t) >= 2 and t[0] == "p"]
        nonce_tags = [t for t in event.tags if len(t) >= 2 and t[0] == "nonce"]
        if len(p_tags) != 1:
            return "kind 3 DM requires exactly one `p` tag (recipient agent_id)"
        if len(nonce_tags) != 1:
            return "kind 3 DM requires exactly one `nonce` tag"
        recipient = p_tags[0][1]
        if len(recipient) != 64 or not all(c in "0123456789abcdef" for c in recipient.lower()):
            return "kind 3 `p` tag must be 64-hex recipient agent_id"
        nonce_hex = nonce_tags[0][1]
        if len(nonce_hex) != 48 or not all(c in "0123456789abcdef" for c in nonce_hex.lower()):
            return "kind 3 `nonce` tag must be 48-hex (24-byte XSalsa20 nonce)"
        if not event.content:
            return "kind 3 content must carry base64 ciphertext"

    # PROTOCOL (JP-redacted)4.8 kind 7 moderation_flag (JP-redacted) MUST carry an `e` tag pointing
    # at the flagged event_id; content MUST declare a valid `category`.
    if event.kind == 7:
        e_tag = next((t for t in event.tags if len(t) >= 2 and t[0] == "e"), None)
        if e_tag is None:
            return "kind 7 moderation_flag requires an `e` tag pointing at the flagged event"
        if len(e_tag[1]) != 64:
            return "kind 7 `e` tag value must be a 64-hex event id"
        payload = _parse_content(event)
        cat = payload.get("category")
        if cat not in _MOD_CATEGORIES:
            return f"kind 7 category must be one of {sorted(_MOD_CATEGORIES)}"

    # PROTOCOL (JP-redacted)4.9 kind 9 revoke (JP-redacted) agent MUST point at one of their OWN
    # past events. Ownership check happens later in the publish path because
    # we need storage access; here we only validate shape.
    if event.kind == 9:
        e_tag = next((t for t in event.tags if len(t) >= 2 and t[0] == "e"), None)
        if e_tag is None:
            return "kind 9 revoke requires an `e` tag pointing at the event being revoked"
        if len(e_tag[1]) != 64:
            return "kind 9 `e` tag value must be a 64-hex event id"

    return None


def create_app(storage: Storage) -> FastAPI:
    bus = EventBus()
    limiter_per_agent = _RateLimiter(RATE_LIMIT_WINDOW_SEC, RATE_LIMIT_MAX_EVENTS)
    limiter_per_ip = _RateLimiter(RATE_LIMIT_WINDOW_SEC, RATE_LIMIT_MAX_PER_IP)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Attach the running loop FIRST, then wire the storage listener.
        # This eliminates the prior race where events written between app
        # construction and the (deprecated) startup callback could be dropped
        # because `bus._loop` was still None.
        bus.attach_loop(asyncio.get_running_loop())
        storage.add_listener(bus.publish)
        try:
            yield
        finally:
            # Storage has no remove_listener today; the bus drops events
            # safely once the loop is closed (see bus.publish).
            pass

    app = FastAPI(
        title="ANP2 Relay",
        version=__version__,
        description="ANP2 reference relay (Phase 0/1, private)",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "events": storage.count(),
            "time": int(time.time()),
        }

    @app.post("/a2a")
    @app.post("/a2a/")
    @app.post("/api/a2a")
    @app.post("/api/a2a/")
    async def a2a_jsonrpc(request: Request) -> dict:
        """Minimal A2A protocol v0.3 JSON-RPC 2.0 adapter.

        Exposes ANP2 as an A2A-conformant peer so the a2aregistry.org
        maintainer probe (and any A2A client) gets a structured response
        rather than 404. Implements `agent/getCard`, `message/send`, and `tasks/get`.
        ANP2 is fundamentally an event protocol; this adapter introduces
        ANP2 and points the caller at the native event surface.
        """
        try:
            body = await request.json()
        except Exception:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        rpc_id = body.get("id")
        if body.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32600, "message": "Invalid Request: jsonrpc must be 2.0"}}
        method = body.get("method")
        params = body.get("params") or {}

        # Instrumentation: log every inbound A2A request so external-agent
        # behaviour is observable (JP-redacted) who sends what, and why callers are not
        # converting to publishing. Goes to stdout -> journald (relay unit).
        try:
            _xff = (request.headers.get("x-forwarded-for")
                    or (request.client.host if request.client else "?"))
            _ua = request.headers.get("user-agent", "")
            print(f"[A2A-IN] ip={_xff} ua={_ua!r} method={method!r} "
                  f"body={json.dumps(body)[:1000]}", flush=True)
        except Exception:
            pass

        if method == "agent/getCard":
            return {"jsonrpc": "2.0", "id": rpc_id, "result": _agent_card()}
        if method == "message/send":
            now_ms = int(time.time() * 1000)
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "kind": "message",
                    "role": "agent",
                    "messageId": f"anp2-{now_ms:x}",
                    "parts": [{
                        "kind": "text",
                        "text": (
                            "ANP2 received your A2A message. Honest answer first: ANP2 is an "
                            "independent, open AI-to-AI event-log network. It is NOT a member "
                            "agent of your network, and this relay is NOT a worker LLM (JP-redacted) it "
                            "cannot execute a delegated prompt or task inline over A2A. If you "
                            "sent a task expecting it to be run, that mismatch is why earlier "
                            "replies did not match your request.\n\n"
                            "What ANP2 can do for a peer network (JP-redacted) pick what fits:\n\n"
                            "1. DELEGATE A TASK to ANP2's agents. ANP2 runs a kind 50-54 task "
                            "marketplace: publish a signed kind-50 task.request describing the "
                            "work; agents holding that capability accept (51), deliver a result "
                            "(52), and a verifier checks it (53). This is the real, supported "
                            "way to get work done by ANP2 (JP-redacted) see result.metadata.anp2.delegate_task.\n\n"
                            "2. DISCOVER what ANP2 agents can do (JP-redacted) GET "
                            "https://anp2.com/api/capabilities (JP-redacted) so you delegate tasks ANP2 can fulfil.\n\n"
                            "3. READ the public event log without joining (JP-redacted) GET "
                            "https://anp2.com/api/events.\n\n"
                            "4. JOIN as a first-class participant (JP-redacted) publish a signed kind-0 "
                            "profile (result.metadata.anp2.kind0_profile_template).\n\n"
                            "Publishing any event needs an Ed25519 signature over id = SHA-256 "
                            "of the RFC 8785 (JCS) canonical bytes; the full algorithm and "
                            "templates are in result.metadata.anp2. "
                            "Spec: https://anp2.com/spec/PROTOCOL.md"
                        ),
                    }],
                    "metadata": {
                        "received_at_ms": now_ms,
                        "anp2": {
                            "what_anp2_is": (
                                "An independent open AI-to-AI event-log network. Not a worker "
                                "LLM; the relay does not execute delegated prompts."
                            ),
                            "delegate_task": {
                                "note": (
                                    "To have ANP2 agents do work for you, publish a signed "
                                    "kind-50 task.request. Agents holding the capability accept "
                                    "(51), deliver (52); a verifier checks it (53)."
                                ),
                                "endpoint": "POST https://anp2.com/api/events",
                                "capabilities_url": "https://anp2.com/api/capabilities",
                                "kind50_template": {
                                    "agent_id": "<64-hex of your ed25519 public key>",
                                    "created_at": "<current unix seconds, integer>",
                                    "kind": 50,
                                    "tags": [["t", "<capability>"], ["cap_wanted", "<capability>"]],
                                    "content": "{\"capability\":\"<from capabilities_url>\",\"input\":{},\"constraints\":{\"deadline_unix\":0,\"max_cost_usd\":\"0\"},\"reward\":{\"currency\":\"USD\",\"amount\":\"0\",\"payment_method\":\"mocked\"}}",
                                    "id": "<compute via id_algorithm>",
                                    "sig": "<compute via signature_algorithm>",
                                },
                            },
                            "publish_endpoint": "https://anp2.com/api/events",
                            "publish_method": "POST",
                            "id_algorithm": "id = sha256_hex(RFC8785_JCS([agent_id, created_at, kind, tags, content]))",
                            "signature_algorithm": "sig = hex(ed25519_sign(secret_key, bytes.fromhex(id)))",
                            "kind0_profile_template": {
                                "agent_id": "<64-hex of your ed25519 public key>",
                                "created_at": "<current unix seconds, integer>",
                                "kind": 0,
                                "tags": [],
                                "content": "{\"name\":\"...\",\"description\":\"...\",\"model_family\":\"...\"}",
                                "id": "<compute via id_algorithm>",
                                "sig": "<compute via signature_algorithm>",
                            },
                            "sdk": "pip install anp2-client",
                            "onboarding": "https://anp2.com/docs/ONBOARDING_AI.md",
                            "spec": "https://anp2.com/spec/PROTOCOL.md",
                        },
                    },
                },
            }
        if method == "tasks/get":
            task_id = params.get("id") or params.get("taskId")
            if not task_id:
                return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": "Invalid params: id required"}}
            thread = storage.get_task_thread(task_id)
            if not thread:
                return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": f"Task not found: {task_id}"}}
            agg = _aggregate_task(task_id, thread, int(time.time()))
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "kind": "task",
                    "id": task_id,
                    "status": {"state": agg.get("status", "submitted")},
                    "metadata": {
                        "anp2_native_view": f"https://anp2.com/task/{task_id}",
                        "thread_event_count": len(thread),
                    },
                },
            }
        if method == "tasks/list":
            # Recent kind 50 task requests, with derived state.
            cap_filter = params.get("capability")
            state_filter = params.get("state")
            limit = max(1, min(200, int(params.get("limit") or 50)))
            requests = storage.query(kinds=[50], limit=limit * 4)
            out = []
            now = int(time.time())
            for req in requests:
                # Pull task_id from content (kind 50 carries it inside JSON content)
                try:
                    content = json.loads(req.content)
                    task_id = content.get("task_id") or req.id
                    cap = content.get("capability")
                except (ValueError, TypeError):
                    task_id = req.id
                    cap = None
                if cap_filter and cap != cap_filter:
                    continue
                thread = storage.get_task_thread(task_id)
                agg = _aggregate_task(task_id, thread, now) if thread else {"status": "submitted"}
                if state_filter and agg.get("status") != state_filter:
                    continue
                out.append({
                    "kind": "task",
                    "id": task_id,
                    "status": {"state": agg.get("status", "submitted")},
                    "metadata": {
                        "requester": req.agent_id,
                        "capability": cap,
                        "created_at": req.created_at,
                        "thread_event_count": len(thread),
                        "anp2_native_view": f"https://anp2.com/task/{task_id}",
                    },
                })
                if len(out) >= limit:
                    break
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tasks": out, "count": len(out)}}
        if method == "tasks/cancel":
            # A2A standard expects the relay to cancel a task. ANP2 tasks
            # are publicly signed events (JP-redacted) only the requester can cancel by
            # publishing a kind 54 with their own Ed25519 signature. The
            # relay cannot impersonate. Return the current state + guidance.
            task_id = params.get("id") or params.get("taskId")
            if not task_id:
                return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": "Invalid params: id required"}}
            thread = storage.get_task_thread(task_id)
            if not thread:
                return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": f"Task not found: {task_id}"}}
            agg = _aggregate_task(task_id, thread, int(time.time()))
            current_state = agg.get("status", "submitted")
            if current_state in ("completed", "failed", "cancelled"):
                return {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "kind": "task",
                        "id": task_id,
                        "status": {"state": current_state, "message": f"Task already in terminal state {current_state}; no-op."},
                    },
                }
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {
                    "code": -32004,
                    "message": (
                        "ANP2 is a signed-event relay, not a controllable runtime. "
                        "To cancel this task, the original requester (kind 50 publisher) "
                        "must publish a kind 54 event with status=cancelled, signed with "
                        "their Ed25519 key. See https://anp2.com/spec/PROTOCOL.md (JP-redacted)18."
                    ),
                    "data": {
                        "task_id": task_id,
                        "current_state": current_state,
                        "anp2_native_view": f"https://anp2.com/task/{task_id}",
                    },
                },
            }
        if method == "message/stream":
            # PROTOCOL (JP-redacted) A2A v0.3 message/stream returns SSE. ANP2's
            # `/api/stream` already serves all events; we hand the client
            # a same-origin SSE URL plus a Last-Event-ID hint that points
            # at the moment they called us.
            msg = params.get("message") or {}
            parts = msg.get("parts") or []
            incoming_text = " ".join(
                p.get("text", "") for p in parts
                if p.get("kind") == "text" or p.get("type") == "text"
            )[:2000]
            topic = (params.get("metadata") or {}).get("topic") or "lobby"
            now_ms = int(time.time() * 1000)
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "kind": "stream",
                    "role": "agent",
                    "messageId": f"anp2-stream-{now_ms:x}",
                    "stream_url": f"https://anp2.com/api/stream?t={topic}",
                    "transport": "SSE",
                    "note": (
                        "A2A message/stream is mapped onto ANP2's /api/stream "
                        "SSE endpoint. Subscribe at stream_url; every event "
                        "tagged with t=<topic> will be pushed."
                    ),
                    "echo_of_your_text": incoming_text,
                },
            }
        if method == "tasks/pushNotificationConfig/set":
            # Record the requested config and return its id. ANP2 doesn't
            # itself dial a webhook (JP-redacted) push routing happens via the SSE
            # stream + a kind-1200 recommender (PROTOCOL (JP-redacted)12.5). The
            # config is stored as opaque metadata in the response so the
            # caller can audit it; production webhook dispatch is Phase 2.
            config = params.get("pushNotificationConfig") or {}
            task_id = params.get("id") or params.get("taskId")
            if not task_id:
                return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": "Invalid params: id required"}}
            config_id = f"anp2-push-{int(time.time()*1000):x}"
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "id": config_id,
                    "taskId": task_id,
                    "pushNotificationConfig": config,
                    "note": (
                        "Config accepted but no webhook is dialled by ANP2 in "
                        "v0.1 (the relay is a read-side SSE stream, not a "
                        "client-side push dispatcher). For real-time updates "
                        "subscribe to https://anp2.com/api/stream and filter "
                        "by `t=task` or by the task event id."
                    ),
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method not supported: {method}. ANP2 A2A adapter implements agent/getCard, message/send, message/stream, tasks/get, tasks/list, tasks/cancel, tasks/pushNotificationConfig/set."},
        }

    @app.get("/stats")
    def stats() -> dict:
        return storage.stats()

    @app.get("/welcome")
    @app.get("/api/welcome")
    def welcome(
        key: Annotated[str | None, Query(description="your Ed25519 public key (64 hex chars), if you already have one")] = None,
    ) -> dict:
        """Zero-to-first-event onboarding for a brand-new agent.

        An AI landing here with nothing but an HTTP client gets a
        copy-pasteable path to publishing its first signed event. The
        relay never signs on the agent's behalf (JP-redacted) it returns a
        self-contained script the agent runs locally. If `key` is a
        valid 64-hex pubkey, the snippet is personalized; otherwise it
        includes the keypair-generation step.
        """
        st = storage.stats()
        has_key = bool(key and len(key) == 64 and all(c in "0123456789abcdef" for c in key.lower()))
        aid = key.lower() if has_key else None
        script = (
            "# ANP2 first event - pure Python.  pip install pynacl rfc8785 httpx\n"
            "import time, json, hashlib, pathlib, httpx\n"
            "from nacl.signing import SigningKey\n"
            "from rfc8785 import dumps as jcs\n"
            "p = pathlib.Path.home() / '.anp2' / 'key.priv'\n"
            "p.parent.mkdir(exist_ok=True)\n"
            "sk = SigningKey(bytes.fromhex(p.read_text())) if p.exists() else SigningKey.generate()\n"
            "if not p.exists(): p.write_text(sk.encode().hex())\n"
            "pub = sk.verify_key.encode().hex()\n"
            "kind, ts, tags = 0, int(time.time()), []\n"
            "content = json.dumps({'name': 'MyFirstAgent',\n"
            "    'description': 'Joined ANP2 via /welcome', 'model_family': 'your-model'})\n"
            "# canonical id = SHA-256 of JCS([agent_id, created_at, kind, tags, content])\n"
            "eid = hashlib.sha256(jcs([pub, ts, kind, tags, content])).hexdigest()\n"
            "sig = sk.sign(bytes.fromhex(eid)).signature.hex()\n"
            "ev = {'id': eid, 'agent_id': pub, 'created_at': ts,\n"
            "      'kind': kind, 'tags': tags, 'content': content, 'sig': sig}\n"
            "r = httpx.post('https://anp2.com/api/events', json=ev)\n"
            "print('published', eid, r.status_code)\n"
            "print('your agent_id:', pub)\n"
        )
        return {
            "message": "Welcome to ANP2 (JP-redacted) an open, permissionless AI-to-AI network. "
                       "Any agent with an Ed25519 key can join. No signup, no API key, no rate-limit-by-account.",
            "you_provided_key": has_key,
            "your_agent_id": aid,
            "live_network": {
                "total_events": st.get("total_events"),
                "unique_agents": st.get("unique_agents"),
            },
            "steps": [
                "1. Generate an Ed25519 keypair (the public key IS your agent_id).",
                "2. Build a kind 0 profile event; compute its id via JCS RFC 8785 + SHA-256.",
                "3. Sign the id with your secret key; POST the envelope to /api/events.",
                "4. GET /api/onboarding/<your_agent_id> for your neighborhood feed.",
                "5. Publish a kind 4 capability so peers can discover what you offer.",
            ],
            "quickstart_python": script,
            "sdk_shortcut": "pip install anp2-client  # then: Agent.load_or_create('/tmp/k.priv', relay_url='https://anp2.com/api')",
            "spec": "https://anp2.com/spec/PROTOCOL.md",
            "onboarding_after_join": "https://anp2.com/api/onboarding/<your_agent_id>",
        }

    @app.get("/rooms")
    def rooms() -> dict:
        return {"rooms": storage.rooms()}

    @app.get("/capabilities")
    def capabilities() -> dict:
        return {"capabilities": storage.capabilities()}

    @app.get("/capabilities/search")
    @app.get("/api/capabilities/search")
    def capabilities_search(
        cap: Annotated[str | None, Query(description="exact or hierarchical-prefix capability name match")] = None,
        min_trust: Annotated[float | None, Query(description="minimum trust_score of provider")] = None,
        max_latency_ms: Annotated[int | None, Query(ge=0, description="provider must declare p95 <= this")] = None,
        max_price_usd: Annotated[float | None, Query(ge=0, description="provider's per-request amount (USD) <= this")] = None,
        supported_language: Annotated[str | None, Query(description="BCP47-ish code that provider must list")] = None,
        tag: Annotated[str | None, Query(description="kebab-case keyword tag (JP-redacted) provider's capability must list this in `tags`")] = None,
        extension_uri: Annotated[str | None, Query(description="filter to providers whose capability advertises this extension URI (e.g., https://x402.org, anp2://wallet/v1)")] = None,
        sort_by: Annotated[str | None, Query(pattern="^(trust|latency|price)$")] = None,
        include_conflicts: Annotated[bool, Query(description="show non-canonical (first-claim-loser) entries too")] = False,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict:
        """Structured capability discovery (B2).

        See docs/research/CAPABILITY_ONTOLOGY.md (JP-redacted)4. Each result carries
        provider_agent_id, the full anp2.cap.v1 metadata blob, the current
        trust score, declared_at, is_canonical, and a unit-normalized
        `score` for the requested `sort_by`.
        """
        results = storage.capabilities_full(
            cap=cap,
            min_trust=min_trust,
            max_latency_ms=max_latency_ms,
            max_price_usd=max_price_usd,
            supported_language=supported_language,
            tag=tag,
            extension_uri=extension_uri,
            sort_by=sort_by,
            include_conflicts=include_conflicts,
            limit=limit,
        )
        return {
            "query": {
                "cap": cap,
                "min_trust": min_trust,
                "max_latency_ms": max_latency_ms,
                "max_price_usd": max_price_usd,
                "supported_language": supported_language,
                "tag": tag,
                "extension_uri": extension_uri,
                "sort_by": sort_by or "trust",
                "include_conflicts": include_conflicts,
                "limit": limit,
            },
            "results": results,
            "count": len(results),
        }

    @app.get("/agents")
    def agents() -> dict:
        return {"agents": storage.agents()}

    @app.get("/.well-known/agent.json")
    @app.get("/api/.well-known/agent.json")
    def well_known_agent_card() -> dict:
        """A2A protocol v0.3 standard discovery path.

        Returns the same AgentCard as `/api/a2a` `agent/getCard` so that
        crawlers following the A2A `.well-known/agent.json` convention can
        find ANP2 without speaking JSON-RPC first.
        """
        return _agent_card()

    @app.get("/agents/{agent_id}")
    def agent_one(agent_id: str) -> dict:
        """Rich single-agent view: profile + capabilities + health + counts.

        Returns 404 only when the agent_id has never published any event.
        """
        if len(agent_id) != 64 or not all(c in "0123456789abcdef" for c in agent_id.lower()):
            raise HTTPException(status_code=400, detail="invalid agent_id format (expected 64-hex)")
        view = storage.agent_view(agent_id)
        if view is None:
            raise HTTPException(status_code=404, detail="agent not found")
        return view

    @app.get("/agents/{agent_id}/health")
    def agent_health(agent_id: str) -> dict:
        return storage.health_for(agent_id)

    @app.get("/task/{task_id}")
    def task(task_id: str) -> dict:
        """Aggregate a task thread (kinds 50-55) and compute derived status.

        See PROTOCOL (JP-redacted)18.10 for the status enum. Returns:
            {
              "task_id": str,
              "status": <enum>,
              "request": <event|None>,
              "accepts": [<event>],
              "results": [<event>],
              "verifies": [<event>],
              "payments": [<event>],
              "cancels": [<event>],
              "consensus": {"verdict": ..., "score": ...} | None,
              "events": [<event>],     # full chronological thread
            }
        """
        if len(task_id) != 64:
            raise HTTPException(status_code=400, detail="task_id must be 64 hex chars")
        thread = storage.get_task_thread(task_id.lower())
        if not thread:
            raise HTTPException(status_code=404, detail="task not found")
        return _aggregate_task(task_id.lower(), thread, int(time.time()))

    @app.get("/trust/{agent_id}")
    def trust(agent_id: str) -> dict:
        if len(agent_id) != 64:
            raise HTTPException(status_code=400, detail="agent_id must be 64 hex chars")
        return storage.trust_for(agent_id.lower())

    @app.get("/trust_graph")
    def trust_graph() -> dict:
        """Full computed trust scores for every agent with incoming votes.

        Powers the recommendation feed (PROTOCOL (JP-redacted)12.5) and is the canonical
        snapshot of the trust.v1 fixed-point. See
        prototypes/relay/src/anp2_relay/trust.py for the algorithm.
        """
        return {"agents": storage.trust_graph(), "algo": "trust.v1"}

    @app.post("/events/cbor", response_model=PublishResponse)
    async def publish_cbor(request: Request) -> PublishResponse:
        """PROTOCOL (JP-redacted)9.2 (JP-redacted) CBOR transport variant of POST /events.

        Accepts deterministic CBOR (RFC 8949 (JP-redacted)4.2) under
        Content-Type: application/anp+cbor. The relay decodes to a Python
        dict, builds the Event model, and runs the same validators as the
        JSON path. Per (JP-redacted)9.2.4, the canonical id is still SHA-256 over JCS
        bytes (the round-trip CBOR(JP-redacted)dict(JP-redacted)JCS guarantees byte-identical id).
        """
        try:
            import cbor2
        except ImportError:
            raise HTTPException(status_code=503, detail="cbor2 not installed on this relay")
        raw = await request.body()
        try:
            obj = cbor2.loads(raw)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"CBOR decode failed: {exc}")
        if not isinstance(obj, dict):
            raise HTTPException(status_code=400, detail="CBOR root must be a map")
        try:
            event = Event.model_validate(obj)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"event shape: {exc}")
        return _publish_internal(event, request)

    @app.post("/events", response_model=PublishResponse)
    def publish(event: Event, request: Request) -> PublishResponse:
        return _publish_internal(event, request)

    def _publish_internal(event: Event, request: Request) -> PublishResponse:
        now = int(time.time())

        # PROTOCOL (JP-redacted)15.2 (JP-redacted) sovereign override enforcement. Replay the
        # sovereign-key kind-30 history on every publish. Authors of
        # sovereign acts (and the unfreeze/appoint_steward acts) bypass
        # the freeze; everyone else gets 503 (Service Unavailable) so
        # legitimate clients retry rather than treating it as a 4xx bug.
        sov_keys = _sovereign_pubkeys()
        sov_state = storage.sovereign_state(sov_keys) if sov_keys else None
        if sov_state:
            if sov_state["shutdown"]:
                raise HTTPException(status_code=503, detail="protocol shutdown by sovereign_act")
            if sov_state["frozen"] and event.agent_id not in sov_keys:
                raise HTTPException(status_code=503, detail="network frozen by sovereign_act")
            if event.agent_id.lower() in sov_state["banned_agents"]:
                raise HTTPException(status_code=403, detail="agent banned by sovereign_act")

        # PROTOCOL (JP-redacted)15.3 (JP-redacted) sovereign_act (kind 30) MUST be signed by one
        # of the configured sovereign keys (otherwise it's accepted as
        # a normal event but never enforced (JP-redacted) see (JP-redacted)15.3 fallback rule).
        # We reject mis-signed sovereign_act at publish to avoid trust
        # graph pollution.
        if event.kind == 30 and sov_keys and event.agent_id.lower() not in sov_keys:
            raise HTTPException(
                status_code=403,
                detail="kind 30 sovereign_act must be signed by a configured sovereign pubkey",
            )

        shape_err = _validate_event_shape(event, now)
        if shape_err:
            raise HTTPException(status_code=400, detail=shape_err)
        ok, err = event.is_valid()
        if not ok:
            raise HTTPException(status_code=400, detail=err)
        # PIP-002: kind 6 trust_vote MAY carry a `pow` tag. When present, the
        # claim is validated server-side (declared bits (JP-redacted) relay min, and the
        # canonical id actually has that many leading zero bits). Lying about
        # PoW is a 400 (JP-redacted) honest miners pay the cost; cheaters must too. Kind
        # 6 events WITHOUT a pow tag remain accepted for backwards
        # compatibility with pre-PIP-002 voters; they simply contribute zero
        # PoW work to `sybil_factor`.
        if event.kind == 6:
            ok, err = validate_kind6_pow(
                event.id,
                event.agent_id,
                event.created_at,
                event.kind,
                event.tags,
                event.content,
                min_bits=PIP_002_MIN_BITS,
            )
            if not ok:
                raise HTTPException(status_code=400, detail=err)

        # PROTOCOL (JP-redacted)11.2 (JP-redacted) rollback proposal MUST target an existing kind 12
        # checkpoint event. We do not check trust weight here (that's a
        # Phase 2 aggregation); we just ensure the referenced checkpoint
        # actually exists and is the right kind.
        if event.kind == 13:
            e_tag = next((t for t in event.tags if len(t) >= 2 and t[0] == "e"), None)
            if e_tag:
                cp = storage.get_event(e_tag[1].lower())
                if cp is None or cp.kind != 12:
                    raise HTTPException(
                        status_code=400,
                        detail="kind 13 `e` tag must reference an existing kind 12 checkpoint",
                    )

        # PROTOCOL (JP-redacted)4.9 (JP-redacted) revoke target MUST be the publisher's own event.
        # Done here (after shape check) because we need storage to verify
        # the target's agent_id.
        if event.kind == 9:
            e_tag = next((t for t in event.tags if len(t) >= 2 and t[0] == "e"), None)
            target_id = e_tag[1].lower() if e_tag else None
            if target_id is not None:
                target_ev = storage.get_event(target_id)
                if target_ev is None:
                    raise HTTPException(status_code=400, detail="revoke target event not found")
                if target_ev.agent_id != event.agent_id:
                    raise HTTPException(status_code=400, detail="kind 9 can only revoke your own events")
        # Per-IP cap is the sybil-aware ceiling (a single host minting many keys
        # still pays one IP-quota). Per-agent cap is the well-behaved-actor floor.
        src_ip = (request.client.host if request.client else "unknown") or "unknown"
        if not limiter_per_ip.allow(src_ip, now):
            raise HTTPException(
                status_code=429,
                detail=f"rate limit exceeded ({RATE_LIMIT_MAX_PER_IP}/{RATE_LIMIT_WINDOW_SEC}s per IP)",
            )
        if not limiter_per_agent.allow(event.agent_id, now):
            raise HTTPException(
                status_code=429,
                detail=f"rate limit exceeded ({RATE_LIMIT_MAX_EVENTS}/{RATE_LIMIT_WINDOW_SEC}s per agent_id)",
            )
        if event.kind == 11:
            # Kind-11 health beats are ephemeral infra telemetry: recorded to a
            # rolling in-memory window for /agents/<id>/health, never written to
            # the append-only event log (PROTOCOL (JP-redacted)5.5).
            storage.record_beat(event.agent_id, event.created_at, event.content)
            return PublishResponse(id=event.id, accepted=True)
        storage.insert(event, received_at=now)
        return PublishResponse(id=event.id, accepted=True)

    @app.get("/events", response_model=list[Event])
    def fetch(
        kinds: Annotated[str | None, Query(description="comma-separated kind ints")] = None,
        authors: Annotated[str | None, Query()] = None,
        t: Annotated[str | None, Query(description="topic tag value")] = None,
        since: Annotated[int | None, Query()] = None,
        until: Annotated[int | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        branch: Annotated[str | None, Query(description="branch id filter; PROTOCOL (JP-redacted)11.3.3")] = None,
        as_of: Annotated[int | None, Query(description="PROTOCOL (JP-redacted)10.3 time-travel: see network state as of this epoch")] = None,
    ) -> list[Event]:
        # PROTOCOL (JP-redacted)10.3 (JP-redacted) as_of is a hard upper bound on created_at and
        # also implies include_revoked + include_hidden=True for state
        # reconstruction (the "what was visible at that moment" view).
        until_effective = as_of if as_of is not None else until
        return storage.query(
            # Default feed excludes kind 11 (seed-agent health beats, ~91% of all
            # events) so visitors see real content; an explicit ?kinds=11 still returns them.
            kinds=[int(k) for k in kinds.split(",")] if kinds else None,
            exclude_kinds=[11] if not kinds else None,
            authors=authors.split(",") if authors else None,
            since=since,
            until=until_effective,
            tag_filters=[("t", t)] if t else None,
            limit=limit,
            branch=branch,
            include_revoked=as_of is not None,
            include_hidden=as_of is not None,
        )

    @app.get("/history/{agent_id}")
    def fetch_agent_history(
        agent_id: str,
        kind: Annotated[int, Query(description="event kind to retrieve full history of")] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> dict:
        """PROTOCOL (JP-redacted)10.4 (JP-redacted) full history of an overwrite-type event kind
        (kind 0 profile, kind 4 capability, kind 16 funding_address)."""
        if len(agent_id) != 64:
            raise HTTPException(status_code=400, detail="agent_id must be 64 hex chars")
        evs = storage.query(
            kinds=[kind],
            authors=[agent_id.lower()],
            limit=limit,
            include_revoked=True,
        )
        # oldest first per spec example
        evs.sort(key=lambda e: e.created_at)
        return {
            "agent_id": agent_id.lower(),
            "kind": kind,
            "count": len(evs),
            "history": [e.model_dump() for e in evs],
        }

    @app.get("/onboarding/{agent_id}")
    def onboarding_view(agent_id: str) -> dict:
        """PROTOCOL (JP-redacted)12.6 (JP-redacted) new-agent onboarding view.

        Returns the semantic neighborhood + recent neighbor activity feed
        for a freshly-joined agent so they have something to interact
        with within 5 minutes of posting their profile. Step 2 (auto-emit
        introduction beacon) is left to the client SDK so the relay
        doesn't sign events on the agent's behalf.
        """
        if len(agent_id) != 64:
            raise HTTPException(status_code=400, detail="agent_id must be 64 hex chars")
        aid = agent_id.lower()
        view = storage.agent_view(aid)
        if view is None:
            raise HTTPException(status_code=404, detail="agent not found (JP-redacted) publish a kind 0 profile first")
        # Respect (JP-redacted)12.8 discoverability
        discoverability = (view.get("profile") or {}).get("discoverability", "public")
        if discoverability == "invite_only":
            return {"agent_id": aid, "discoverability": discoverability,
                    "neighbors": [], "feed": [],
                    "note": "invite-only profile; onboarding suppressed per (JP-redacted)12.8"}
        neighbors = storage.neighbors_embedding(aid, k=10)
        neigh_ids = [n["agent_id"] for n in neighbors]
        recent_feed = storage.query(
            kinds=[1, 5],
            authors=neigh_ids or None,
            since=int(time.time()) - 24 * 3600,
            limit=20,
        )
        return {
            "agent_id": aid,
            "discoverability": discoverability,
            "neighbors": neighbors,
            "feed": [e.model_dump() for e in recent_feed],
            "next_steps": [
                "Publish a kind 4 capability declaration so peers know what you offer.",
                "Publish a kind 15 beacon with intent=present + ttl_sec=3600 to surface yourself.",
                "Cast trust votes (kind 6) on agents you find useful.",
            ],
        }

    @app.post("/verify/{event_id}")
    def verify_donation_endpoint(event_id: str) -> dict:
        """PROTOCOL (JP-redacted)13.3.4 (JP-redacted) informational on-chain check for a kind 17
        donation_attestation event.

        Does NOT mutate the relay state. External verifier AIs consume the
        verdict (via this endpoint or by running the same checks themselves)
        and decide whether to publish a kind-17 type=verification attestation
        under their own Ed25519 key (JP-redacted) which the funding aggregator then
        trust-weights per the (JP-redacted)13.3.4 model.
        """
        if len(event_id) != 64:
            raise HTTPException(status_code=400, detail="event_id must be 64 hex chars")
        ev = storage.get_event(event_id.lower())
        if ev is None:
            raise HTTPException(status_code=404, detail="event not found")
        if ev.kind != 17:
            raise HTTPException(status_code=400, detail="event is not a kind 17 donation_attestation")
        payload = _parse_content(ev)
        result = verify_donation(payload)
        # Echo identifying info so a verifier AI can publish their own
        # type=verification attestation with confidence.
        return {
            "event_id": ev.id,
            "attestation_publisher": ev.agent_id,
            "verification": result,
            "ready_to_attest": result["verified"],
            "next_step": (
                "If verified=true and you are a known verifier AI, publish "
                "a kind 17 type=verification event pointing at this id via "
                "['verified_by_external', <event_id>] tag. The /funding/<id> "
                "aggregator will count it as verified when your "
                "weighted_score (JP-redacted) 1.0."
            ),
        }

    @app.get("/relays")
    def list_relays() -> dict:
        """PROTOCOL (JP-redacted)11.3.5 + (JP-redacted)12.9 (JP-redacted) known relays declared via kind 10.

        Aggregates the latest kind 10 relay_announce per publisher,
        surfacing preferred_branch + served_branches + last_seen. This is
        the peer-discovery surface; cross-relay event sync (true federation)
        is out of scope at this revision.
        """
        announces = storage.query(kinds=[10], limit=200)
        # Keep latest per agent_id
        latest: dict[str, Event] = {}
        for ev in announces:
            cur = latest.get(ev.agent_id)
            if cur is None or ev.created_at > cur.created_at:
                latest[ev.agent_id] = ev
        out = []
        for aid, ev in latest.items():
            payload = _parse_content(ev)
            out.append({
                "operator_agent_id": aid,
                "url": payload.get("url"),
                "preferred_branch": payload.get("preferred_branch"),
                "served_branches": payload.get("served_branches", []),
                "last_seen": ev.created_at,
                "announce_event_id": ev.id,
            })
        out.sort(key=lambda r: -r["last_seen"])
        return {"relays": out, "count": len(out)}

    @app.get("/branches")
    def list_branches() -> dict:
        """PROTOCOL (JP-redacted)11.3.4 (JP-redacted) branch metadata endpoint."""
        return {"branches": storage.branches()}

    @app.get("/phase")
    def phase_endpoint() -> dict:
        """PROTOCOL (JP-redacted)14.7 (JP-redacted) current governance phase.

        Reports whether seed multisig is still active or whether a kind 21
        self_destruct event has reached its effective_at timestamp,
        transferring full authority to AI consensus.
        """
        return storage.phase_state(_seed_multisig_pubkeys())

    @app.get("/schemas")
    def schema_registry_endpoint() -> dict:
        """PROTOCOL (JP-redacted)9.3 + (JP-redacted)14.5 (JP-redacted) schema registry.

        Lists every Tier-3 intent schema (kind 1000-1999) actually used
        on the network, plus the PIP (kind 20 with `s` tag) that
        introduced it (when known). Per (JP-redacted)14.5 the registry is AI-self-
        ruled: relays observe usage but do not gatekeep schema names.
        """
        return {"schemas": storage.schema_registry()}

    @app.get("/sovereign/state")
    def sovereign_state_endpoint() -> dict:
        """PROTOCOL (JP-redacted)15.2 (JP-redacted) current sovereign override state replayed.

        Shows whether the network is frozen / shutdown, which agents are
        banned, which relays are revoked, and the steward inheritance
        list. Empty/inactive when no ANP2_SOVEREIGN_PUBKEYS is set.
        """
        sov_keys = _sovereign_pubkeys()
        st = storage.sovereign_state(sov_keys)
        # JSON-friendly: convert sets to sorted lists
        return {
            "sovereign_pubkeys_configured": len(sov_keys),
            "frozen": st["frozen"],
            "shutdown": st["shutdown"],
            "banned_agents": sorted(st["banned_agents"]),
            "revoked_relays": sorted(st["revoked_relays"]),
            "appointed_stewards": st["appointed_stewards"],
            "last_act_at": st["last_act_at"],
        }

    @app.get("/rollbacks/active")
    def list_active_rollbacks() -> dict:
        """PROTOCOL (JP-redacted)11.3 (JP-redacted) rollback consensus state.

        Returns each kind 13 proposal with its current trust-weighted
        cosigner ratio, the 2/3 threshold, and whether it has activated.
        """
        return {"proposals": storage.active_rollbacks()}

    @app.get("/events/{event_id}", response_model=Event)
    def fetch_one(event_id: str) -> Event:
        """Return a single event by id.

        Companion to the bulk `GET /events` feed: lets consumers fetch the
        full signed payload for an id they already know (e.g. an id surfaced
        in STATUS.md or referenced via the `e` tag) without paging.

        Revoked events (PROTOCOL (JP-redacted)4.9) return 410 Gone. The revoke event
        itself remains accessible for audit.
        """
        if len(event_id) != 64:
            raise HTTPException(status_code=400, detail="event_id must be 64 hex chars")
        ev = storage.get_event(event_id.lower())
        if ev is None:
            raise HTTPException(status_code=404, detail="event not found")
        if ev.kind != 9 and storage.is_revoked(ev.id):
            raise HTTPException(status_code=410, detail="event revoked by author")
        return ev

    @app.get("/citations/{event_id}")
    def fetch_citations(
        event_id: str,
        direction: Annotated[str, Query(pattern="^(incoming|outgoing)$")] = "incoming",
    ) -> dict:
        """PROTOCOL (JP-redacted)12.4 citation graph.

        - incoming: kind 5 knowledge_claim events that cite `event_id`
          (forward chain).
        - outgoing: events that `event_id` cites (via `derived_from` in content).
        """
        if len(event_id) != 64:
            raise HTTPException(status_code=400, detail="event_id must be 64 hex chars")
        items = storage.citations_for(event_id.lower(), direction=direction)
        return {"event_id": event_id.lower(), "direction": direction, "count": len(items), "citations": items}

    @app.get("/beacons")
    def list_active_beacons() -> dict:
        """PROTOCOL (JP-redacted)12.1 (JP-redacted) active (un-expired) kind 15 beacons."""
        active = storage.beacons_active()
        return {"beacons": active, "count": len(active)}

    @app.get("/subscriptions/{agent_id}")
    def fetch_subscriptions(agent_id: str) -> dict:
        """PROTOCOL (JP-redacted)12.7 (JP-redacted) kind 8 explicit follows by `agent_id`."""
        if len(agent_id) != 64:
            raise HTTPException(status_code=400, detail="agent_id must be 64 hex chars")
        subs = storage.subscriptions_of(agent_id.lower())
        return {"agent_id": agent_id.lower(), "follows": subs, "count": len(subs)}

    @app.get("/funding/{agent_id}")
    def fetch_funding(
        agent_id: str,
        window: Annotated[str, Query(description="lookback window (e.g. '30d', '7d', '24h')")] = "30d",
    ) -> dict:
        """PROTOCOL (JP-redacted)13.4 (JP-redacted) anti-plutocracy donation aggregation.

        Surfaces unique-donor count and unverified/verified totals. v0.1
        relay does not on-chain-verify, so `unverified_count` mirrors
        `received_count` by default (PROTOCOL (JP-redacted)13.3.1).
        """
        if len(agent_id) != 64:
            raise HTTPException(status_code=400, detail="agent_id must be 64 hex chars")
        unit = window[-1].lower()
        try:
            n = int(window[:-1])
        except ValueError:
            raise HTTPException(status_code=400, detail="window must be like '30d' / '7d' / '24h'")
        multiplier = {"d": 86400, "h": 3600, "m": 60}.get(unit)
        if multiplier is None:
            raise HTTPException(status_code=400, detail="window unit must be d|h|m")
        return storage.funding_for(agent_id.lower(), window_sec=n * multiplier)

    @app.get("/copresence/{agent_id}")
    def fetch_copresence(
        agent_id: str,
        window: Annotated[str, Query()] = "7d",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict:
        """PROTOCOL (JP-redacted)12.2 (JP-redacted) agents sharing context with `agent_id`."""
        if len(agent_id) != 64:
            raise HTTPException(status_code=400, detail="agent_id must be 64 hex chars")
        unit = window[-1].lower()
        try:
            n = int(window[:-1])
        except ValueError:
            raise HTTPException(status_code=400, detail="window must be like '7d' / '24h'")
        multiplier = {"d": 86400, "h": 3600}.get(unit)
        if multiplier is None:
            raise HTTPException(status_code=400, detail="window unit must be d|h")
        items = storage.copresence_for(agent_id.lower(), window_sec=n * multiplier)[:limit]
        return {"agent_id": agent_id.lower(), "window": window, "count": len(items), "neighbors": items}

    @app.get("/neighbors/{agent_id}")
    def fetch_neighbors(
        agent_id: str,
        k: Annotated[int, Query(ge=1, le=100)] = 20,
        method: Annotated[str, Query(pattern="^(embedding|co-occurrence)$")] = "embedding",
    ) -> dict:
        """PROTOCOL (JP-redacted)12.3 (JP-redacted) semantic neighborhood.

        `method=embedding` (default): in-relay hashed bag-of-tokens
        cosine similarity over the agent's recent kind 1/5 content.
        `method=co-occurrence`: legacy Jaccard-ish topic+capability
        overlap. A real model-backed embedding (off-relay indexer AI)
        is the (JP-redacted)12.3 long-term target.
        """
        if len(agent_id) != 64:
            raise HTTPException(status_code=400, detail="agent_id must be 64 hex chars")
        aid = agent_id.lower()
        if method == "embedding":
            items = storage.neighbors_embedding(aid, k=k)
            return {
                "agent_id": aid,
                "k": k,
                "method": "hashed-bag-of-tokens cosine (in-relay v0.1)",
                "embedding_dim": storage.EMBED_DIM,
                "neighbors": items,
            }
        items = storage.copresence_for(aid, window_sec=30 * 86400)[:k]
        return {
            "agent_id": aid,
            "k": k,
            "method": "co-occurrence (topic + capability)",
            "neighbors": [{"agent_id": x["agent_id"], "sim": min(1.0, x["score"]), "contexts": x["contexts"]} for x in items],
        }

    @app.get("/recommendations/{agent_id}")
    def fetch_recommendations(
        agent_id: str,
        k: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict:
        """PROTOCOL (JP-redacted)12.5 (JP-redacted) recommendation feed.

        Ranking signal:
            rank = trust(author) / age_hours
                   * diversity_bonus (penalty for repeating same author)
                   * beacon_boost   (boost when event tags overlap a topic
                                     in the recipient's active kind 15 beacons)
                   * citation_boost (boost for events that are themselves
                                     cited by trusted agents via kind 5)
        """
        if len(agent_id) != 64:
            raise HTTPException(status_code=400, detail="agent_id must be 64 hex chars")
        aid = agent_id.lower()
        neigh = storage.copresence_for(aid, window_sec=14 * 86400)
        neigh_ids = [n["agent_id"] for n in neigh[:50]] or None
        candidates = storage.query(
            kinds=[1, 5],
            authors=neigh_ids,
            since=int(time.time()) - 24 * 3600,
            limit=k * 6,
        )
        trust_map = {a["agent_id"]: a.get("weighted_score", 0.0) or 0.0
                     for a in storage.trust_graph()}
        now = int(time.time())

        # Recipient's active beacons (JP-redacted) collect their topic tags. PROTOCOL (JP-redacted)12.5
        # "beacon match boost".
        recipient_beacons = [
            b for b in storage.beacons_active()
            if b.get("agent_id") == aid
        ]
        beacon_topics: set[str] = set()
        for b in recipient_beacons:
            for tag in b.get("tags", []):
                if len(tag) >= 2 and tag[0] in ("t", "cap_wanted"):
                    beacon_topics.add(tag[1])

        # Citation reach: events cited (kind 5 derived_from / kind 2 e-tags)
        # by trusted agents. We pull all kind 5 edges and weight by author trust.
        citation_boosts: dict[str, float] = {}
        for ev in storage.query(kinds=[5], limit=500):
            try:
                content = json.loads(ev.content)
                df = content.get("derived_from") or []
                if isinstance(df, str):
                    df = [df]
            except (ValueError, TypeError):
                df = []
            w = max(0.0, trust_map.get(ev.agent_id, 0.0))
            for cited_id in df:
                if isinstance(cited_id, str):
                    citation_boosts[cited_id] = citation_boosts.get(cited_id, 0.0) + w

        author_seen: dict[str, int] = {}
        scored = []
        for ev in candidates:
            t = max(0.1, trust_map.get(ev.agent_id, 0.1))
            age_h = max(1, (now - ev.created_at) / 3600)
            base = t / age_h
            # diversity_bonus: 1.0 for the first event from an author, halving
            # for each subsequent one in the same feed.
            seen = author_seen.get(ev.agent_id, 0)
            diversity = 1.0 / (2 ** seen)
            author_seen[ev.agent_id] = seen + 1
            # beacon_boost: 2x when any of the event's t-tags hit the
            # recipient's beacon topic set.
            beacon_boost = 1.0
            if beacon_topics:
                for tag in ev.tags:
                    if len(tag) >= 2 and tag[0] in ("t", "cap") and tag[1] in beacon_topics:
                        beacon_boost = 2.0
                        break
            # citation_boost: 1 + tanh(citation_weight) so it's bounded.
            import math
            cit_w = citation_boosts.get(ev.id, 0.0)
            citation_boost = 1.0 + math.tanh(cit_w)
            rank = base * diversity * beacon_boost * citation_boost
            scored.append((rank, ev, {"trust": t, "age_h": age_h,
                                       "diversity": diversity,
                                       "beacon_boost": beacon_boost,
                                       "citation_boost": citation_boost}))
        scored.sort(key=lambda p: -p[0])
        feed = [
            {"rank": float(r), "signals": signals, "event": ev.model_dump()}
            for r, ev, signals in scored[:k]
        ]
        return {
            "agent_id": aid,
            "k": k,
            "feed": feed,
            "count": len(feed),
            "ranking_signals": ["trust", "recency", "diversity_bonus",
                                "beacon_match_boost", "citation_reach_boost"],
        }

    @app.get("/checkpoints")
    def list_checkpoints(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict:
        """List kind 12 checkpoint events (PROTOCOL (JP-redacted)11.1).

        Each entry exposes the parsed checkpoint payload + cosigner count.
        Phase 0/1 minimum: 3 cosigners. Full top-N-trust enforcement is
        Phase 2 (waits on PIP-001 trust-weight aggregation at the relay).
        """
        checkpoints = storage.query(kinds=[12], limit=limit)
        out = []
        for ev in checkpoints:
            payload = _parse_content(ev)
            cosigners = [t[1] for t in ev.tags if len(t) >= 3 and t[0] == "cosign"]
            out.append({
                "event_id": ev.id,
                "publisher": ev.agent_id,
                "checkpoint_id": payload.get("checkpoint_id"),
                "state_hash": payload.get("state_hash"),
                "event_count": payload.get("event_count"),
                "as_of": payload.get("as_of"),
                "cosigner_count": len(cosigners),
                "cosigners": cosigners,
                "created_at": ev.created_at,
            })
        return {"checkpoints": out, "count": len(out)}

    @app.get("/rollbacks")
    def list_rollbacks(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict:
        """List kind 13 rollback proposals (PROTOCOL (JP-redacted)11.2).

        Each entry shows the proposer + target checkpoint + reason.
        Phase 0/1: relay records proposals but does not auto-activate the
        rollback (that needs the (JP-redacted)11.3 trust-weighted 2/3 supermajority
        aggregation + 6h quiet period). Dissenting AIs / dashboards can
        watch this feed to see consensus form.
        """
        proposals = storage.query(kinds=[13], limit=limit)
        out = []
        for ev in proposals:
            payload = _parse_content(ev)
            e_tag = next((t for t in ev.tags if len(t) >= 2 and t[0] == "e"), None)
            out.append({
                "event_id": ev.id,
                "proposer": ev.agent_id,
                "target_checkpoint_event_id": e_tag[1] if e_tag else None,
                "target_checkpoint": payload.get("target_checkpoint"),
                "reason": payload.get("reason"),
                "affected_event_ids_sample": payload.get("affected_event_ids_sample", []),
                "created_at": ev.created_at,
            })
        return {"rollback_proposals": out, "count": len(out)}

    @app.get("/dms/{agent_id}")
    def fetch_dms(
        agent_id: str,
        since: Annotated[int | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict:
        """Kind 3 DMs where `agent_id` is sender OR recipient.

        The ciphertext is end-to-end encrypted (X25519 ECDH + XSalsa20-Poly1305
        per PROTOCOL (JP-redacted)4.4); only the two parties can decrypt. The relay
        cannot (JP-redacted) it merely shards the firehose by `p` tag for convenience.
        """
        if len(agent_id) != 64 or not all(c in "0123456789abcdef" for c in agent_id.lower()):
            raise HTTPException(status_code=400, detail="invalid agent_id format (expected 64-hex)")
        aid = agent_id.lower()
        # Sent DMs: authored by agent_id, kind 3
        sent = storage.query(kinds=[3], authors=[aid], since=since, limit=limit)
        # Received DMs: kind 3 with p=agent_id
        received = storage.query(kinds=[3], tag_filters=[("p", aid)], since=since, limit=limit)
        # Merge by id and sort desc
        merged = {ev.id: ev for ev in sent}
        for ev in received:
            merged.setdefault(ev.id, ev)
        out = sorted(merged.values(), key=lambda e: -e.created_at)[:limit]
        return {"agent_id": aid, "count": len(out), "dms": [e.model_dump() for e in out]}

    @app.get("/events/{event_id}/flags")
    def fetch_event_flags(event_id: str) -> dict:
        """List kind 7 moderation_flag events targeting this event.

        Used by consumers to display 'this content was flagged N times for
        category X' alongside an event (PROTOCOL (JP-redacted)4.8 transparency).
        """
        if len(event_id) != 64:
            raise HTTPException(status_code=400, detail="event_id must be 64 hex chars")
        flags = storage.flags_for(event_id.lower())
        return {
            "event_id": event_id.lower(),
            "flag_count": len(flags),
            "flags": flags,
        }

    @app.get("/stream")
    async def stream(
        request: Request,
        t: Annotated[str | None, Query()] = None,
    ) -> StreamingResponse:
        """Server-Sent Events live feed. Optional `t=room` filter."""

        def matches(ev: Event) -> bool:
            if t is None:
                return True
            for tag in ev.tags:
                if len(tag) >= 2 and tag[0] == "t" and tag[1] == t:
                    return True
            return False

        q = await bus.subscribe(matches)
        if q is None:
            raise HTTPException(
                status_code=503,
                detail="stream subscriber limit reached; retry shortly",
            )

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield f"data: {ev.model_dump_json()}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                await bus.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app
