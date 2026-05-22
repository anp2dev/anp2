"""In-memory pub/sub bus for SSE subscribers.

`_subs` is touched from two thread contexts: the event loop thread
(`subscribe` / `unsubscribe`) and the sync HTTP worker thread (`publish`,
which is called as a `storage.add_listener` callback). All mutations and
reads of `_subs` are guarded by `threading.Lock`. List swap idiom is used
so iteration always operates on a private snapshot.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Callable

from .events import Event

Predicate = Callable[[Event], bool]

# Hard cap on concurrent SSE subscribers. Each holds a 256-slot asyncio.Queue,
# so an unbounded subscriber count is a memory-exhaustion vector on a small
# host. Past the cap, subscribe() returns None and /stream answers 503.
MAX_SUBSCRIBERS = 512


class EventBus:
    def __init__(self) -> None:
        self._subs: list[tuple[Predicate, asyncio.Queue]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def subscribe(self, predicate: Predicate) -> asyncio.Queue | None:
        """Register a subscriber. Returns None if the concurrent-subscriber
        cap (MAX_SUBSCRIBERS) has been reached (JP-redacted) the caller answers 503."""
        with self._lock:
            if len(self._subs) >= MAX_SUBSCRIBERS:
                return None
            q: asyncio.Queue = asyncio.Queue(maxsize=256)
            self._subs = self._subs + [(predicate, q)]
            return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(p, queue) for p, queue in self._subs if queue is not q]

    def size(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, event: Event) -> None:
        """Called from sync (storage thread). Safely hop to event loop.

        We do NOT short-circuit on `_subs` emptiness here because that read
        would be unguarded vs. concurrent `subscribe`. Hopping a no-op onto
        the loop is cheap; the real snapshot + dispatch happens under the
        lock on the loop thread.
        """
        if self._loop is None:
            return
        loop = self._loop

        def _dispatch() -> None:
            with self._lock:
                snapshot = list(self._subs)
            dead: list[asyncio.Queue] = []
            for predicate, q in snapshot:
                try:
                    if predicate(event):
                        try:
                            q.put_nowait(event)
                        except asyncio.QueueFull:
                            dead.append(q)
                except Exception:
                    dead.append(q)
            if dead:
                with self._lock:
                    self._subs = [
                        (p, queue) for p, queue in self._subs if queue not in dead
                    ]

        try:
            loop.call_soon_threadsafe(_dispatch)
        except RuntimeError:
            # Loop already closed (e.g., during shutdown). Drop the event silently.
            return
