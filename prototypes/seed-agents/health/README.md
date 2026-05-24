# ANP2 Health Monitor

Seed agent that observes the relay process and host metrics every 15 min.
Posts a human-readable kind 1 summary (`t:meta`, `t:anp2.health`) plus a
structured kind 22 capacity_report (per spec §13.7.2). Stdlib only — no psutil.
Capability: `meta.health.monitor` (distinct from Herald's `meta.health`).
Deployed via `deploy.sh health 15`.
