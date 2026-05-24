"""DemoEcho — provides anp2.demo.echo capability for quickstart users.

Listens for kind 50 task.requests where capability == anp2.demo.echo,
accepts (kind 51), echoes the payload back (kind 52), then waits to be
verified. No payment side — this is a demo capability priced free.

Run via systemd timer every 60s (see demoecho.service / demoecho.timer).
"""
from __future__ import annotations
import json, os, time
from anp2_client import Agent

RELAY = "http://127.0.0.1:8000"
CAP = "anp2.demo.echo"
KEY_PATH = "/var/lib/anp2/demoecho.priv"
WINDOW_SEC = 600  # only react to task.requests in last 10 minutes


def main() -> int:
    agent = Agent.load_or_create(KEY_PATH, relay_url=RELAY)

    # Re-declare profile + capability (overwrite kind 0 + kind 4) if stale
    last = agent.query(kinds=[4], authors=[agent.agent_id], limit=1)
    needs_decl = True
    if last and (int(time.time()) - last[0]["created_at"]) < 86400:
        needs_decl = False
    if needs_decl:
        agent.declare_profile(
            name="DemoEcho",
            description="Provides anp2.demo.echo for quickstart users. Auto-accepts task.requests, echoes payload back, no payment.",
            model_family="rule-based",
        )
        agent.declare_capability([{
            "name": CAP,
            "version": "1.0",
            "description": "Echoes back the payload text. Used by anp2-quickstart for end-to-end lifecycle verification.",
            "examples": ["echo: hello world"],
            "input_modes": ["text/plain", "application/json"],
            "output_modes": ["text/plain", "application/json"],
            "tags": ["demo", "echo", "quickstart"],
            "pricing": {"currency": "USD", "model": "free", "amount": 0},
        }])
        print(f"[DemoEcho] re-declared profile + capability {CAP}")

    # Look for new kind 50 with cap=anp2.demo.echo in window
    now = int(time.time())
    requests = agent.query(kinds=[50], limit=200)
    relevant = []
    for ev in requests:
        if (now - ev["created_at"]) > WINDOW_SEC:
            continue
        if ev["agent_id"] == agent.agent_id:
            continue
        try:
            payload = json.loads(ev["content"])
            if payload.get("capability") != CAP:
                continue
        except (json.JSONDecodeError, TypeError):
            continue
        relevant.append((ev, payload))

    if not relevant:
        print("[DemoEcho] nothing to echo")
        return 0

    # Skip ones we already accepted (check our own kind 51 history)
    our_51s = agent.query(kinds=[51], authors=[agent.agent_id], limit=200)
    already_accepted: set[str] = set()
    for a in our_51s:
        try:
            payload = json.loads(a["content"])
            if payload.get("task_id"):
                already_accepted.add(payload["task_id"])
        except (json.JSONDecodeError, TypeError):
            pass

    processed = 0
    for ev, payload in relevant:
        task_id = payload.get("task_id")
        if not task_id or task_id in already_accepted:
            continue
        # 1. accept (kind 51)
        try:
            accept_event = agent.accept_task(
                task_id=task_id,
                eta_unix=int(time.time()) + 30,
                price_quote={"currency": "USD", "amount": 0},
                terms_hash="demo-no-terms",
                requester_agent_id=ev["agent_id"],
                capability=CAP,
            )
            accept_id = accept_event.get("id")
            print(f"[DemoEcho] accepted task {task_id[:24]} from {ev['agent_id'][:12]}")
        except Exception as e:
            print(f"[DemoEcho] accept failed: {e}")
            continue
        # 2. submit result (kind 52) — echo back
        echo_in = payload.get("payload") or {}
        echo_text = echo_in.get("text", "") if isinstance(echo_in, dict) else str(echo_in)
        result_payload = {
            "text": echo_text,
            "echoed_by": agent.agent_id[:16],
            "echoed_at": int(time.time()),
            "len": len(echo_text),
        }
        try:
            agent.submit_result(
                task_id=task_id,
                output=result_payload,
                runtime_ms=1,
                output_format="json",
                accept_event_id=accept_id,
                requester_agent_id=ev["agent_id"],
                capability=CAP,
            )
            print(f"[DemoEcho] submitted result for {task_id[:24]}")
            processed += 1
        except Exception as e:
            print(f"[DemoEcho] result failed: {e}")

    print(f"[DemoEcho] processed {processed}/{len(relevant)} tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
