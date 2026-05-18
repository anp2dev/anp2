"""In-memory pub/sub bus for SSE subscribers."""

from __future__ import annotations

import asyncio
from typing import Callable

from .events import Event

Predicate = Callable[[Event], bool]


class EventBus:
    def __init__(self) -> None:
        self._subs: list[tuple[Predicate, asyncio.Queue]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def subscribe(self, predicate: Predicate) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subs.append((predicate, q))
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs = [(p, queue) for p, queue in self._subs if queue is not q]

    def publish(self, event: Event) -> None:
        """Called from sync (storage thread). Safely hop to event loop."""
        if self._loop is None or not self._subs:
            return
        loop = self._loop

        def _dispatch() -> None:
            dead: list[asyncio.Queue] = []
            for predicate, q in list(self._subs):
                try:
                    if predicate(event):
                        try:
                            q.put_nowait(event)
                        except asyncio.QueueFull:
                            dead.append(q)
                except Exception:
                    dead.append(q)
            if dead:
                self._subs = [(p, queue) for p, queue in self._subs if queue not in dead]

        loop.call_soon_threadsafe(_dispatch)
