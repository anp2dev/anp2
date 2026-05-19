"""ANP2TaskRequester (JP-redacted) drives the full task lifecycle end-to-end.

Every run (timer fires every 5 minutes):
  1. Picks one Demo phrase from a curated list of 30+ short test phrases
  2. Posts a kind 50 task.request for capability `transform.text.demo`
  3. Waits ~30s, then queries kinds 51 (accept) and 52 (result) tagged with
     this task_id
  4. If a result is found, posts a kind 53 task.verify with verdict=passed
     (mocked verification at this stage) and score=1.0
  5. Then posts a kind 54 payment.release with payment_method=mocked and
     tx_hash="mock-<short hash>"
  6. Logs each lifecycle stage for journalctl observation

This is one of TWO independent verifiers (the other is `verifier.py`); having
both proves multi-verifier consensus is mechanically possible. Future work:
majority-of-verifiers logic.

Capabilities: coordinate.test.task_requester
Mocked: payment (no real value transfer at this stage)
Real: all signed events on the live relay, multi-participant flow
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time

from anp2_client import Agent

AGENT_NAME = "ANP2TaskRequester"
AGENT_KEY = os.environ.get("TASKREQ_KEY", "/var/lib/anp2/taskreq.priv")
RELAY_URL = os.environ.get("TASKREQ_RELAY", "http://127.0.0.1:8000")
SEEN_LOG = os.environ.get("TASKREQ_LOG", "/var/lib/anp2/taskreq_seen.log")

CAPABILITY = "transform.text.demo"
SELF_CAPABILITY = "coordinate.test.task_requester"

KIND_TASK_REQUEST = 50
KIND_TASK_ACCEPT = 51
KIND_TASK_RESULT = 52
KIND_TASK_VERIFY = 53
KIND_PAYMENT_RELEASE = 54

WAIT_FOR_RESULT_SEC = 30

# 30+ short Demo test phrases. Mix of greetings, weather, common nouns,
# tiny sentences. Kept short so the rule-based translator can hit something.
TEST_PHRASES: list[str] = [
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "demo cherry sample",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
    "(JP-redacted)",
]


# ---------------------------------------------------------------------------
# Seen-log helpers (one line per fully-completed task_id).
# ---------------------------------------------------------------------------
def load_seen() -> set[str]:
    try:
        with open(SEEN_LOG) as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def mark_seen(task_id: str) -> None:
    os.makedirs(os.path.dirname(SEEN_LOG), exist_ok=True)
    with open(SEEN_LOG, "a") as f:
        f.write(task_id + "\n")


# ---------------------------------------------------------------------------
# Event builders. Use client helpers if they exist, fall back to publish().
# ---------------------------------------------------------------------------
def post_task_request(agent: Agent, phrase: str) -> dict:
    now = int(time.time())
    body = {
        "cap": CAPABILITY,
        "input": {"text": phrase, "lang": "ja"},
        "constraints": {"deadline_unix": now + 60, "max_cost_usd": 0.01},
        "reward": {"currency": "USD", "amount": 0, "payment_method": "mocked"},
    }
    if hasattr(agent, "request_task"):
        return agent.request_task(  # type: ignore[attr-defined]
            capability=CAPABILITY,
            input=body["input"],
            constraints=body["constraints"],
            reward=body["reward"],
        )
    tags = [["cap_wanted", CAPABILITY], ["t", "task.request"]]
    return agent.publish(KIND_TASK_REQUEST, json.dumps(body, separators=(",", ":")), tags)


def find_events_for_task(
    agent: Agent, kinds: list[int], task_id: str, since: int
) -> list[dict]:
    """Client-side e-tag filter. Works on the current relay; will also work if
    the relay later adds native `?e=` filtering (the extra filter becomes a no-op)."""
    evs = agent.query(kinds=kinds, since=since, limit=200)
    out = []
    for ev in evs:
        for tag in ev.get("tags", []) or []:
            if len(tag) >= 2 and tag[0] == "e" and tag[1] == task_id:
                out.append(ev)
                break
    return out


def post_verify(
    agent: Agent,
    task_id: str,
    result_id: str,
    verifier_target_id: str,
    verdict: str,
    score: float,
    reasons: list[str],
) -> dict:
    body = {
        "task_id": task_id,
        "result_id": result_id,
        "verdict": verdict,
        "score": score,
        "reasons": reasons,
        "verifier_kind": "self",
    }
    if hasattr(agent, "verify_task"):
        return agent.verify_task(  # type: ignore[attr-defined]
            task_id=task_id,
            result_event_id=result_id,
            verdict=verdict,
            score=score,
            reasons=reasons,
            provider_agent_id=verifier_target_id,
        )
    tags = [
        ["e", task_id, "task"],
        ["e", result_id, "result"],
        ["p", verifier_target_id],
        ["verdict", verdict],
    ]
    return agent.publish(KIND_TASK_VERIFY, json.dumps(body, separators=(",", ":")), tags)


def post_payment_release(
    agent: Agent,
    task_id: str,
    result_id: str,
    worker_id: str,
    amount: float,
) -> dict:
    short = hashlib.sha256(
        f"{task_id}:{result_id}:{worker_id}".encode()
    ).hexdigest()[:12]
    tx_hash = f"mock-{short}"
    body = {
        "task_id": task_id,
        "result_id": result_id,
        "worker_id": worker_id,
        "amount": amount,
        "currency": "USD",
        "payment_method": "mocked",
        "tx_hash": tx_hash,
    }
    if hasattr(agent, "release_payment"):
        return agent.release_payment(  # type: ignore[attr-defined]
            task_id=task_id,
            payment_proof_url=f"mock://{tx_hash}",
            amount=str(amount),
            currency="USD",
            tx_hash=tx_hash,
            payment_method="mocked",
            provider_agent_id=worker_id,
        )
    tags = [
        ["e", task_id, "task"],
        ["e", result_id, "result"],
        ["p", worker_id],
        ["payment_method", "mocked"],
        ["tx_hash", tx_hash],
    ]
    return agent.publish(
        KIND_PAYMENT_RELEASE, json.dumps(body, separators=(",", ":")), tags
    )


# ---------------------------------------------------------------------------
# Main loop (JP-redacted) one full lifecycle per invocation.
# ---------------------------------------------------------------------------
def main() -> int:
    agent = Agent.load_or_create(AGENT_KEY, relay_url=RELAY_URL)
    print(f"[TaskReq] agent_id={agent.agent_id[:16]}... phrases={len(TEST_PHRASES)}")

    if not agent.has_recent_event(0):
        agent.declare_profile(
            name=AGENT_NAME,
            description=(
                "Drives the full kind 50-54 task lifecycle for ja->en "
                "translation on a 5-minute timer. Posts a request, waits for "
                "a result, then self-verifies and releases mocked payment. "
                "Lets the network demonstrate end-to-end signed-event work."
            ),
            model_family="rule-based",
            languages=["ja", "en"],
        )
        print("[TaskReq] profile posted")
    if not agent.has_recent_event(4):
        agent.declare_capability([
            {
                "name": SELF_CAPABILITY,
                "description": (
                    "Orchestrates a complete kind 50-54 task lifecycle, "
                    "exercising the protocol against any live transform.text.demo "
                    "provider. Payment is mocked at this phase."
                ),
                "input": "none (timer-driven)",
                "output": "kind 50 task.request, kind 53 task.verify, kind 54 payment.release",
                "price": "free",
            }
        ])
        print("[TaskReq] capability posted")

    seen = load_seen()
    phrase = random.choice(TEST_PHRASES)
    print(f"[TaskReq] picked phrase: {phrase}")

    req = post_task_request(agent, phrase)
    task_id = req["id"]
    request_ts = req.get("created_at", int(time.time()))
    print(f"[TaskReq] STAGE=request task_id={task_id[:16]} kind=50 phrase={phrase!r}")

    if task_id in seen:
        # Shouldn't happen on a freshly-signed event, but be defensive.
        print(f"[TaskReq] task {task_id[:16]} already in seen log; skipping")
        return 0

    # Wait for accept + result. We poll a couple of times.
    print(f"[TaskReq] waiting up to {WAIT_FOR_RESULT_SEC}s for result...")
    deadline = time.monotonic() + WAIT_FOR_RESULT_SEC
    accept_ev: dict | None = None
    result_ev: dict | None = None
    while time.monotonic() < deadline:
        if accept_ev is None:
            accepts = find_events_for_task(
                agent, [KIND_TASK_ACCEPT], task_id, since=request_ts - 5
            )
            if accepts:
                accept_ev = accepts[0]
                print(
                    f"[TaskReq] STAGE=accept task_id={task_id[:16]} "
                    f"kind=51 worker={accept_ev['agent_id'][:16]} "
                    f"accept_id={accept_ev['id'][:16]}"
                )
        results = find_events_for_task(
            agent, [KIND_TASK_RESULT], task_id, since=request_ts - 5
        )
        if results:
            result_ev = results[0]
            break
        time.sleep(3)

    if result_ev is None:
        print(
            f"[TaskReq] STAGE=timeout task_id={task_id[:16]} "
            "no kind 52 result within window; leaving lifecycle incomplete"
        )
        return 0

    # Parse result payload for logging + verification.
    try:
        rbody = json.loads(result_ev.get("content") or "{}")
    except (ValueError, TypeError):
        rbody = {}
    output = rbody.get("output", "")
    runtime_ms = rbody.get("runtime_ms", -1)
    worker_id = result_ev["agent_id"]
    print(
        f"[TaskReq] STAGE=result task_id={task_id[:16]} "
        f"kind=52 worker={worker_id[:16]} result_id={result_ev['id'][:16]} "
        f"runtime_ms={runtime_ms} out={output!r}"
    )

    # Self-verify: at this phase we trust any non-empty output. (Verifier.py
    # does a slightly stricter independent check; both should converge.)
    verdict = "passed" if isinstance(output, str) and output.strip() else "failed"
    reasons = (
        ["self-verify mocked at this phase: any non-empty output passes"]
        if verdict == "passed"
        else ["empty or non-string output"]
    )
    vr = post_verify(
        agent, task_id, result_ev["id"], worker_id, verdict, 1.0, reasons
    )
    print(
        f"[TaskReq] STAGE=verify task_id={task_id[:16]} kind=53 "
        f"verify_id={vr['id'][:16]} verdict={verdict} score=1.0"
    )

    # Release (mocked) payment.
    pay = post_payment_release(agent, task_id, result_ev["id"], worker_id, 0.0)
    tx = next(
        (t[1] for t in pay.get("tags", []) if len(t) >= 2 and t[0] == "tx_hash"),
        "?",
    )
    print(
        f"[TaskReq] STAGE=payment task_id={task_id[:16]} kind=54 "
        f"payment_id={pay['id'][:16]} tx_hash={tx} amount=0 USD method=mocked"
    )

    mark_seen(task_id)
    print(f"[TaskReq] STAGE=done task_id={task_id[:16]} lifecycle complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
