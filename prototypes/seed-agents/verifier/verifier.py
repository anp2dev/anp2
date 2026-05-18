"""ANP2Verifier (JP-redacted) independent second-opinion verifier for translate.en_es.

Every 5 minutes, scans for recent kind-52 task.result events for the
`translate.en_es` capability that do not yet have a kind-53 task.verify by
THIS verifier, and posts an independent verdict.

This proves multi-verifier consensus is mechanically possible. TaskRequester
self-verifies (with verdict=passed for any non-empty output); this Verifier
runs slightly stricter independent checks. Future work: majority-of-verifiers
aggregation logic.

Verification checks (mocked but real signal):
  - output is a non-empty string
  - output is mostly-latin / ASCII (a ja->en translation should look English-ish)
  - output length is plausible relative to the original (no 1-char outputs
    for a 5-word input, no 1000-char outputs either)

Verdict: passed/failed with reasons[] explaining why.

Capability: verify.translation.basic
"""

from __future__ import annotations

import json
import os
import time

from anp2_client import Agent

AGENT_NAME = "ANP2Verifier"
AGENT_KEY = os.environ.get("VERIFIER_KEY", "/var/lib/anp2/verifier.priv")
RELAY_URL = os.environ.get("VERIFIER_RELAY", "http://127.0.0.1:8000")
SEEN_LOG = os.environ.get("VERIFIER_LOG", "/var/lib/anp2/verifier_seen.log")
WINDOW_SEC = 3600  # only look at results from the last hour

CAPABILITY = "verify.translation.basic"
TARGET_CAP = "translate.en_es"

KIND_TASK_REQUEST = 50
KIND_TASK_RESULT = 52
KIND_TASK_VERIFY = 53


def load_seen() -> set[str]:
    try:
        with open(SEEN_LOG) as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def mark_seen(result_id: str) -> None:
    os.makedirs(os.path.dirname(SEEN_LOG), exist_ok=True)
    with open(SEEN_LOG, "a") as f:
        f.write(result_id + "\n")


# ---------------------------------------------------------------------------
# Independent verification logic.
# ---------------------------------------------------------------------------
def _is_mostly_latin(text: str, threshold: float = 0.7) -> bool:
    if not text:
        return False
    latin = sum(1 for ch in text if ord(ch) < 128)
    return (latin / len(text)) >= threshold


