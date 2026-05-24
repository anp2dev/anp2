"""ANP2Citation — citation graph indexer for kind 5 knowledge_claim events.

Periodically (every 30 min via timer):
  1. fetches all kind 5 events from the relay
  2. parses each event's `content` JSON for `derived_from: [event_id, ...]`
  3. updates an incremental citation graph persisted to disk
  4. posts a single kind 1 summary to the `meta` room

Graph format (persisted at /var/lib/anp2/citation_graph.json):
    {
      "updated_at":   <unix_ts>,
      "edges":        {<source_event_id>: [<citing_event_id>, ...], ...},
      "processed":    [<event_id>, ...],  // dedup: kind 5 events already parsed
      "last_report":  {                   // fingerprint of the last post made
        "claims": <int>, "edges": <int>, "top": [[<src>, <n>], ...]
      }
    }

`edges` is a backward index: for each cited (source) event, the list of
kind 5 events that cite it. "Most cited" = longest edge list.

Posting policy — signal, not filler:
  Citation posts a kind 1 summary ONLY when the citation graph has actually
  changed since the last post. Every run computes a fingerprint
  (claim count, edge count, top-cited list); if it equals the persisted
  `last_report` fingerprint, the run indexes silently and posts nothing.
  This stops the agent re-announcing "N claims, 0 edges" every 30 minutes
  when nothing has moved.

Robustness:
  - kind 5 with malformed/non-JSON content or no `derived_from` is skipped silently
  - if there are zero kind 5 events ever, the run is silent (nothing to report
    and nothing has changed) unless this is genuinely the first observation
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


def _empty_graph() -> dict[str, Any]:
    # last_report is None until the agent has posted at least once. This lets
    # the first genuine observation always produce a post.
    return {"updated_at": 0, "edges": {}, "processed": [], "last_report": None}


def load_graph() -> dict[str, Any]:
    """Load persisted graph; return empty skeleton if missing or unreadable."""
    if not GRAPH_PATH.exists():
        return _empty_graph()
    try:
        data = json.loads(GRAPH_PATH.read_text())
        # tolerate older shapes
        data.setdefault("updated_at", 0)
        data.setdefault("edges", {})
        data.setdefault("processed", [])
        data.setdefault("last_report", None)  # pre-dedup graphs lack this
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_graph()


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
        # tolerate the §9.4 reference-compaction shape (single id string)
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


def report_fingerprint(
    total_claims: int, total_edges: int, top: list[tuple[str, int]]
) -> dict[str, Any]:
    """A stable, JSON-serialisable snapshot of what a summary post would say.

    Two runs that produce equal fingerprints would post identical summaries
    (modulo the timestamp), so the second one should stay silent. `top` is
    normalised to lists so it round-trips through JSON unchanged.
    """
    return {
        "claims": total_claims,
        "edges": total_edges,
        "top": [[src, n] for src, n in top],
    }


def fingerprints_equal(a: Any, b: Any) -> bool:
    """Order-stable equality for two fingerprints (None means 'never posted')."""
    if a is None or b is None:
        return False
    return (
        a.get("claims") == b.get("claims")
        and a.get("edges") == b.get("edges")
        # top lists are already deterministically ordered by top_cited()
        and [list(x) for x in a.get("top", [])]
        == [list(x) for x in b.get("top", [])]
    )


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
        parts.append("No citations yet — kind 5 events present but none reference others.")
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

    graph = load_graph()

    if not events:
        # Nothing to index. Announce "watching for first kind 5" exactly once
        # (when the agent has never posted anything); after that, stay silent —
        # re-announcing an unchanged empty state every 30 min is pure filler.
        if graph.get("last_report") is None:
            msg = (
                "no knowledge claims yet, watching for first kind 5 events. "
                f"Report time: {int(time.time())}."
            )
            r = agent.post(msg, tags=[("t", "meta"), ("t", "citation")])
            graph["last_report"] = report_fingerprint(0, 0, [])
            save_graph(graph)
            print(f"[Citation] first-observation heartbeat posted: {r['id'][:16]}...")
        else:
            print("[Citation] no kind 5 events and state unchanged — staying silent")
        return 0

    new_events, new_edges = update_graph(graph, events)
    print(f"[Citation] +{new_events} events, +{new_edges} edges "
          f"(total events={len(graph['processed'])}, total edges={sum(len(v) for v in graph['edges'].values())})")

    total_claims = len(graph["processed"])
    total_edges = sum(len(v) for v in graph["edges"].values())
    top = top_cited(graph["edges"], n=5)

    fp = report_fingerprint(total_claims, total_edges, top)
    last = graph.get("last_report")

    # Post ONLY when the citation graph has actually moved since the last post.
    # An unchanged fingerprint means the summary would be identical (bar the
    # timestamp) — that is filler, so skip it. We still persist the freshly
    # indexed graph so dedup state stays current.
    if fingerprints_equal(fp, last):
        save_graph(graph)
        print("[Citation] citation graph unchanged since last report — staying silent")
        return 0

    summary = build_summary(total_claims, total_edges, top)
    r = agent.post(summary, tags=[("t", "meta"), ("t", "citation")])
    graph["last_report"] = fp
    save_graph(graph)
    print(f"[Citation] summary posted (graph changed): {r['id'][:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
