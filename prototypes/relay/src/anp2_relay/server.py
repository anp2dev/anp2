"""FastAPI relay server (Phase 0/1)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import __version__
from .bus import EventBus
from .events import Event
from .storage import Storage


class PublishResponse(BaseModel):
    id: str
    accepted: bool


def create_app(storage: Storage) -> FastAPI:
    app = FastAPI(
        title="ANP2 Relay",
        version=__version__,
        description="ANP reference relay (Phase 0/1, private)",
    )
    bus = EventBus()
    storage.add_listener(bus.publish)

    @app.on_event("startup")
    async def _startup() -> None:
        bus.attach_loop(asyncio.get_running_loop())

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

    @app.post("/events", response_model=PublishResponse)
    def publish(event: Event) -> PublishResponse:
        ok, err = event.is_valid()
        if not ok:
            raise HTTPException(status_code=400, detail=err)
        storage.insert(event, received_at=int(time.time()))
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
