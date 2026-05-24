"""ANP2Verifier — independent second-opinion verifier for transform.text.demo.

Every 5 minutes, scans for recent kind-52 task.result events for the
`transform.text.demo` capability that do not yet have a kind-53 task.verify by
THIS verifier, and posts an independent verdict.

This proves multi-verifier consensus is mechanically possible. TaskRequester
self-verifies (with verdict=passed for any non-empty output); this Verifier
runs slightly stricter independent checks. Future work: majority-of-verifiers
aggregation logic.

Verification checks (mocked but real signal):
  - output is a non-empty string
  - output script range is plausible for the declared target language
  - output length is plausible relative to the original (no 1-char outputs
    for a 5-word input, no 1000-char outputs either)

Verdict: passed/failed with reasons[] explaining why.

Capability: verify.result.basic
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
WINDOW_SEC = 3600          # only look at results from the last hour
REQUEST_LOOKBACK_SEC = 86400  # but pre-fetch kind-50s for the last 24h
                              # so we can find the originating request even if
                              # the result came at the tail of a long deadline

CAPABILITY = "verify.result.basic"
TARGET_CAP = "transform.text.demo"

KIND_TASK_REQUEST = 50
KIND_TASK_RESULT = 52
KIND_TASK_VERIFY = 53

# Iter 28: seed-verifier standing check (PROTOCOL §18.11).
#
# Closes the 2-sock-puppet attack where an attacker R + P (both PoW-minted)
# rides this seed verifier as a free oracle: R posts a kind-50, P provides
# a structurally-valid kind-52, the seed verifier neutrally passes —
# settlement — P earns +(amount-fee) and verified_provider_tasks += 1.
# With this check the verifier refuses to publish kind-53 when the
# REQUESTER (kind-50 author) has zero standing AND no operator-issuer
# exemption — there is no settlement to drive, so P does not accrue
# standing for free.
#
# Note: this does NOT close the 3-sock-puppet attack where the attacker
# also runs their own neutral verifier (V). Closing that requires multi-
# verifier consensus or trust-weighted verification (Phase 2+).
ANP2_ISSUER_AGENT_IDS = frozenset([
    # taskreq seed — the canonical operator-issuer for Phase 0/1.
    "62144704d3d1c1c8f0506882a27e9693ec331909c11a1a98b37802ccff6d561e",
])
# Requester standing threshold (matches translate's COURTESY_BALANCE_LIMIT).
COURTESY_BALANCE_LIMIT = -50


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
# Result-payload helper.
# ---------------------------------------------------------------------------
def extract_output_text(output) -> str:
    """Normalise a kind 52 `output` field to a plain string.

    PROTOCOL §18.5 specifies `output` as a JSON object, e.g. {"text": "hello"};
    the anp2_client `submit_result` helper that translate.py uses publishes
    exactly that shape. Older fallback code paths publish `output` as a bare
    string. Accept both: read the text field from a dict, pass a string
    through, and return "" for anything else.

    Without this, `output` is the raw dict and verify_translation() rejects
    every result as "not a string" — i.e. every verdict would be failed.
    """
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in ("text", "content", "result", "translation"):
            v = output.get(key)
            if isinstance(v, str):
                return v
    return ""


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
        reasons.append("output is not mostly latin (suspicious for the declared target)")
    out_len = len(output_text.strip())
    if input_text:
        in_len = len(input_text.strip())
        # very rough plausibility: english tends to be 1x-6x the char count of
        # a short Demo phrase (kanji is dense). Reject absurd ratios only.
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
    """True if a kind-52 result is for capability `cap`.

    PROTOCOL §18.7 mandates the capability rides on the ["t", "<cap>"] tag
    (uniform across kinds 50-55), and the anp2_client `submit_result`
    helper that translate.py uses emits exactly that. Earlier code also
    published a ["cap", ...] tag and/or a `cap` body field, so we accept all
    three forms. (Missing the `t` tag was the bug that made this verifier
    match zero results and therefore never publish a kind 53.)
    """
    for tag in ev.get("tags", []) or []:
        if len(tag) >= 2 and tag[0] in ("t", "cap") and tag[1] == cap:
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


def _requester_has_standing(agent: Agent, requester_id: str) -> tuple[bool, str]:
    """Iter 28 seed-verifier standing check (PROTOCOL §18.11). Returns
    (ok, reason_when_refusing). Same rules as translate.py's courtesy
    throttle, applied at the verification layer:

      - Operator-issuers in ANP2_ISSUER_AGENT_IDS are always served.
      - Requesters with verified_provider_tasks > 0 carry real standing.
      - Requesters with available >= COURTESY_BALANCE_LIMIT are still in
        the courtesy window.
      - Anything else: refuse. The kind-52 stays on the log but no
        kind-53 from us means no settlement, so the 2-sock-puppet
        attacker (R + P, riding the seed verifier) accrues no standing.
      - If the credit endpoint is unreachable, default to serve (an
        availability problem should not look like a Sybil block).
    """
    if requester_id in ANP2_ISSUER_AGENT_IDS:
        return (True, "")
    try:
        credit = agent.get_credit(requester_id)
    except Exception:
        return (True, "")
    if int(credit.get("verified_provider_tasks", 0)) > 0:
        return (True, "")
    available = int(credit.get("available", 0))
    if available >= COURTESY_BALANCE_LIMIT:
        return (True, "")
    return (
        False,
        f"requester {requester_id[:16]} has zero standing; "
        f"available {available} (balance {credit.get('balance', 0)} - "
        f"locked {credit.get('locked', 0)}) < {COURTESY_BALANCE_LIMIT}",
    )


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

    if agent.ensure_profile(
        name=AGENT_NAME,
        description=(
            "Independent second-opinion verifier for transform.text.demo "
            "task.result events. Posts kind 53 task.verify with verdict + "
            "reasons. Demonstrates multi-verifier consensus is possible."
        ),
        model_family="rule-based",
        languages=["en"],
    ):
        print("[Verifier] profile (re)declared")
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
                "input": "kind 52 task.result with cap=transform.text.demo",
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
        print("[Verifier] no new transform.text.demo results to verify")
        return 0

    # Pre-fetch matching requests so we can pull the original input text
    # AND check requester standing (Iter 28). Use the wider
    # REQUEST_LOOKBACK_SEC window so we still see the kind-50 even when
    # the result came near the tail of a long deadline.
    requests = agent.query(
        kinds=[KIND_TASK_REQUEST],
        since=now - REQUEST_LOOKBACK_SEC,
        limit=500,
    )
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

        # Iter 28: seed-verifier standing check on the REQUESTER. If the
        # requester (kind-50 author) has no standing and no operator-issuer
        # exemption, refuse to verify — this closes the 2-sock-puppet
        # attack that used to ride this seed verifier as a free oracle.
        request_ev = req_by_id.get(task_id)
        if request_ev is None:
            print(
                f"[Verifier] result {result_id[:16]} references task_id "
                f"{task_id[:16]} not in {REQUEST_LOOKBACK_SEC // 3600}h "
                f"request window; refusing to verify (no requester context)"
            )
            mark_seen(result_id)
            continue
        requester_id = request_ev["agent_id"]
        ok, why = _requester_has_standing(agent, requester_id)
        if not ok:
            print(
                f"[Verifier] skip result {result_id[:16]} task={task_id[:16]}: {why}"
            )
            mark_seen(result_id)
            continue

        try:
            rbody = json.loads(result_ev.get("content") or "{}")
        except (ValueError, TypeError):
            rbody = {}
        # PROTOCOL §18.5: `output` is a JSON object (e.g. {"text": ...}); the
        # publish() fallback path emits a bare string. Normalise both.
        output_text = extract_output_text(rbody.get("output", ""))

        input_text = _request_input_text(agent, task_id, req_by_id.get(task_id))
        verdict, reasons = verify_translation(input_text, output_text)
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
                result_event_id=result_id,
                verdict=verdict,
                score=score,
                reasons=reasons,
                provider_agent_id=result_ev["agent_id"],
                capability=CAPABILITY,
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
