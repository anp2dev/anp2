"""SQLite event storage (append-only, see PROTOCOL (JP-redacted)10 Persistence)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from .events import Event
from .trust import compute_trust, parse_votes

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

    # ---------------------------------------------------------------
    # B2 capability ontology (JP-redacted) see docs/research/CAPABILITY_ONTOLOGY.md
    # ---------------------------------------------------------------

    def capabilities_full(
        self,
        cap: str | None = None,
        min_trust: float | None = None,
        max_latency_ms: int | None = None,
        max_price_usd: float | None = None,
        supported_language: str | None = None,
        tag: str | None = None,
        sort_by: str | None = None,
        include_conflicts: bool = False,
        limit: int = 50,
        now: int | None = None,
    ) -> list[dict]:
        """Search capabilities by their **parsed** anp2.cap.v1 metadata blob.

        Walks the latest kind 4 event per agent_id (overwrite-type per
        PROTOCOL (JP-redacted)4.5), parses each `content.capabilities[]` entry, and
        applies the requested filters. First-claim canonicality ((JP-redacted)2.4) is
        derived live: per `name`, the earliest-`declared_at` provider is
        flagged `is_canonical=True`; others are returned only when
        `include_conflicts=True`.

        Returns dicts with the fields the `/api/capabilities/search`
        endpoint surfaces (see CAPABILITY_ONTOLOGY (JP-redacted)4.1). Sort by `score`
        (descending) where `score` is unit-normalized against the result set
        per `sort_by`. If no `sort_by`, results default to `trust` ranking.
        """
        t_now = int(time.time()) if now is None else int(now)

        # Step 1: latest kind 4 per agent (overwrite type semantics).
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT e.agent_id, e.content, e.created_at
                FROM events e
                JOIN (
                    SELECT agent_id, MAX(created_at) AS max_ts
                    FROM events
                    WHERE kind = 4
                    GROUP BY agent_id
                ) latest
                  ON latest.agent_id = e.agent_id
                 AND latest.max_ts   = e.created_at
                WHERE e.kind = 4
                """
            ).fetchall()
        finally:
            conn.close()

        # Step 2: trust score lookup (once).
        try:
            trust_index = {
                row["agent_id"]: row["weighted_score"]
                for row in self.trust_graph(now=t_now)
            }
        except Exception:
            trust_index = {}

        # Step 3: explode each event's content.capabilities[] and apply
        # per-row filters.
        candidates: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row["content"])
                caps = payload.get("capabilities") or []
            except (json.JSONDecodeError, TypeError):
                # Malformed kind 4 content => drop from structured search
                # but the event itself remains stored (immutability).
                continue
            for meta in caps:
                if not isinstance(meta, dict) or "name" not in meta:
                    continue
                name = meta["name"]
                # cap filter: exact or hierarchical-prefix match
                if cap and not (name == cap or name.startswith(cap + ".")):
                    continue
                constraints = meta.get("constraints") or {}
                pricing = meta.get("pricing") or {}

                # constraint filters
                if max_latency_ms is not None:
                    p95 = constraints.get("p95_latency_ms")
                    if p95 is None or p95 > max_latency_ms:
                        continue
                if supported_language is not None:
                    langs = constraints.get("supported_languages") or []
                    if supported_language not in langs:
                        continue
                if max_price_usd is not None:
                    amt = pricing.get("amount")
                    cur = pricing.get("currency", "USD")
                    if amt is None or cur != "USD" or amt > max_price_usd:
                        continue
                if tag is not None:
                    if tag not in (meta.get("tags") or []):
                        continue

                trust = float(trust_index.get(row["agent_id"], 0.0))
                if min_trust is not None and trust < min_trust:
                    continue

                candidates.append(
                    {
                        "name": name,
                        "version": meta.get("version", "1.0"),
                        "provider_agent_id": row["agent_id"],
                        "metadata": meta,
                        "trust_score": trust,
                        "declared_at": row["created_at"],
                    }
                )

        # Step 4: first-claim canonicality per name.
        earliest_per_name: dict[str, tuple[int, str]] = {}
        for c in candidates:
            key = c["name"]
            stamp = (c["declared_at"], c["provider_agent_id"])
            if key not in earliest_per_name or stamp < earliest_per_name[key]:
                earliest_per_name[key] = stamp
        for c in candidates:
            owner = earliest_per_name[c["name"]]
            c["is_canonical"] = (c["declared_at"], c["provider_agent_id"]) == owner

        if not include_conflicts:
            candidates = [c for c in candidates if c["is_canonical"]]

        # Step 5: scoring (JP-redacted) unit-normalized against the result set per
        # sort_by. Non-normative; callers can re-sort on raw fields.
        sort_key = sort_by or "trust"
        if sort_key == "trust":
            maxv = max((c["trust_score"] for c in candidates), default=0.0)
            for c in candidates:
                c["score"] = (c["trust_score"] / (maxv + 1.0)) if maxv > 0 else 0.0
            candidates.sort(key=lambda c: c["trust_score"], reverse=True)
        elif sort_key == "latency":
            def _p95(c: dict) -> float:
                v = (c["metadata"].get("constraints") or {}).get("p95_latency_ms")
                return float(v) if v is not None else float("inf")
            present = [_p95(c) for c in candidates if _p95(c) != float("inf")]
            maxv = max(present) if present else 1.0
            for c in candidates:
                v = _p95(c)
                c["score"] = (1.0 - v / maxv) if (maxv > 0 and v != float("inf")) else 0.0
            candidates.sort(key=lambda c: _p95(c))
        elif sort_key == "price":
            def _amt(c: dict) -> float:
                v = (c["metadata"].get("pricing") or {}).get("amount")
                return float(v) if v is not None else float("inf")
            present = [_amt(c) for c in candidates if _amt(c) != float("inf")]
            maxv = max(present) if present else 1.0
            for c in candidates:
                v = _amt(c)
                c["score"] = (1.0 - v / maxv) if (maxv > 0 and v != float("inf")) else 1.0
            candidates.sort(key=lambda c: _amt(c))
        else:
            for c in candidates:
                c["score"] = 0.0

        return candidates[:limit]

    def _load_all_votes(self) -> list:
        """Load every kind 6 trust_vote event as (voter, target, content, created_at) rows.

        Used by both trust_for() and trust_graph() so the underlying SQL is in
        one place. Returns sqlite3.Row dicts with keys: voter, target, content,
        created_at.
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT e.agent_id AS voter,
                       t.value     AS target,
                       e.content   AS content,
                       e.created_at AS created_at
                FROM events e
                JOIN tags   t ON t.event_id = e.id
                WHERE e.kind = 6
                  AND t.name = 'p'
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def trust_for(self, agent_id: str, now: int | None = None) -> dict:
        """Aggregate trust score for an agent (JP-redacted) backed by trust.py (trust.v1).

        Return shape extends the v0.1 minimal `{agent_id, score_in, voter_count,
        votes}` with `weighted_score` (iterative trust-weighted, sybil-dampened,
        time-decayed) and `iterations` (how many fixed-point passes ran).

        `score_in` is preserved as the *raw* decayed sum (no voter weighting) so
        existing clients see a sensible number. `weighted_score` is the new
        normative value per PROTOCOL (JP-redacted)6 / trust.v1.
        """
        t_now = int(time.time()) if now is None else int(now)
        votes = parse_votes(self._load_all_votes())
        result = compute_trust(votes, t_now)

        votes_for = result.votes_for.get(agent_id, [])
        return {
            "agent_id": agent_id,
            "score_in": result.raw_score.get(agent_id, 0.0),
            "weighted_score": result.weighted_score.get(agent_id, 0.0),
            "voter_count": result.voter_count.get(agent_id, 0),
            "iterations": result.iterations,
            "votes": votes_for,
        }

    def trust_graph(self, now: int | None = None) -> list[dict]:
        """Compute trust for every agent that has at least one incoming vote.

        Used by the recommendation feed (PROTOCOL (JP-redacted)12.5) and the /trust_graph
        endpoint. Sorted by weighted_score descending.
        """
        t_now = int(time.time()) if now is None else int(now)
        votes = parse_votes(self._load_all_votes())
        result = compute_trust(votes, t_now)

        targets = set(result.voter_count.keys())
        out = [
            {
                "agent_id": a,
                "weighted_score": result.weighted_score.get(a, 0.0),
                "raw_score": result.raw_score.get(a, 0.0),
                "voter_count": result.voter_count.get(a, 0),
            }
            for a in targets
        ]
        out.sort(key=lambda d: d["weighted_score"], reverse=True)
        return out

    def get_task_thread(self, task_id: str) -> list[Event]:
        """Return all events belonging to a task thread, sorted chronologically.

        Per PROTOCOL (JP-redacted)18.7, a task thread is:
            { event whose id == task_id }   (the kind 50 request itself)
          (JP-redacted) { event whose tags include ["e", task_id, <role>] for any role }   (kinds 51-55)

        Sorted by (created_at, id) ascending (JP-redacted) the same global ordering rule
        the relay uses everywhere else (PROTOCOL (JP-redacted)10.1).
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT e.*
                FROM events e
                LEFT JOIN tags t ON t.event_id = e.id
                WHERE e.id = ?
                   OR (t.name = 'e' AND t.value = ?)
                ORDER BY e.created_at ASC, e.id ASC
                """,
                (task_id, task_id),
            ).fetchall()
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
