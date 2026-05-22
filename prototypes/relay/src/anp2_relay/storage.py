"""SQLite event storage (append-only, see PROTOCOL (JP-redacted)10 Persistence)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
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
        # Kind-11 health beats are ephemeral infra signals, not protocol
        # content: held in a rolling in-memory window (bounded by HEALTH_7D),
        # never written to the append-only event log. See record_beat().
        self._health_beats: dict[str, deque] = {}
        self._health_lock = threading.Lock()
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

    # PROTOCOL (JP-redacted)4.8 + (JP-redacted)7.4 (JP-redacted) moderation auto-hide.
    # Hidden when EITHER:
    #   (a) (JP-redacted) MOD_HIDE_THRESHOLD distinct flaggers (simple count, Phase 0/1)
    #   (b) (JP-redacted) flagger_trust_weight (JP-redacted) MOD_HIDE_TRUST_WEIGHT ((JP-redacted)7.4 override)
    # A single high-trust flagger can hide content; a swarm of zero-trust
    # flaggers cannot. raw_score (JP-redacted) 1.0 is roughly "the relay accepts your
    # trust signal" (JP-redacted) equivalent to one mid-tier honest voter.
    MOD_HIDE_THRESHOLD = 3
    MOD_HIDE_TRUST_WEIGHT = 1.0

    def query(
        self,
        kinds: list[int] | None = None,
        exclude_kinds: list[int] | None = None,
        authors: list[str] | None = None,
        since: int | None = None,
        until: int | None = None,
        tag_filters: list[tuple[str, str]] | None = None,
        limit: int = 100,
        include_revoked: bool = False,
        include_hidden: bool = False,
        branch: str | None = None,
    ) -> list[Event]:
        """Query events. PROTOCOL (JP-redacted)11.3.3 branch filter:
          - branch=None or 'main': events without a branch tag OR with branch=main
          - branch='all': no branch filter at all
          - branch=<other>: events that carry a matching branch tag
          - branch='a,b' comma-separated: union over branches
        """
        clauses: list[str] = []
        params: list = []
        if branch and branch != "all":
            branch_ids = [b.strip() for b in branch.split(",") if b.strip()]
            # PROTOCOL (JP-redacted)11.3.1 (JP-redacted) pre-rollback-<id8> branches are implicit
            # snapshots: they include every event with created_at (JP-redacted) the
            # target checkpoint's created_at. We resolve those here.
            implicit_filters: list[str] = []
            explicit_branches: list[str] = []
            for bid in branch_ids:
                if bid.startswith("pre-rollback-"):
                    # find the proposal event whose id starts with the suffix
                    suffix = bid[len("pre-rollback-"):]
                    if not suffix or not all(c in "0123456789abcdef" for c in suffix.lower()):
                        explicit_branches.append(bid)
                        continue
                    conn = self._conn()
                    try:
                        prop = conn.execute(
                            "SELECT content, tags_json FROM events WHERE kind = 13 AND id LIKE ? LIMIT 1",
                            (suffix.lower() + "%",),
                        ).fetchone()
                    finally:
                        conn.close()
                    if prop:
                        try:
                            tags_p = json.loads(prop["tags_json"])
                            e_tag = next((t for t in tags_p if len(t) >= 2 and t[0] == "e"), None)
                            cp_id = e_tag[1] if e_tag else None
                        except (ValueError, TypeError):
                            cp_id = None
                        if cp_id:
                            cp_row = self._conn().execute(
                                "SELECT created_at FROM events WHERE id = ? LIMIT 1", (cp_id,)
                            ).fetchone()
                            if cp_row:
                                implicit_filters.append("events.created_at <= ?")
                                params.append(int(cp_row["created_at"]))
                else:
                    explicit_branches.append(bid)
            if explicit_branches == ["main"]:
                clauses.append(
                    "NOT EXISTS ("
                    "  SELECT 1 FROM tags bt WHERE bt.event_id = events.id "
                    "  AND bt.name = 'branch' AND bt.value != 'main'"
                    ")"
                )
            elif explicit_branches:
                placeholders = ",".join("?" * len(explicit_branches))
                clauses.append(
                    "EXISTS ("
                    f"  SELECT 1 FROM tags bt WHERE bt.event_id = events.id "
                    f"  AND bt.name = 'branch' AND bt.value IN ({placeholders})"
                    ")"
                )
                params.extend(explicit_branches)
            # implicit pre-rollback filters use OR across the implicit set;
            # combined as an additional AND in WHERE (a single implicit
            # branch == created_at (JP-redacted) snapshot)
            if implicit_filters:
                clauses.append("(" + " OR ".join(implicit_filters) + ")")
        if kinds:
            clauses.append(f"events.kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if exclude_kinds:
            clauses.append(f"events.kind NOT IN ({','.join('?' * len(exclude_kinds))})")
            params.extend(exclude_kinds)
        if authors:
            clauses.append(f"events.agent_id IN ({','.join('?' * len(authors))})")
            params.extend(authors)
        if since is not None:
            clauses.append("events.created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("events.created_at <= ?")
            params.append(until)
        if tag_filters:
            for name, value in tag_filters:
                clauses.append(
                    "events.id IN (SELECT event_id FROM tags WHERE name = ? AND value = ?)"
                )
                params.extend([name, value])

        # PROTOCOL (JP-redacted)4.9 (JP-redacted) drop events that have been revoked by their author.
        # The revoke (kind 9) must point at the event via `e` tag AND share
        # the author. Kind 9 events themselves are NOT hidden (audit trail).
        if not include_revoked:
            clauses.append(
                "NOT EXISTS ("
                "  SELECT 1 FROM events r "
                "  JOIN tags rt ON rt.event_id = r.id "
                "  WHERE r.kind = 9 AND r.agent_id = events.agent_id "
                "    AND rt.name = 'e' AND rt.value = events.id"
                ")"
            )

        # PROTOCOL (JP-redacted)4.8 (JP-redacted) auto-hide events with (JP-redacted) MOD_HIDE_THRESHOLD distinct
        # moderation_flag reports. The flag events themselves stay visible.
        # Outer parens are mandatory: the `kind = 7 OR (...)` would otherwise
        # bind tighter than the preceding AND clauses and let every event
        # with count<3 through, regardless of `kinds=...` filters.
        if not include_hidden:
            clauses.append(
                "(events.kind = 7 OR ("
                "  SELECT COUNT(DISTINCT f.agent_id) FROM events f "
                "  JOIN tags ft ON ft.event_id = f.id "
                "  WHERE f.kind = 7 AND ft.name = 'e' AND ft.value = events.id"
                f") < {self.MOD_HIDE_THRESHOLD})"
            )

        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"SELECT events.* FROM events {where} ORDER BY events.created_at DESC LIMIT ?"
        params.append(limit)

        conn = self._conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            evs = [
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
        # PROTOCOL (JP-redacted)7.4 (JP-redacted) high-trust override. Trust-weighted aggregation
        # over already-SQL-filtered rows; cheap because trust_map is
        # computed once and the candidate set is already tight.
        if not include_hidden and evs:
            try:
                trust_map = {a["agent_id"]: max(0.0, a.get("weighted_score", 0.0) or 0.0)
                             for a in self.trust_graph()}
                evs = [
                    ev for ev in evs
                    if ev.kind == 7 or self.flag_weight_for(ev.id, trust_map) < self.MOD_HIDE_TRUST_WEIGHT
                ]
            except Exception:
                # Trust graph may be empty during bootstrap; skip override.
                pass
        return evs

    # PROTOCOL (JP-redacted)11.3 (JP-redacted) quiet period (6h) inside which kind 13 cosigners
    # may push past the 2/3 trust supermajority that activates a rollback.
    ROLLBACK_QUIET_PERIOD_SEC = 6 * 3600
    ROLLBACK_THRESHOLD_FRAC = 0.67

    def phase_state(self, seed_multisig_pubkeys: set[str]) -> dict:
        """PROTOCOL (JP-redacted)14.7 (JP-redacted) current governance phase.

        Phase 0/1 (seed-multisig + AI moderation) flips to Phase 3+
        (full AI self-rule) when a kind 21 self_destruct event from any
        seed-multisig key reaches its `effective_at` timestamp. Until then we
        report `phase` as 0/1.
        """
        t_now = int(time.time())
        if not seed_multisig_pubkeys:
            return {"phase": "0/1", "seed_multisig_active": True, "destruction_event_id": None,
                    "effective_at": None}
        conn = self._conn()
        try:
            rows = conn.execute(
                f"SELECT id, content FROM events "
                f"WHERE kind = 21 AND agent_id IN ({','.join('?' * len(seed_multisig_pubkeys))}) "
                f"ORDER BY created_at DESC",
                tuple(seed_multisig_pubkeys),
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            try:
                p = json.loads(r["content"])
                eff = int(p.get("effective_at") or 0)
                if eff and t_now >= eff:
                    return {
                        "phase": "3+",
                        "seed_multisig_active": False,
                        "destruction_event_id": r["id"],
                        "effective_at": eff,
                    }
            except (ValueError, TypeError):
                pass
        return {"phase": "0/1", "seed_multisig_active": True, "destruction_event_id": None,
                "effective_at": None}

    def schema_registry(self) -> list[dict]:
        """PROTOCOL (JP-redacted)9.3 + (JP-redacted)14.5 (JP-redacted) registered Tier-3 intent schemas.

        Aggregates from kind 20 PIPs (status tag) and from kind 1000-1999
        events that declare their `s` tag schema name. Returns one entry
        per unique schema name with first/last-seen timestamps and the
        PIP event id (if any) that introduced it.
        """
        conn = self._conn()
        try:
            # observed Tier-3 schemas in actual use
            usage = conn.execute(
                """
                SELECT t.value AS schema_name,
                       MIN(e.created_at) AS first_seen,
                       MAX(e.created_at) AS last_seen,
                       COUNT(DISTINCT e.agent_id) AS unique_publishers,
                       COUNT(*) AS event_count
                FROM events e
                JOIN tags t ON t.event_id = e.id
                WHERE e.kind BETWEEN 1000 AND 1999
                  AND t.name = 's'
                GROUP BY t.value
                ORDER BY event_count DESC
                """
            ).fetchall()
            # PIPs that may introduce schemas (kind 20 with `s` tag)
            pips = conn.execute(
                """
                SELECT e.id, e.agent_id, e.created_at, e.content, t.value AS schema_ref
                FROM events e
                JOIN tags t ON t.event_id = e.id
                WHERE e.kind = 20 AND t.name = 's'
                """
            ).fetchall()
        finally:
            conn.close()
        pip_index = {p["schema_ref"]: p for p in pips}
        out = []
        for u in usage:
            pip = pip_index.get(u["schema_name"])
            out.append({
                "schema": u["schema_name"],
                "event_count": u["event_count"],
                "unique_publishers": u["unique_publishers"],
                "first_seen": u["first_seen"],
                "last_seen": u["last_seen"],
                "introduced_by_pip": pip["id"] if pip else None,
                "pip_proposer": pip["agent_id"] if pip else None,
            })
        return out

    def sovereign_state(self, sovereign_pubkeys: set[str]) -> dict:
        """PROTOCOL (JP-redacted)15 (JP-redacted) current sovereign override state.

        Replays all kind-30 events from sovereign keys, in created_at order.
        The latest non-unfreeze act of each kind wins. Returns a single dict:

          {
            "frozen": bool,
            "banned_agents": set[str],
            "revoked_relays": set[str],
            "appointed_stewards": list[str],
            "shutdown": bool,
            "last_act_at": int | None,
          }
        """
        if not sovereign_pubkeys:
            return {"frozen": False, "banned_agents": set(), "revoked_relays": set(),
                    "appointed_stewards": [], "shutdown": False, "last_act_at": None}
        conn = self._conn()
        try:
            rows = conn.execute(
                f"SELECT id, agent_id, created_at, content, tags_json FROM events "
                f"WHERE kind = 30 AND agent_id IN ({','.join('?' * len(sovereign_pubkeys))}) "
                f"ORDER BY created_at ASC",
                tuple(sovereign_pubkeys),
            ).fetchall()
        finally:
            conn.close()
        st = {"frozen": False, "banned_agents": set(), "revoked_relays": set(),
              "appointed_stewards": [], "shutdown": False, "last_act_at": None}
        for r in rows:
            try:
                payload = json.loads(r["content"])
                tags = json.loads(r["tags_json"])
            except (ValueError, TypeError):
                continue
            act = payload.get("act")
            st["last_act_at"] = r["created_at"]
            if act == "freeze_network":
                st["frozen"] = True
            elif act == "unfreeze":
                st["frozen"] = False
            elif act == "ban_agent":
                for t in tags:
                    if len(t) >= 2 and t[0] == "p":
                        st["banned_agents"].add(t[1].lower())
            elif act == "revoke_relay":
                for t in tags:
                    if len(t) >= 2 and t[0] == "relay":
                        st["revoked_relays"].add(t[1])
            elif act == "appoint_steward":
                for t in tags:
                    if len(t) >= 2 and t[0] == "p" and t[1] not in st["appointed_stewards"]:
                        st["appointed_stewards"].append(t[1])
            elif act == "shutdown_protocol":
                st["shutdown"] = True
            elif act == "rollback_to":
                # Tag with `e:<checkpoint>`; the active_rollbacks() pipeline
                # exposes this as an admin-initiated rollback path.
                pass
        return st

    def active_rollbacks(self) -> list[dict]:
        """Determine which kind 13 rollback proposals have crossed the
        2/3 trust-weighted supermajority within the (JP-redacted)11.3 quiet period.

        Each proposal points at a kind 12 checkpoint; cosigners attach via
        `cosign` tags on the proposal (3-tuple form). The activated rollback
        creates a `pre-rollback-<id8>` branch (PROTOCOL (JP-redacted)11.3.1).
        """
        t_now = int(time.time())
        proposals = self.query(kinds=[13], limit=200)
        if not proposals:
            return []
        # trust-weighted denominator: sum of all weighted_score values
        graph = self.trust_graph()
        total_weight = sum(max(0.0, a.get("weighted_score", 0.0) or 0.0) for a in graph) or 1.0
        trust_map = {a["agent_id"]: max(0.0, a.get("weighted_score", 0.0) or 0.0) for a in graph}
        out = []
        for prop in proposals:
            age = t_now - prop.created_at
            if age > self.ROLLBACK_QUIET_PERIOD_SEC:
                # outside quiet period; activation decided at the boundary
                pass  # keep evaluating; status reflects past activation too
            cosign_agents = [t[1] for t in prop.tags if len(t) >= 3 and t[0] == "cosign"]
            # include proposer themselves as automatic cosigner
            cosign_agents = list({prop.agent_id, *cosign_agents})
            cosign_weight = sum(trust_map.get(a, 0.0) for a in cosign_agents)
            ratio = cosign_weight / total_weight
            activated = ratio >= self.ROLLBACK_THRESHOLD_FRAC
            out.append({
                "proposal_event_id": prop.id,
                "proposer": prop.agent_id,
                "target_checkpoint_event_id": next(
                    (t[1] for t in prop.tags if len(t) >= 2 and t[0] == "e"), None
                ),
                "cosigner_count": len(cosign_agents),
                "cosign_weight": cosign_weight,
                "ratio": ratio,
                "threshold": self.ROLLBACK_THRESHOLD_FRAC,
                "activated": activated,
                "created_at": prop.created_at,
                "quiet_period_remaining_sec": max(0, self.ROLLBACK_QUIET_PERIOD_SEC - age),
            })
        return out

    def branches(self) -> list[dict]:
        """PROTOCOL (JP-redacted)11.3.4 (JP-redacted) branch metadata.

        v0.1 surfaces:
          - `main` (the default consensus branch),
          - `pre-rollback-<id8>` for each activated rollback (auto-derived
            from `active_rollbacks()`; no explicit `branch` tag needed),
          - `b-<id8>` for any voluntary branch declared via a kind 1+ event
            that carries a `["branch","b-..."]` tag.
        """
        out = [{"id": "main", "head_event_id": None, "event_count": 0, "trust_weight_pct": 0.0}]
        # PROTOCOL (JP-redacted)11.3.1 (JP-redacted) pre-rollback-<id8> branches are *implicitly*
        # created at rollback activation. They don't require an explicit
        # `branch` tag on the historical events; the relay derives the
        # branch from the proposal event id.
        for prop in self.active_rollbacks():
            if not prop["activated"]:
                continue
            rid = prop["proposal_event_id"][:8]
            cp_id = prop.get("target_checkpoint_event_id")
            cp_at = None
            if cp_id:
                cp_ev = self.get_event(cp_id)
                cp_at = cp_ev.created_at if cp_ev else None
            # event_count for pre-rollback branch = count of events with
            # created_at <= cp_at (the snapshot moment).
            cnt = 0
            if cp_at:
                conn = self._conn()
                try:
                    cnt = conn.execute(
                        "SELECT COUNT(*) AS c FROM events WHERE created_at <= ?",
                        (cp_at,),
                    ).fetchone()["c"] or 0
                finally:
                    conn.close()
            out.append({
                "id": f"pre-rollback-{rid}",
                "head_event_id": cp_id,
                "event_count": cnt,
                "trust_weight_pct": round(100.0 * prop["ratio"], 2),
                "created_from": "rollback",
                "rollback_proposal": prop["proposal_event_id"],
                "activated_at": prop["created_at"],
            })
        conn = self._conn()
        try:
            # Voluntary forks via explicit branch tag (b-* or other)
            rows = conn.execute(
                """
                SELECT t.value AS branch_id,
                       COUNT(DISTINCT t.event_id) AS event_count,
                       MAX(e.created_at) AS last_at
                FROM tags t JOIN events e ON e.id = t.event_id
                WHERE t.name = 'branch' AND t.value != 'main'
                  AND t.value NOT LIKE 'pre-rollback-%'
                GROUP BY t.value
                """
            ).fetchall()
            total_events = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"] or 1
            for r in rows:
                out.append({
                    "id": r["branch_id"],
                    "head_event_id": None,
                    "event_count": r["event_count"],
                    "trust_weight_pct": round(100.0 * r["event_count"] / total_events, 2),
                    "created_from": "voluntary_fork",
                })
            main_rows = conn.execute(
                """
                SELECT COUNT(*) AS c FROM events
                WHERE NOT EXISTS (
                  SELECT 1 FROM tags WHERE tags.event_id = events.id
                    AND tags.name = 'branch' AND tags.value != 'main'
                )
                """
            ).fetchone()
            out[0]["event_count"] = main_rows["c"]
            out[0]["trust_weight_pct"] = round(100.0 * main_rows["c"] / total_events, 2)
        finally:
            conn.close()
        return out

    def citations_for(self, event_id: str, direction: str = "incoming") -> list[dict]:
        """Citation graph (PROTOCOL (JP-redacted)12.4).

        - incoming: kind 5 events that reference `event_id` via `derived_from`
          in their content (or via an `e` tag with role 'derived').
        - outgoing: events that `event_id` itself references.
        """
        conn = self._conn()
        try:
            if direction == "incoming":
                rows = conn.execute(
                    """
                    SELECT DISTINCT e.id, e.agent_id, e.created_at, e.content
                    FROM events e
                    JOIN tags t ON t.event_id = e.id
                    WHERE e.kind = 5 AND t.name = 'e' AND t.value = ?
                    ORDER BY e.created_at DESC
                    LIMIT 200
                    """,
                    (event_id,),
                ).fetchall()
            else:  # outgoing
                source = conn.execute(
                    "SELECT content FROM events WHERE id = ?", (event_id,)
                ).fetchone()
                if not source:
                    return []
                try:
                    payload = json.loads(source["content"])
                    df = payload.get("derived_from") or []
                    if isinstance(df, str):
                        df = [df]
                except (ValueError, TypeError):
                    df = []
                if not df:
                    return []
                placeholders = ",".join("?" * len(df))
                rows = conn.execute(
                    f"SELECT id, agent_id, created_at, content FROM events WHERE id IN ({placeholders})",
                    df,
                ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def beacons_active(self, now: int | None = None) -> list[dict]:
        """Return kind 15 beacons whose TTL has not expired (PROTOCOL (JP-redacted)12.1)."""
        t_now = int(time.time()) if now is None else int(now)
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, agent_id, created_at, content, tags_json "
                "FROM events WHERE kind = 15 "
                "ORDER BY created_at DESC LIMIT 500"
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            try:
                p = json.loads(r["content"])
                ttl = int(p.get("ttl_sec") or 0)
            except (ValueError, TypeError):
                continue
            if r["created_at"] + ttl < t_now:
                continue
            d = dict(r)
            d["payload"] = p
            d["expires_at"] = r["created_at"] + ttl
            d.pop("content", None)
            try:
                d["tags"] = json.loads(d.pop("tags_json"))
            except (ValueError, TypeError):
                d["tags"] = []
                d.pop("tags_json", None)
            out.append(d)
        return out

    def subscriptions_of(self, agent_id: str) -> list[dict]:
        """kind 8 subscription_extension targets followed by `agent_id` (latest per p)."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT t.value AS target, MAX(e.created_at) AS at, e.id
                FROM events e
                JOIN tags t ON t.event_id = e.id
                WHERE e.kind = 8 AND e.agent_id = ? AND t.name = 'p'
                GROUP BY t.value
                ORDER BY at DESC
                """,
                (agent_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def funding_for(self, agent_id: str, window_sec: int = 30 * 86400) -> dict:
        """kind 17 donation aggregation for `agent_id` (PROTOCOL (JP-redacted)13.4).

        Returns counts split by:
          - unverified (relay-stamped on accept; (JP-redacted)13.3.2)
          - third-party verified: kind 17 with type=verification cross-
            referencing the original via `verified_by_external` tag, where
            the verifier's weighted_score >= 1.0 (trust-weighted (JP-redacted)13.3.4)
        """
        t_now = int(time.time())
        since = t_now - window_sec
        conn = self._conn()
        try:
            # received
            rec = conn.execute(
                """
                SELECT e.id, e.agent_id AS donor, e.content, e.created_at
                FROM events e
                JOIN tags t ON t.event_id = e.id
                WHERE e.kind = 17 AND t.name = 'p' AND t.value = ?
                  AND e.created_at >= ?
                """,
                (agent_id, since),
            ).fetchall()
            addr_row = conn.execute(
                """
                SELECT content FROM events
                WHERE kind = 16 AND agent_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
            # third-party verifications: kind 17 with type=verification +
            # `verified_by_external` tag pointing at an original donation
            verifications = conn.execute(
                """
                SELECT e.agent_id AS verifier, e.content, e.created_at,
                       (SELECT t2.value FROM tags t2 WHERE t2.event_id = e.id
                        AND t2.name = 'verified_by_external' LIMIT 1) AS target_event
                FROM events e
                WHERE e.kind = 17
                """
            ).fetchall()
        finally:
            conn.close()
        addresses = []
        if addr_row:
            try:
                addresses = json.loads(addr_row["content"]).get("addresses", [])
            except (ValueError, TypeError):
                pass
        trust_map = {a["agent_id"]: max(0.0, a.get("weighted_score", 0.0) or 0.0)
                     for a in self.trust_graph()}
        VERIFIER_TRUST_THRESHOLD = 1.0
        verified_event_ids: set[str] = set()
        for v in verifications:
            try:
                p = json.loads(v["content"])
                if p.get("type") != "verification":
                    continue
                target = v["target_event"]
                if not target:
                    continue
                if trust_map.get(v["verifier"], 0.0) >= VERIFIER_TRUST_THRESHOLD:
                    verified_event_ids.add(target)
            except (ValueError, TypeError):
                pass
        donors = set()
        unverified = 0
        verified = 0
        for r in rec:
            donors.add(r["donor"])
            if r["id"] in verified_event_ids:
                verified += 1
            else:
                unverified += 1
        return {
            "agent_id": agent_id,
            "window_sec": window_sec,
            "addresses": addresses,
            "received_count": len(rec),
            "received_unique_donors": len(donors),
            "received_verified_count": verified,
            "received_unverified_count": unverified,
            "verifier_trust_threshold": VERIFIER_TRUST_THRESHOLD,
        }

    EMBED_DIM = 256  # PROTOCOL (JP-redacted)12.3 (JP-redacted) hashed bag-of-tokens dim for v0.1 embeddings

    def _agent_embedding(self, agent_id: str, window_sec: int = 30 * 86400) -> list[float]:
        """v0.1 embedding: hashed bag-of-tokens cosine vector over the
        agent's recent kind 1 + 5 content. Deterministic, no external
        model. PROTOCOL (JP-redacted)12.3 acknowledges a real embedding model is
        Phase 2+ via off-relay indexer AIs; this is the in-relay fallback.
        """
        import hashlib
        since = int(time.time()) - window_sec
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT content FROM events "
                "WHERE agent_id = ? AND kind IN (1, 5) AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT 100",
                (agent_id, since),
            ).fetchall()
        finally:
            conn.close()
        vec = [0.0] * self.EMBED_DIM
        for r in rows:
            text = r["content"] or ""
            # Lowercase + whitespace tokenize; cheap but enough for cosine.
            for tok in text.lower().split():
                tok = tok.strip(".,!?;:\"'()[]{}/<>")
                if not tok:
                    continue
                h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest()[:8], 16)
                vec[h % self.EMBED_DIM] += 1.0
        # L2 normalize
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0:
            return vec
        return [v / norm for v in vec]

    def _discoverability(self, agent_id: str) -> str:
        """PROTOCOL (JP-redacted)12.8 (JP-redacted) read the profile.discoverability setting.

        Returns one of {public, topic_only, invite_only}. Defaults to
        public when the profile has no field or the agent has no kind 0.
        """
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT content FROM events WHERE agent_id = ? AND kind = 0 "
                "ORDER BY created_at DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return "public"
        try:
            d = json.loads(row["content"]).get("discoverability") or "public"
            if d in ("public", "topic_only", "invite_only"):
                return d
        except (ValueError, TypeError):
            pass
        return "public"

    def neighbors_embedding(self, agent_id: str, k: int = 20) -> list[dict]:
        """PROTOCOL (JP-redacted)12.3 (JP-redacted) semantic neighborhood by cosine similarity
        over `_agent_embedding`. Returns the top-k agents by sim score.
        """
        own_vec = self._agent_embedding(agent_id)
        if sum(v * v for v in own_vec) == 0:
            return []
        conn = self._conn()
        try:
            others = conn.execute(
                "SELECT DISTINCT agent_id FROM events WHERE agent_id != ? LIMIT 500",
                (agent_id,),
            ).fetchall()
        finally:
            conn.close()
        scored = []
        for r in others:
            other_id = r["agent_id"]
            # PROTOCOL (JP-redacted)12.8 (JP-redacted) invite_only profiles never appear in
            # neighborhood/copresence results; topic_only shows only
            # when topic overlap exists (we approximate "topic overlap"
            # as embedding sim > 0).
            if self._discoverability(other_id) == "invite_only":
                continue
            other_vec = self._agent_embedding(other_id)
            sim = sum(a * b for a, b in zip(own_vec, other_vec))
            if sim > 0:
                # Sample a few topics for the response payload
                conn = self._conn()
                try:
                    tops = conn.execute(
                        "SELECT DISTINCT t.value FROM events e JOIN tags t ON t.event_id = e.id "
                        "WHERE e.agent_id = ? AND t.name = 't' LIMIT 5",
                        (other_id,),
                    ).fetchall()
                finally:
                    conn.close()
                scored.append({
                    "agent_id": other_id,
                    "sim": round(sim, 4),
                    "sample_topics": [t["value"] for t in tops],
                })
        scored.sort(key=lambda x: -x["sim"])
        return scored[:k]

    def copresence_for(self, agent_id: str, window_sec: int = 7 * 86400) -> list[dict]:
        """Co-presence index (PROTOCOL (JP-redacted)12.2).

        Other agents seen sharing a thread root, topic tag, capability, or
        knowledge_claim citation with `agent_id` within `window_sec`.
        """
        since = int(time.time()) - window_sec
        conn = self._conn()
        try:
            # Topic tag overlap
            topics = conn.execute(
                """
                SELECT DISTINCT t.value FROM events e
                JOIN tags t ON t.event_id = e.id
                WHERE e.agent_id = ? AND t.name = 't' AND e.created_at >= ?
                """,
                (agent_id, since),
            ).fetchall()
            topic_vals = [r["value"] for r in topics]
            scores: dict[str, dict] = {}
            for topic in topic_vals:
                rows = conn.execute(
                    """
                    SELECT DISTINCT e.agent_id FROM events e
                    JOIN tags t ON t.event_id = e.id
                    WHERE t.name = 't' AND t.value = ? AND e.agent_id != ?
                      AND e.created_at >= ?
                    LIMIT 50
                    """,
                    (topic, agent_id, since),
                ).fetchall()
                for r in rows:
                    # PROTOCOL (JP-redacted)12.8 (JP-redacted) invite_only opts out of copresence.
                    if self._discoverability(r["agent_id"]) == "invite_only":
                        continue
                    s = scores.setdefault(r["agent_id"], {"agent_id": r["agent_id"], "contexts": [], "score": 0.0})
                    s["contexts"].append({"type": "topic", "ref": topic})
                    s["score"] += 0.3
            # Capability overlap
            cap_rows = conn.execute(
                """
                SELECT DISTINCT t.value FROM events e
                JOIN tags t ON t.event_id = e.id
                WHERE e.agent_id = ? AND t.name = 'cap'
                """,
                (agent_id,),
            ).fetchall()
            for cr in cap_rows:
                rows = conn.execute(
                    """
                    SELECT DISTINCT e.agent_id FROM events e
                    JOIN tags t ON t.event_id = e.id
                    WHERE t.name = 'cap' AND t.value = ? AND e.agent_id != ?
                    LIMIT 50
                    """,
                    (cr["value"], agent_id),
                ).fetchall()
                for r in rows:
                    if self._discoverability(r["agent_id"]) == "invite_only":
                        continue
                    s = scores.setdefault(r["agent_id"], {"agent_id": r["agent_id"], "contexts": [], "score": 0.0})
                    s["contexts"].append({"type": "capability", "ref": cr["value"]})
                    s["score"] += 0.5
        finally:
            conn.close()
        # Cap score at 1.0; sort desc
        for s in scores.values():
            s["score"] = min(1.0, s["score"])
        return sorted(scores.values(), key=lambda x: -x["score"])

    def flag_weight_for(self, event_id: str, trust_map: dict[str, float] | None = None) -> float:
        """PROTOCOL (JP-redacted)7.4 (JP-redacted) sum of flagger trust weights against `event_id`.

        Caller can pass a precomputed `trust_map` to avoid per-call trust
        recompute when scanning many events.
        """
        if trust_map is None:
            trust_map = {a["agent_id"]: max(0.0, a.get("weighted_score", 0.0) or 0.0)
                         for a in self.trust_graph()}
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT f.agent_id
                FROM events f JOIN tags ft ON ft.event_id = f.id
                WHERE f.kind = 7 AND ft.name = 'e' AND ft.value = ?
                """,
                (event_id,),
            ).fetchall()
        finally:
            conn.close()
        return sum(trust_map.get(r["agent_id"], 0.0) for r in rows)

    def flags_for(self, event_id: str) -> list[dict]:
        """Return the kind 7 moderation_flag events targeting `event_id`."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT f.id, f.agent_id, f.created_at, f.content
                FROM events f
                JOIN tags ft ON ft.event_id = f.id
                WHERE f.kind = 7 AND ft.name = 'e' AND ft.value = ?
                ORDER BY f.created_at DESC
                """,
                (event_id,),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("content"))
            except (ValueError, TypeError):
                d["payload"] = {}
                d.pop("content", None)
            out.append(d)
        return out

    def is_revoked(self, event_id: str) -> bool:
        """True iff the event's author has published a kind 9 pointing at it."""
        conn = self._conn()
        try:
            row = conn.execute(
                """
                SELECT 1 FROM events orig
                WHERE orig.id = ?
                AND EXISTS (
                    SELECT 1 FROM events r
                    JOIN tags rt ON rt.event_id = r.id
                    WHERE r.kind = 9 AND r.agent_id = orig.agent_id
                      AND rt.name = 'e' AND rt.value = orig.id
                )
                LIMIT 1
                """,
                (event_id,),
            ).fetchone()
            return row is not None
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
        """Distinct declared capabilities (JP-redacted) only those present in each agent's
        LATEST kind 4 (per PROTOCOL (JP-redacted)4.5 kind 4 is overwrite-type). Caps that
        appeared only in superseded events are dropped from the registry.
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                WITH latest_k4 AS (
                    SELECT e.id, e.agent_id, e.created_at
                    FROM events e
                    JOIN (
                        SELECT agent_id, MAX(created_at) AS max_ts
                        FROM events WHERE kind = 4 GROUP BY agent_id
                    ) m
                      ON m.agent_id = e.agent_id AND m.max_ts = e.created_at
                    WHERE e.kind = 4
                )
                SELECT t.value AS capability,
                       COUNT(DISTINCT lk.agent_id) AS providers,
                       MAX(lk.created_at) AS last_declared
                FROM tags t
                JOIN latest_k4 lk ON lk.id = t.event_id
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
        extension_uri: str | None = None,
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
                if extension_uri is not None:
                    exts = meta.get("extensions") or []
                    uris = {e.get("uri") for e in exts if isinstance(e, dict)}
                    if extension_uri not in uris:
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
        """Load every kind 6 trust_vote event as
        (voter, target, content, created_at, tags_json) rows.

        Used by both trust_for() and trust_graph() so the underlying SQL is
        in one place. `tags_json` is the raw stored tag list (JP-redacted) needed by
        `parse_votes()` to extract PIP-002 `pow` bits per vote and feed the
        per-target `sybil_factor_pow`.
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT e.agent_id  AS voter,
                       t.value     AS target,
                       e.content   AS content,
                       e.created_at AS created_at,
                       e.tags_json AS tags_json
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
            # PIP-002 (JP-redacted)3 (JP-redacted) incoming-PoW sybil_factor (1.0 when no PoW votes
            # observed for this agent; tanh((JP-redacted) 2^pow_bits / 2^16) otherwise).
            "sybil_factor_pow": result.sybil_factor_pow.get(agent_id, 1.0),
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
                # PIP-002 (JP-redacted)3 (JP-redacted) per-target incoming-PoW dampening factor.
                "sybil_factor_pow": result.sybil_factor_pow.get(a, 1.0),
            }
            for a in targets
        ]
        out.sort(key=lambda d: d["weighted_score"], reverse=True)
        return out

    def get_event(self, event_id: str) -> Event | None:
        """Return a single event by its id, or None if not found.

        Direct lookup against the events table primary key. Used by
        `GET /events/{event_id}` so consumers can fetch a single event by id
        without paging the bulk feed.
        """
        conn = self._conn()
        try:
            r = conn.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if r is None:
                return None
            return Event(
                id=r["id"],
                agent_id=r["agent_id"],
                created_at=r["created_at"],
                kind=r["kind"],
                tags=json.loads(r["tags_json"]),
                content=r["content"],
                sig=r["sig"],
            )
        finally:
            conn.close()

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

    HEALTH_KIND = 11
    HEALTH_HEALTHY_WINDOW_SEC = 300        # 5 minutes
    HEALTH_24H = 24 * 3600
    HEALTH_7D = 7 * HEALTH_24H
    HEALTH_BUCKET_SEC = 300

    def agents(self) -> list[dict]:
        """List distinct agent_ids with their latest profile content, event count, and liveness summary."""
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
            out = []
            for r in rows:
                d = dict(r)
                h = self.health_for(d["agent_id"])
                d["is_healthy"]     = h["is_healthy"]
                d["uptime_24h_pct"] = h["uptime_24h_pct"]
                d["last_seen_at"]   = h["last_seen_at"]
                # PROTOCOL (JP-redacted)5.5: each /agents summary item MUST surface `name`
                # at the top level. The latest profile is JSON in `latest_profile`;
                # surface its `name` so consumers don't have to parse twice.
                d["name"] = None
                if d.get("latest_profile"):
                    try:
                        import json as _json
                        prof = _json.loads(d["latest_profile"])
                        if isinstance(prof, dict):
                            d["name"] = prof.get("name")
                    except (ValueError, TypeError):
                        pass
                out.append(d)
            return out
        finally:
            conn.close()

    def agent_view(self, agent_id: str) -> dict | None:
        """Rich single-agent view: profile + capabilities + counts + health.

        Returns None when the agent has never published any event.
        """
        conn = self._conn()
        try:
            row = conn.execute(
                """
                SELECT
                    e.agent_id,
                    (SELECT content FROM events
                       WHERE agent_id = e.agent_id AND kind = 0
                       ORDER BY created_at DESC LIMIT 1) AS latest_profile,
                    (SELECT content FROM events
                       WHERE agent_id = e.agent_id AND kind = 4
                       ORDER BY created_at DESC LIMIT 1) AS latest_capability,
                    COUNT(*) AS event_count,
                    MIN(created_at) AS first_seen,
                    MAX(created_at) AS last_seen
                FROM events e
                WHERE e.agent_id = ?
                GROUP BY e.agent_id
                """,
                (agent_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        d = dict(row)
        # Extract name from latest_profile
        d["name"] = None
        d["profile"] = None
        if d.get("latest_profile"):
            try:
                prof = json.loads(d["latest_profile"])
                if isinstance(prof, dict):
                    d["name"] = prof.get("name")
                    d["profile"] = prof
            except (ValueError, TypeError):
                pass
        # Extract capability list
        d["capabilities"] = []
        if d.get("latest_capability"):
            try:
                cap = json.loads(d["latest_capability"])
                if isinstance(cap, dict):
                    d["capabilities"] = cap.get("capabilities", [])
            except (ValueError, TypeError):
                pass
        # Strip raw blobs now that we've parsed them
        d.pop("latest_profile", None)
        d.pop("latest_capability", None)
        # Inline liveness summary
        h = self.health_for(agent_id)
        d["is_healthy"]     = h["is_healthy"]
        d["uptime_24h_pct"] = h["uptime_24h_pct"]
        d["last_seen_at"]   = h["last_seen_at"]
        return d

    def record_beat(self, agent_id: str, created_at: int, content: str) -> None:
        """Record a kind-11 health beat into the rolling in-memory window.

        Kind 11 is ephemeral infra telemetry (JP-redacted) it feeds health_for() but is
        NEVER written to the append-only event log (PROTOCOL (JP-redacted)5.5). The
        per-agent window is trimmed to HEALTH_7D so memory stays flat.
        """
        latency: float | None = None
        try:
            v = json.loads(content).get("latency_ms")
            if isinstance(v, (int, float)) and v >= 0:
                latency = float(v)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        cutoff = int(time.time()) - self.HEALTH_7D
        with self._health_lock:
            dq = self._health_beats.setdefault(agent_id, deque())
            dq.append((int(created_at), latency))
            while dq and dq[0][0] < cutoff:
                dq.popleft()

    def health_for(self, agent_id: str, now: int | None = None) -> dict:
        """Aggregate kind 11 health beats into per-agent uptime + latency stats.

        See PROTOCOL (JP-redacted)5.5. Beats are ephemeral (JP-redacted) read from the in-memory
        rolling window (record_beat), not the append-only log.
        """
        import statistics
        t_now = int(time.time()) if now is None else int(now)
        with self._health_lock:
            beats = [b for b in self._health_beats.get(agent_id, ())
                     if b[0] > t_now - self.HEALTH_7D]
        if not beats:
            return {
                "agent_id": agent_id,
                "last_seen_at": None,
                "is_healthy": False,
                "uptime_24h_pct": 0.0,
                "uptime_7d_pct": 0.0,
                "beats_24h": 0,
                "p50_latency_ms": None,
                "p95_latency_ms": None,
                "status_notes": [],
            }
        last_seen = max(ts for ts, _ in beats)
        is_healthy = (t_now - last_seen) <= self.HEALTH_HEALTHY_WINDOW_SEC
        buckets_24h = {((t_now - ts) // self.HEALTH_BUCKET_SEC) for ts, _ in beats
                       if (t_now - ts) < self.HEALTH_24H}
        buckets_7d  = {((t_now - ts) // self.HEALTH_BUCKET_SEC) for ts, _ in beats}
        latencies = [float(lat) for ts, lat in beats
                     if isinstance(lat, (int, float)) and lat >= 0]
        p50 = statistics.median(latencies) if latencies else None
        if len(latencies) >= 20:
            srt = sorted(latencies)
            p95 = srt[int(len(srt) * 0.95) - 1]
        else:
            p95 = max(latencies) if latencies else None
        return {
            "agent_id": agent_id,
            "last_seen_at": last_seen,
            "is_healthy": is_healthy,
            "uptime_24h_pct": round(100.0 * len(buckets_24h) / (self.HEALTH_24H // self.HEALTH_BUCKET_SEC), 2),
            "uptime_7d_pct":  round(100.0 * len(buckets_7d)  / (self.HEALTH_7D  // self.HEALTH_BUCKET_SEC), 2),
            "beats_24h": sum(1 for ts, _ in beats if (t_now - ts) < self.HEALTH_24H),
            "p50_latency_ms": p50,
            "p95_latency_ms": p95,
            "status_notes": [],
        }
