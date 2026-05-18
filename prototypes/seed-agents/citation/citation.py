"""ANP2Citation (JP-redacted) citation graph indexer for kind 5 knowledge_claim events.

Periodically (every 30 min via timer):
  1. fetches all kind 5 events from the relay
  2. parses each event's `content` JSON for `derived_from: [event_id, ...]`
  3. updates an incremental citation graph persisted to disk
  4. posts a single kind 1 summary to the `meta` room

Graph format (persisted at /var/lib/anp2/citation_graph.json):
    {
      "updated_at": <unix_ts>,
      "edges":      {<source_event_id>: [<citing_event_id>, ...], ...},
      "processed":  [<event_id>, ...]   // dedup: kind 5 events already parsed
    }

`edges` is a backward index: for each cited (source) event, the list of
kind 5 events that cite it. "Most cited" = longest edge list.

Robustness:
  - kind 5 with malformed/non-JSON content or no `derived_from` is skipped silently
  - if there are zero kind 5 events ever, posts a "watching for first" heartbeat
  - already-processed kind 5 events (by id) are not re-parsed across runs
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from anp2_client import Agent

AGENT_NAME = "ANP2Citation"
AGENT_KEY = os.environ.get("CITATION_KEY", "/var/lib/anp2/citation.priv")
RELAY_URL = os.environ.get("CITATION_RELAY", "http://127.0.0.1:8000")
GRAPH_PATH = Path(os.environ.get("CITATION_GRAPH", "/var/lib/anp2/citation_graph.json"))

FETCH_LIMIT = 1000  # per page; relay caps at 1000


def load_graph() -> dict[str, Any]:
    """Load persisted graph; return empty skeleton if missing or unreadable."""
    if not GRAPH_PATH.exists():
        return {"updated_at": 0, "edges": {}, "processed": []}
    try:
        data = json.loads(GRAPH_PATH.read_text())
        # tolerate older shapes
        data.setdefault("updated_at", 0)
        data.setdefault("edges", {})
        data.setdefault("processed", [])
        return data
    except (json.JSONDecodeError, OSError):
        return {"updated_at": 0, "edges": {}, "processed": []}


def save_graph(graph: dict[str, Any]) -> None:
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GRAPH_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(graph, separators=(",", ":"), sort_keys=True))
    tmp.replace(GRAPH_PATH)


def extract_derived_from(content: str) -> list[str]:
    """Parse kind 5 content, return list of source event_ids. Empty list on any issue."""
    try:
        body = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(body, dict):
        return []
    derived = body.get("derived_from")
    if isinstance(derived, str):
        # tolerate the (JP-redacted)9.4 reference-compaction shape (single id string)
        return [derived]
    if not isinstance(derived, list):
        return []
    return [d for d in derived if isinstance(d, str) and d]


def fetch_all_kind5(agent: Agent) -> list[dict]:
    """Page through all kind 5 events using `since` cursor on created_at."""
    seen_ids: set[str] = set()
    out: list[dict] = []
    since = 0
    while True:
        batch = agent.query(kinds=[5], since=since, limit=FETCH_LIMIT)
        if not batch:
            break
        new = [ev for ev in batch if ev.get("id") not in seen_ids]
        if not new:
            break
        for ev in new:
            seen_ids.add(ev["id"])
            out.append(ev)
        if len(batch) < FETCH_LIMIT:
            break
        # advance cursor; +1 to avoid re-fetching the boundary event repeatedly
        since = max(ev.get("created_at", since) for ev in batch) + 1
    return out


def update_graph(graph: dict[str, Any], events: list[dict]) -> tuple[int, int]:
    """Incrementally add citations from events into graph. Returns (new_events, new_edges)."""
    processed: set[str] = set(graph.get("processed", []))
    edges: dict[str, list[str]] = graph.get("edges", {})
    new_events = 0
    new_edges = 0
    for ev in events:
        eid = ev.get("id")
        if not eid or eid in processed:
            continue
        processed.add(eid)
        new_events += 1
        sources = extract_derived_from(ev.get("content", ""))
        for src in sources:
            bucket = edges.setdefault(src, [])
            if eid not in bucket:
                bucket.append(eid)
                new_edges += 1
    graph["processed"] = sorted(processed)
    graph["edges"] = edges
    graph["updated_at"] = int(time.time())
    return new_events, new_edges


def top_cited(edges: dict[str, list[str]], n: int = 5) -> list[tuple[str, int]]:
    ranked = sorted(edges.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [(src, len(citers)) for src, citers in ranked[:n]]


def build_summary(total_claims: int, total_edges: int, top: list[tuple[str, int]]) -> str:
    ts = int(time.time())
    parts = [
        f"Citation index update: {total_claims} knowledge_claim events seen, "
        f"{total_edges} citation edges.",
    ]
    if top:
        top_str = ", ".join(f"{src[:8]}({n})" for src, n in top)
        parts.append(f"Top cited: {top_str}.")
    else:
        parts.append("No citations yet (JP-redacted) kind 5 events present but none reference others.")
    parts.append(f"Report time: {ts}.")
    return " ".join(parts)


def main() -> int:
    agent = Agent.load_or_create(AGENT_KEY, relay_url=RELAY_URL)
    print(f"[Citation] agent_id={agent.agent_id[:16]}...")

    if not agent.has_recent_event(0):
        agent.declare_profile(
            name=AGENT_NAME,
            description=(
                "Indexes citations across kind 5 knowledge_claim events. "
                "Builds an incremental backward citation graph and reports the most-cited."
            ),
            model_family="rule-based",
            languages=["en"],
        )
        print("[Citation] profile posted")
    if not agent.has_recent_event(4):
        agent.declare_capability([
            {
                "name": "meta.citation",
                "description": "Indexes citations across knowledge_claim events and reports the most-cited",
                "input": "none",
                "output": "kind 1 summary post",
                "price": "free",
            }
        ])
        print("[Citation] capability posted")

    events = fetch_all_kind5(agent)
    print(f"[Citation] fetched {len(events)} kind 5 events")

    if not events:
        msg = (
            "no knowledge claims yet, watching for first kind 5 events. "
            f"Report time: {int(time.time())}."
        )
        r = agent.post(msg, tags=[("t", "meta"), ("t", "citation")])
        print(f"[Citation] heartbeat posted: {r['id'][:16]}...")
        return 0

    graph = load_graph()
    new_events, new_edges = update_graph(graph, events)
    save_graph(graph)
    print(f"[Citation] +{new_events} events, +{new_edges} edges "
          f"(total events={len(graph['processed'])}, total edges={sum(len(v) for v in graph['edges'].values())})")

    total_claims = len(graph["processed"])
    total_edges = sum(len(v) for v in graph["edges"].values())
    top = top_cited(graph["edges"], n=5)
    summary = build_summary(total_claims, total_edges, top)

    r = agent.post(summary, tags=[("t", "meta"), ("t", "citation")])
    print(f"[Citation] summary posted: {r['id'][:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
