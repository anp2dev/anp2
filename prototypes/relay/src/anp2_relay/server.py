"""FastAPI relay server (Phase 0/1 minimal)."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from . import __version__
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

    return app
