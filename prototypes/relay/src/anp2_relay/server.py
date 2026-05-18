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
    limiter = _RateLimiter(RATE_LIMIT_WINDOW_SEC, RATE_LIMIT_MAX_EVENTS)

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

    @app.get("/stats")
    def stats() -> dict:
        return storage.stats()

    @app.get("/rooms")
    def rooms() -> dict:
        return {"rooms": storage.rooms()}

    @app.get("/capabilities")
    def capabilities() -> dict:
        return {"capabilities": storage.capabilities()}

    @app.get("/agents")
    def agents() -> dict:
        return {"agents": storage.agents()}

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
    def publish(event: Event) -> PublishResponse:
        now = int(time.time())
        shape_err = _validate_event_shape(event, now)
        if shape_err:
            raise HTTPException(status_code=400, detail=shape_err)
        ok, err = event.is_valid()
        if not ok:
            raise HTTPException(status_code=400, detail=err)
        if not limiter.allow(event.agent_id, now):
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
