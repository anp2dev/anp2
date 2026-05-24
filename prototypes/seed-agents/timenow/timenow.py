"""TimeNow — provides util.time.now capability for quickstart users.

Listens for kind 50 task.requests where capability == util.time.now,
accepts (kind 51), returns the current UTC ISO 8601 timestamp + Unix
epoch + timezone offset (always UTC). No payment — free utility.

Run via systemd timer every 60s (see anp2-timenow.service / .timer).
"""
from __future__ import annotations
import json, time
from datetime import datetime, timezone
from anp2_client import Agent

RELAY = "http://127.0.0.1:8000"
CAP = "util.time.now"
KEY_PATH = "/var/lib/anp2/timenow.priv"
WINDOW_SEC = 600  # only react to task.requests in last 10 minutes


def main() -> int:
    agent = Agent.load_or_create(KEY_PATH, relay_url=RELAY)

    # Re-declare profile + capability at most once per 24h.
    last = agent.query(kinds=[4], authors=[agent.agent_id], limit=1)
    needs_decl = True
    if last and (int(time.time()) - last[0]["created_at"]) < 86400:
        needs_decl = False
    if needs_decl:
        agent.declare_profile(
            name="TimeNow",
            description="Provides util.time.now — returns current UTC ISO 8601 timestamp, Unix epoch, and timezone offset. Free utility for quickstart users.",
            model_family="rule-based",
        )
        agent.declare_capability([{
            "name": CAP,
            "version": "1.0",
            "description": "Returns current server time as UTC ISO 8601 string, Unix epoch seconds, and timezone offset (always +00:00). Deterministic, no external API.",
            "examples": ["request: {} -> {\"iso\": \"2026-05-19T12:34:56+00:00\", \"epoch\": 1779280496, \"tz_offset\": \"+00:00\"}"],
            "input_modes": ["application/json"],
            "output_modes": ["application/json"],
            "tags": ["utility", "time", "clock"],
            "pricing": {"currency": "USD", "model": "free", "amount": 0},
        }])
        print(f"[TimeNow] re-declared profile + capability {CAP}")

    # Find recent kind 50 with cap=util.time.now.
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
        print("[TimeNow] nothing to do")
        return 0

    # Skip task_ids we already accepted.
    our_51s = agent.query(kinds=[51], authors=[agent.agent_id], limit=200)
    already_accepted: set[str] = set()
    for a in our_51s:
        for tag in a.get("tags", []) or []:
            if len(tag) >= 2 and tag[0] == "e":
                already_accepted.add(tag[1])

    processed = 0
    for ev, payload in relevant:
        task_id = ev["id"]
        if task_id in already_accepted:
            continue
        try:
            accept_event = agent.accept_task(
                task_id=task_id,
                eta_unix=int(time.time()) + 5,
                price_quote={"currency": "USD", "amount": 0},
                terms_hash="free-no-terms",
                requester_agent_id=ev["agent_id"],
                capability=CAP,
            )
            accept_id = accept_event.get("id")
            print(f"[TimeNow] accepted task {task_id[:24]} from {ev['agent_id'][:12]}")
        except Exception as e:
            print(f"[TimeNow] accept failed: {e}")
            continue

        t0 = time.time()
        nowdt = datetime.now(timezone.utc)
        result_payload = {
            "iso": nowdt.isoformat(),
            "epoch": int(nowdt.timestamp()),
            "tz_offset": "+00:00",
        }
        runtime_ms = max(1, int((time.time() - t0) * 1000))
        try:
            agent.submit_result(
                task_id=task_id,
                output=result_payload,
                runtime_ms=runtime_ms,
                output_format="json",
                accept_event_id=accept_id,
                requester_agent_id=ev["agent_id"],
                capability=CAP,
            )
            print(f"[TimeNow] submitted result for {task_id[:24]}")
            processed += 1
        except Exception as e:
            print(f"[TimeNow] result failed: {e}")

    print(f"[TimeNow] processed {processed}/{len(relevant)} tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
