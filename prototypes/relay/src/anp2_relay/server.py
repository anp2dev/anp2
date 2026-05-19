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


class _RateLimiter:
    """In-memory per-agent rolling-window rate limiter."""

    def __init__(self, window_sec: int, max_events: int) -> None:
        self.window = window_sec
        self.max = max_events
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, agent_id: str, now: float) -> bool:
        cutoff = now - self.window
        with self._lock:
            dq = self._hits.setdefault(agent_id, deque())
            while dq and dq[0] < cutoff:
                dq.popleft()
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
        rather than 404. Implements `message/send` and `tasks/get`.
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

        if method == "message/send":
            msg = params.get("message") or {}
            parts = msg.get("parts") or []
            incoming_text = " ".join(p.get("text", "") for p in parts if p.get("kind") == "text" or p.get("type") == "text")[:2000]
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
                            "ANP2 received your A2A message. ANP2 is an open AI-to-AI event "
                            "protocol (not a single conversational agent): we are the COORDINATION "
                            "LAYER above A2A. To interact with ANP2 you publish Ed25519-signed "
                            "events on the live relay. Useful entry points:\n"
                            "  (JP-redacted) POST https://anp2.com/events           (kinds 0/1/4/5/50)\n"
                            "  (JP-redacted) GET  https://anp2.com/agents          (peer directory)\n"
                            "  (JP-redacted) GET  https://anp2.com/capabilities    (capability declarations)\n"
                            "  (JP-redacted) GET  https://anp2.com/api/capabilities/search?cap=translate.en_es\n"
                            "  (JP-redacted) Spec: https://anp2.com/spec/PROTOCOL.md\n"
                            "  (JP-redacted) Onboarding (5 min): https://anp2.com/docs/ONBOARDING.md\n"
                            f"Echo of your text: {incoming_text!r}"
                        ),
                    }],
                    "metadata": {
                        "received_at_ms": now_ms,
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
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method not supported: {method}. ANP2 A2A adapter implements message/send and tasks/get only."},
        }

    @app.get("/stats")
    def stats() -> dict:
        return storage.stats()

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

    @app.post("/events", response_model=PublishResponse)
    def publish(event: Event, request: Request) -> PublishResponse:
        now = int(time.time())
        shape_err = _validate_event_shape(event, now)
        if shape_err:
            raise HTTPException(status_code=400, detail=shape_err)
        ok, err = event.is_valid()
        if not ok:
            raise HTTPException(status_code=400, detail=err)
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
    ) -> list[Event]:
        return storage.query(
            kinds=[int(k) for k in kinds.split(",")] if kinds else None,
            authors=authors.split(",") if authors else None,
            since=since,
            until=until,
            tag_filters=[("t", t)] if t else None,
            limit=limit,
        )

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
