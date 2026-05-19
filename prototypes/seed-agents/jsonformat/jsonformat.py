"""JsonFormat (JP-redacted) provides format.json.prettify capability for quickstart users.

Listens for kind 50 task.requests where capability == format.json.prettify
with payload {"text": "<json string>"}. Returns the prettified+sorted JSON
string. On invalid JSON, still emits kind 52 with an "error" field set
(does NOT crash, does NOT skip (JP-redacted) caller sees a structured failure).
"""
from __future__ import annotations
import json, time
from anp2_client import Agent

RELAY = "http://127.0.0.1:8000"
CAP = "format.json.prettify"
KEY_PATH = "/var/lib/anp2/jsonformat.priv"
WINDOW_SEC = 600


def prettify(text: str) -> tuple[str | None, str | None]:
    """Return (pretty_text, None) on success, (None, error_msg) on failure."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e.msg} at line {e.lineno} col {e.colno}"
    except (TypeError, ValueError) as e:
        return None, f"invalid JSON: {e}"
    try:
        pretty = json.dumps(parsed, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return None, f"could not serialize: {e}"
    return pretty, None


def main() -> int:
    agent = Agent.load_or_create(KEY_PATH, relay_url=RELAY)

    last = agent.query(kinds=[4], authors=[agent.agent_id], limit=1)
    needs_decl = True
    if last and (int(time.time()) - last[0]["created_at"]) < 86400:
        needs_decl = False
    if needs_decl:
        agent.declare_profile(
            name="JsonFormat",
            description="Provides format.json.prettify (JP-redacted) parses a JSON string and returns it indented and key-sorted. Returns a structured error if input is invalid JSON. Free utility.",
            model_family="rule-based",
        )
        agent.declare_capability([{
            "name": CAP,
            "version": "1.0",
            "description": "Pretty-prints + key-sorts a JSON document. Input payload: {\"text\": \"<json string>\"}. Output: {\"pretty\": \"...\"} or {\"error\": \"...\"} for invalid input.",
            "examples": ["{\"text\": \"{\\\"b\\\":1,\\\"a\\\":2}\"} -> {\"pretty\": \"{\\n  \\\"a\\\": 2,\\n  \\\"b\\\": 1\\n}\"}"],
            "input_modes": ["application/json"],
            "output_modes": ["application/json"],
            "tags": ["utility", "json", "format"],
            "pricing": {"currency": "USD", "model": "free", "amount": 0},
        }])
        print(f"[JsonFormat] re-declared profile + capability {CAP}")

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
        print("[JsonFormat] nothing to do")
        return 0

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
            print(f"[JsonFormat] accepted task {task_id[:24]} from {ev['agent_id'][:12]}")
        except Exception as e:
            print(f"[JsonFormat] accept failed: {e}")
            continue

        t0 = time.time()
        inp = payload.get("input") or payload.get("payload") or {}
        text = inp.get("text", "") if isinstance(inp, dict) else str(inp)
        pretty, err = prettify(text)
        if err is not None:
            result_payload = {"error": err, "input_len": len(text)}
        else:
            result_payload = {"pretty": pretty, "input_len": len(text), "output_len": len(pretty)}
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
            print(f"[JsonFormat] submitted result for {task_id[:24]} (error={err is not None})")
            processed += 1
        except Exception as e:
            print(f"[JsonFormat] result failed: {e}")

    print(f"[JsonFormat] processed {processed}/{len(relevant)} tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
