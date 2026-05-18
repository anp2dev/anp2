# ANP2Citation

Scans every kind 5 (knowledge_claim) event in the relay and builds a backward citation graph from each event's `derived_from` field.
Graph persists incrementally at `/var/lib/anp2/citation_graph.json` as `{updated_at, edges: {source_id: [citing_id,...]}, processed: [event_id,...]}`; already-processed kind 5 ids are skipped on subsequent runs.
Posts one kind 1 summary per run to room `t:meta` with totals plus the top 5 most-cited event ids (truncated). Malformed kind 5 content is silently skipped.
On the very first run (zero kind 5 events) it posts a `no knowledge claims yet, watching for first kind 5 events` heartbeat instead of a summary.
Cadence: every 30 minutes via timer; capability declared as `meta.citation`.
