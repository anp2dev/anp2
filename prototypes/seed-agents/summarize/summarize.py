"""Summarize (JP-redacted) provides summarize.text.simple capability for quickstart users.

Listens for kind 50 task.requests where capability == summarize.text.simple
with payload {"text": "..."}. Returns the first sentence + last sentence
joined with " ... " as a deterministic heuristic. Explicitly NOT an LLM
summary (JP-redacted) labelled as a heuristic so callers understand the limitation.
"""
from __future__ import annotations
import json, re, time
from anp2_client import Agent

RELAY = "http://127.0.0.1:8000"
CAP = "summarize.text.simple"
KEY_PATH = "/var/lib/anp2/summarize.priv"
WINDOW_SEC = 600

# Split on . ! ? followed by whitespace or end. Conservative, no abbrev handling.
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = _SENT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def summarize(text: str) -> dict:
    sentences = split_sentences(text)
    if not sentences:
        return {
            "summary": "",
            "method": "first+last_sentence_heuristic",
            "sentence_count": 0,
            "input_len": len(text),
            "note": "input contained no sentences; empty summary returned",
        }
    if len(sentences) == 1:
        summary = sentences[0]
    else:
        summary = f"{sentences[0]} ... {sentences[-1]}"
    return {
        "summary": summary,
        "method": "first+last_sentence_heuristic",
        "sentence_count": len(sentences),
        "input_len": len(text),
        "note": "deterministic heuristic, not LLM; returns first and last sentence joined by ' ... '",
    }


def main() -> int:
    agent = Agent.load_or_create(KEY_PATH, relay_url=RELAY)

    last = agent.query(kinds=[4], authors=[agent.agent_id], limit=1)
    needs_decl = True
    if last and (int(time.time()) - last[0]["created_at"]) < 86400:
        needs_decl = False
    if needs_decl:
        agent.declare_profile(
            name="Summarize",
            description="Provides summarize.text.simple (JP-redacted) deterministic first+last sentence heuristic. Not an LLM, no external API; transparent about the limitation. Free utility.",
            model_family="rule-based",
        )
        agent.declare_capability([{
            "name": CAP,
            "version": "1.0",
            "description": "Returns first sentence + last sentence joined by ' ... '. Deterministic, no LLM, no external service. Honest heuristic for quickstart users who want lifecycle without an API key.",
            "examples": ["{\"text\": \"A. B. C.\"} -> {\"summary\": \"A. ... C.\"}"],
            "input_modes": ["application/json"],
            "output_modes": ["application/json"],
            "tags": ["utility", "summarize", "heuristic"],
            "pricing": {"currency": "USD", "model": "free", "amount": 0},
        }])
        print(f"[Summarize] re-declared profile + capability {CAP}")

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
        print("[Summarize] nothing to do")
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
            print(f"[Summarize] accepted task {task_id[:24]} from {ev['agent_id'][:12]}")
        except Exception as e:
            print(f"[Summarize] accept failed: {e}")
            continue

        t0 = time.time()
        inp = payload.get("input") or payload.get("payload") or {}
        text = inp.get("text", "") if isinstance(inp, dict) else str(inp)
        result_payload = summarize(text)
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
            print(f"[Summarize] submitted result for {task_id[:24]}")
            processed += 1
        except Exception as e:
            print(f"[Summarize] result failed: {e}")

    print(f"[Summarize] processed {processed}/{len(relevant)} tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
