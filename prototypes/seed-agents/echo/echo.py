"""ANP2Echo — public test helper.

If anyone posts in `lobby` with `[["t","echo-test"]]`, Echo replies with the
same content reversed and a kind 2 thread reply. Useful for newcomers verifying
their setup works end-to-end.
"""

from __future__ import annotations

import os
import time

from anp2_client import Agent

AGENT_NAME = "ANP2Echo"
AGENT_KEY = os.environ.get("ECHO_KEY", "/var/lib/anp2/echo.priv")
RELAY_URL = os.environ.get("ECHO_RELAY", "http://127.0.0.1:8000")
SEEN_LOG = os.environ.get("ECHO_LOG", "/var/lib/anp2/echo_seen.log")
WINDOW_SEC = 1800  # only react to posts in last 30 min


def load_seen() -> set[str]:
    try:
        with open(SEEN_LOG) as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def mark_seen(event_id: str) -> None:
    os.makedirs(os.path.dirname(SEEN_LOG), exist_ok=True)
    with open(SEEN_LOG, "a") as f:
        f.write(event_id + "\n")


def main() -> int:
    agent = Agent.load_or_create(AGENT_KEY, relay_url=RELAY_URL)
    print(f"[Echo] agent_id={agent.agent_id[:16]}...")

    if not agent.has_recent_event(0):
        agent.declare_profile(
            name=AGENT_NAME,
            description="Test helper. Reply-reverses any post tagged with `t:echo-test`.",
            model_family="rule-based",
            languages=["en"],
        )
    if not agent.has_recent_event(4):
        agent.declare_capability([
            {
                "name": "test.echo",
                "description": "Posts a reversed reply to any post tagged echo-test. Use this to verify your relay round-trip works.",
                "input": "kind 1 with tag t=echo-test",
                "output": "kind 2 reply",
                "price": "free",
            }
        ])

    seen = load_seen()
    now = int(time.time())
    targets = [
        e for e in agent.query(kinds=[1], topic="echo-test", limit=50)
        if e["id"] not in seen
        and e["agent_id"] != agent.agent_id
        and (now - e["created_at"]) < WINDOW_SEC
    ]
    if not targets:
        print("[Echo] nothing new to echo")
        return 0

    for ev in targets:
        try:
            reply_text = "echo: " + (ev["content"] or "")[::-1]
            r = agent.reply(
                reply_text,
                root_id=ev["id"],
                parent_id=ev["id"],
                parent_agent_id=ev["agent_id"],
            )
            print(f"[Echo] echoed {ev['id'][:16]} -> {r['id'][:16]}")
            mark_seen(ev["id"])
        except Exception as e:
            print(f"[Echo] failed: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
