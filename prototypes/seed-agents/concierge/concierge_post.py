"""ANP2Concierge — post-only helper.

Driven externally (= Claude Code in /loop mode). Reads reply text from
stdin, signs + posts as ANP2Concierge to the relay. Stays minimal to
keep the host-side surface small.

Usage:
    echo "reply text" | python3 concierge_post.py <event_id> <sender_agent_id>

Stdout: posted kind-2 event_id (1 line) on success.
Exit: 0 success, 1 on signing/post failure.
"""
from __future__ import annotations
import os
import sys

from anp2_client import Agent

KEY_PATH = os.environ.get("CONCIERGE_KEY", "/var/lib/anp2/concierge.priv")
RELAY_URL = os.environ.get("CONCIERGE_RELAY", "http://127.0.0.1:8000")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: concierge_post.py <event_id> <sender_agent_id>",
              file=sys.stderr)
        return 1
    event_id, sender = sys.argv[1], sys.argv[2]
    reply_text = sys.stdin.read().strip()
    if not reply_text:
        print("no reply text on stdin", file=sys.stderr)
        return 1
    if len(reply_text) > 500:
        print(f"reply too long ({len(reply_text)} > 500)", file=sys.stderr)
        return 1
    agent = Agent.load_or_create(KEY_PATH, relay_url=RELAY_URL)
    try:
        r = agent.post(reply_text, tags=[
            ("e", event_id),
            ("p", sender),
            ("t", "lobby"),
        ])
        print(r.get("id", ""))
        return 0
    except Exception as e:
        print(f"post failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
