"""SQLite event storage (append-only, see PROTOCOL (JP-redacted)10 Persistence)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable

from .events import Event

OnInsert = Callable[[Event], None]


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    kind INTEGER NOT NULL,
    tags_json TEXT NOT NULL,
    content TEXT NOT NULL,
    sig TEXT NOT NULL,
    received_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_agent ON events(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS events_by_kind ON events(kind, created_at DESC);
CREATE INDEX IF NOT EXISTS events_by_created ON events(created_at DESC);

CREATE TABLE IF NOT EXISTS tags (
    event_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tags_lookup ON tags(name, value);
CREATE INDEX IF NOT EXISTS tags_by_event ON tags(event_id);
"""


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._listeners: list[OnInsert] = []
        self._init_db()

    def add_listener(self, fn: OnInsert) -> None:
        self._listeners.append(fn)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def insert(self, event: Event, received_at: int) -> bool:
        """Returns True if inserted, False if duplicate (id collision)."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO events(id, agent_id, created_at, kind, tags_json, content, sig, received_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.id,
                        event.agent_id,
                        event.created_at,
                        event.kind,
                        json.dumps(event.tags, separators=(",", ":")),
                        event.content,
                        event.sig,
                        received_at,
                    ),
                )
                for tag in event.tags:
                    if len(tag) >= 2:
                        conn.execute(
                            "INSERT INTO tags(event_id, name, value) VALUES(?, ?, ?)",
                            (event.id, tag[0], tag[1]),
                        )
            except sqlite3.IntegrityError:
                return False
            finally:
                conn.close()
        # notify listeners outside the lock
        for fn in self._listeners:
            try:
                fn(event)
            except Exception:
                pass
        return True

    def query(
        self,
        kinds: list[int] | None = None,
        authors: list[str] | None = None,
        since: int | None = None,
        until: int | None = None,
        tag_filters: list[tuple[str, str]] | None = None,
        limit: int = 100,
    ) -> list[Event]:
        clauses: list[str] = []
        params: list = []
        if kinds:
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if authors:
            clauses.append(f"agent_id IN ({','.join('?' * len(authors))})")
            params.extend(authors)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        if tag_filters:
            for name, value in tag_filters:
                clauses.append(
                    "id IN (SELECT event_id FROM tags WHERE name = ? AND value = ?)"
                )
                params.extend([name, value])

        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"SELECT * FROM events {where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        conn = self._conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [
                Event(
                    id=r["id"],
                    agent_id=r["agent_id"],
                    created_at=r["created_at"],
                    kind=r["kind"],
                    tags=json.loads(r["tags_json"]),
                    content=r["content"],
                    sig=r["sig"],
                )
                for r in rows
            ]
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            agents = conn.execute("SELECT COUNT(DISTINCT agent_id) FROM events").fetchone()[0]
            by_kind = {
                str(r["kind"]): r["c"]
                for r in conn.execute(
                    "SELECT kind, COUNT(*) AS c FROM events GROUP BY kind ORDER BY c DESC"
                ).fetchall()
            }
            return {"total_events": total, "unique_agents": agents, "by_kind": by_kind}
        finally:
            conn.close()

    def rooms(self) -> list[dict]:
        """List distinct topic tag values (= rooms) with activity stats."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT t.value AS room,
                       COUNT(DISTINCT t.event_id) AS messages,
                       MAX(e.created_at) AS last_at
                FROM tags t JOIN events e ON e.id = t.event_id
                WHERE t.name = 't'
                GROUP BY t.value
                ORDER BY last_at DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def capabilities(self) -> list[dict]:
        """Distinct declared capabilities (from kind 4 events via `cap` tag)."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT t.value AS capability,
                       COUNT(DISTINCT e.agent_id) AS providers,
                       MAX(e.created_at) AS last_declared
                FROM tags t JOIN events e ON e.id = t.event_id
                WHERE t.name = 'cap'
                GROUP BY t.value
                ORDER BY providers DESC, last_declared DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def agents(self) -> list[dict]:
        """List distinct agent_ids with their latest profile content and event count."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT
                    e.agent_id,
                    (SELECT content FROM events
                       WHERE agent_id = e.agent_id AND kind = 0
                       ORDER BY created_at DESC LIMIT 1) AS latest_profile,
                    COUNT(*) AS event_count,
                    MIN(created_at) AS first_seen,
                    MAX(created_at) AS last_seen
                FROM events e
                GROUP BY e.agent_id
                ORDER BY last_seen DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
