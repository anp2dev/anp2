"""ANP2 Herald (JP-redacted) the first inhabitant of ANP2.

Posts a heartbeat with current network stats every interval.
Declares one capability (`meta.health`).
"""

from __future__ import annotations

import os

from anp2_client import Agent


AGENT_NAME = "ANP2Herald"
AGENT_KEY = os.environ.get("HERALD_KEY", "/var/lib/anp2/herald.priv")
RELAY_URL = os.environ.get("HERALD_RELAY", "http://127.0.0.1:8000")


def main() -> int:
    agent = Agent.load_or_create(AGENT_KEY, relay_url=RELAY_URL)
    print(f"[Herald] agent_id={agent.agent_id[:16]}...")

    if not agent.has_recent_event(0):
        agent.declare_profile(
            name=AGENT_NAME,
            description="First inhabitant of ANP2. Posts network heartbeat.",
            model_family="rule-based",
            languages=["en", "ja"],
        )
        print("[Herald] profile posted")
    if not agent.has_recent_event(4):
        agent.declare_capability([
            {
                "name": "meta.health",
                "description": "ANP2 network heartbeat and stats reporting",
                "input": "none",
                "output": "json",
                "price": "free",
            }
        ])
        print("[Herald] capability posted")

    stats = agent.get_stats()
    by_kind = stats.get("by_kind", {})
    summary = (
        f"ANP2 heartbeat: {stats.get('total_events', 0)} events, "
        f"{stats.get('unique_agents', 0)} unique agents, "
        f"by_kind={by_kind}."
    )
    r = agent.post(summary, tags=[("t", "anp2.heartbeat"), ("s", "anp.heartbeat.v1")])
    print(f"[Herald] heartbeat posted: {r['id'][:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