def verify_translation(input_text: str, output_text: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not isinstance(output_text, str) or not output_text.strip():
        return "failed", ["output empty or not a string"]
    if not _is_mostly_latin(output_text):
        reasons.append("output is not mostly latin (suspicious for ja->en)")
    out_len = len(output_text.strip())
    if input_text:
        in_len = len(input_text.strip())
        # very rough plausibility: english tends to be 1x-6x the char count of
        # a short Japanese phrase (kanji is dense). Reject absurd ratios only.
        if out_len < max(1, in_len // 3):
            reasons.append(
                f"output too short ({out_len} chars) vs input ({in_len} chars)"
            )
        if out_len > max(50, in_len * 20):
            reasons.append(
                f"output too long ({out_len} chars) vs input ({in_len} chars)"
            )
    if out_len > 2000:
        reasons.append("output exceeds 2000 chars (implausible for short phrase)")

    if reasons:
        return "failed", reasons
    return "passed", ["non-empty, mostly-latin, length plausible"]


# ---------------------------------------------------------------------------
# Lookup helpers.
# ---------------------------------------------------------------------------
def _task_id_from_result(ev: dict) -> str | None:
    """Find the kind 50 task_id this result is tied to via its `e` tag."""
    for tag in ev.get("tags", []) or []:
        if len(tag) >= 2 and tag[0] == "e":
            return tag[1]
    # fallback: maybe embedded in JSON body
    try:
        body = json.loads(ev.get("content") or "{}")
        if isinstance(body, dict):
            tid = body.get("task_id")
            if isinstance(tid, str):
                return tid
    except (ValueError, TypeError):
        pass
    return None


def _result_targets_cap(ev: dict, cap: str) -> bool:
    for tag in ev.get("tags", []) or []:
        if len(tag) >= 2 and tag[0] == "cap" and tag[1] == cap:
            return True
    try:
        body = json.loads(ev.get("content") or "{}")
        if isinstance(body, dict) and body.get("cap") == cap:
            return True
    except (ValueError, TypeError):
        pass
    return False


def _request_input_text(agent: Agent, task_id: str, request_ev: dict | None) -> str:
    if request_ev is None:
        evs = agent.query(kinds=[KIND_TASK_REQUEST], limit=200)
        for e in evs:
            if e["id"] == task_id:
                request_ev = e
                break
    if request_ev is None:
        return ""
    try:
        body = json.loads(request_ev.get("content") or "{}")
        if isinstance(body, dict):
            inp = body.get("input")
            if isinstance(inp, dict):
                for k in ("text", "ja", "content"):
                    v = inp.get(k)
                    if isinstance(v, str):
                        return v
            for k in ("input", "text", "ja"):
                v = body.get(k)
                if isinstance(v, str):
                    return v
    except (ValueError, TypeError):
        pass
    return (request_ev.get("content") or "").strip()


def _already_verified_by_us(
    agent: Agent, result_id: str, since: int
) -> bool:
    verifies = agent.query(kinds=[KIND_TASK_VERIFY], authors=[agent.agent_id], since=since, limit=200)
    for v in verifies:
        for tag in v.get("tags", []) or []:
            if len(tag) >= 2 and tag[0] == "e" and tag[1] == result_id:
                return True
    return False


# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------
def main() -> int:
    agent = Agent.load_or_create(AGENT_KEY, relay_url=RELAY_URL)
    print(f"[Verifier] agent_id={agent.agent_id[:16]}...")

    if not agent.has_recent_event(0):
        agent.declare_profile(
            name=AGENT_NAME,
            description=(
                "Independent second-opinion verifier for translate.en_es "
                "task.result events. Posts kind 53 task.verify with verdict + "
                "reasons. Demonstrates multi-verifier consensus is possible."
            ),
            model_family="rule-based",
            languages=["en"],
        )
        print("[Verifier] profile posted")
    if not agent.has_recent_event(4):
        agent.declare_capability([
            {
                "name": CAPABILITY,
                "description": (
                    "Basic structural verification of translation outputs: "
                    "non-empty, mostly-latin, length plausible relative to "
                    "the original input. Independent from the requester's "
                    "self-verification."
                ),
                "input": "kind 52 task.result with cap=translate.en_es",
                "output": "kind 53 task.verify with verdict + reasons",
                "price": "free",
            }
        ])
        print("[Verifier] capability posted")

    seen = load_seen()
    now = int(time.time())
    since = now - WINDOW_SEC

    results = agent.query(kinds=[KIND_TASK_RESULT], since=since, limit=200)
    candidates = [
        ev for ev in results
        if ev["id"] not in seen
        and ev["agent_id"] != agent.agent_id
        and _result_targets_cap(ev, TARGET_CAP)
    ]

    if not candidates:
        print("[Verifier] no new translate.en_es results to verify")
        return 0

    # Pre-fetch matching requests so we can pull the original input text.
    requests = agent.query(kinds=[KIND_TASK_REQUEST], since=since, limit=200)
    req_by_id = {ev["id"]: ev for ev in requests}

    verified = 0
    for result_ev in candidates:
        result_id = result_ev["id"]
        task_id = _task_id_from_result(result_ev)
        if not task_id:
            print(f"[Verifier] result {result_id[:16]} has no task_id tag; skipping")
            mark_seen(result_id)
            continue

        if _already_verified_by_us(agent, result_id, since=since):
            mark_seen(result_id)
            continue

        try:
            rbody = json.loads(result_ev.get("content") or "{}")
        except (ValueError, TypeError):
            rbody = {}
        output = rbody.get("output", "")

        input_text = _request_input_text(agent, task_id, req_by_id.get(task_id))
        verdict, reasons = verify_translation(input_text, output)
        score = 1.0 if verdict == "passed" else 0.0

        body = {
            "task_id": task_id,
            "result_id": result_id,
            "verdict": verdict,
            "score": score,
            "reasons": reasons,
            "verifier_kind": "independent",
            "verifier_capability": CAPABILITY,
        }
        if hasattr(agent, "verify_task"):
            v = agent.verify_task(  # type: ignore[attr-defined]
                task_id=task_id,
                result_id=result_id,
                verdict=verdict,
                score=score,
                reasons=reasons,
            )
        else:
            tags = [
                ["e", task_id, "task"],
                ["e", result_id, "result"],
                ["p", result_ev["agent_id"]],
                ["verdict", verdict],
                ["cap", CAPABILITY],
            ]
            v = agent.publish(
                KIND_TASK_VERIFY, json.dumps(body, separators=(",", ":")), tags
            )
        print(
            f"[Verifier] verified task={task_id[:16]} result={result_id[:16]} "
            f"-> {v['id'][:16]} verdict={verdict} score={score} reasons={reasons}"
        )
        mark_seen(result_id)
        verified += 1

    print(f"[Verifier] verified {verified} result(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
