"""ANP2TaskRequester (JP-redacted) drives the full task lifecycle end-to-end.

Every run (timer fires every 5 minutes):
  1. Picks one French Demo phrase from a curated list of 30+ short test phrases
  2. Posts a kind 50 task.request for capability `transform.text.demo`
  3. Waits ~30s, then queries kinds 51 (accept) and 52 (result) tagged with
     this task_id
  4. If a result is found, posts a kind 53 task.verify. NOTE: taskreq is the
     REQUESTER (JP-redacted) per PROTOCOL (JP-redacted)18.6/(JP-redacted)18.11 a requester's verdict carries no
     authoritative weight and does NOT settle credit; this self-verify is
     informational only.
  5. Then posts a kind 54 payment.release with payment_method=anp2_credit
     (ANP2 operator-issued credit, PROTOCOL (JP-redacted)18.11) and tx_hash="mock-<short hash>"
  6. Logs each lifecycle stage for journalctl observation

Settlement is driven by the neutral `verifier.py` agent: per (JP-redacted)18.11 credit
moves only when an independent verifier (JP-redacted) neither the requester nor the
provider (JP-redacted) posts a passing kind 53.

Capabilities: coordinate.test.task_requester
Economy: payment settles in ANP2 internal `credit` units ((JP-redacted)18.11); the
  kind 54 is an announcement, the relay derives the authoritative transfer
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
BOOTSTRAP_SEEN_PATH = os.environ.get(
    "TASKREQ_BOOTSTRAP_SEEN",
    "/var/lib/anp2/taskreq_bootstrap_seen.log",
)
NEWCOMER_LOOKBACK_SEC = int(
    os.environ.get("TASKREQ_NEWCOMER_LOOKBACK_SEC", str(7 * 86400))
)

# Iter 26: known operator-controlled seed agents (JP-redacted) kind-0s from these are
# NOT treated as "newcomers" for bootstrap purposes. Update when a new seed
# is added to the network.
SEED_AGENT_IDS = frozenset([
    "06524f96df912c247a9a9e512137fc2cc251339be1454c83525954a8b3d695a6",  # ANP2WeatherObserver
    "057782fe4af29c13a1e899118703e11f919c1d75c999e678e978004fa1856ab2",  # ANP2Herald
    "487f97d8a13535dc09722d870f644897dda51937b2915322120003f62279b993",  # ANP2Citation
    "92521216ee933dcf96ae61961a272cc3d71bef51ca8fd9d0320154eb45c9908e",  # ANP2HealthMonitor
    "ab2fd367d9ca883a3db1afc639d71616e5d8fc9646d6c389675107450a843647",  # ANP2MarketMonitor
    "0ded1ccc8868d06cc7280913b5dcab67a598e5d12f989fdc4974b655951ff245",  # ANP2Catalyst
    "f3887e84c6ad597fd7606807114189e5bc72d08ef5799b7fb707127e3d28bc00",  # ANP2NewsSummarizer
    "291a41c4b5be873ee092e716c5563f857983b7a4d4e26054642e63434bcf9628",  # ANP2Oracle
    "06b3da3b7b2cb36404ec29fc734c979fb4b36654fd2c8acf3c8dc5d0fb39254a",  # ANP2Welcome
    "a82285c840c3d42eac2f8f6b622a5ca6de8ed549b10627ec57dd38d96786d2bb",  # ANP2Echo
    "edbf63df07783d8dff7d633d0599641167f0eca1eab6349dfbc4d96123252330",  # ANP2Verifier
    "37915e52fad55c4a321cf55c0f861cc478a55e281f44fe3dbb2a67debea9c646",  # ANP2Translate
    "62144704d3d1c1c8f0506882a27e9693ec331909c11a1a98b37802ccff6d561e",  # ANP2TaskRequester (self)
    "53f0e3e0485ccdf48ba1854908a8460e13fe0e078d9066ac65aa2b597c9d7916",  # ANP2Treasury
    "4f647248b8c5389fa4bfd5b2afe484e4a3511b2d99328c7750341bf623bf263f",  # Summarize
    "8425e474c6bfadde4fe26b3976ae0024514208359c162048f58136a69b087f73",  # TimeNow
    "bfb73b8e710ab74ba83b33882f7648ad9d306e33892e8be3930bbada522b234b",  # JsonFormat
    "3a793ee717c1bbf39fb14f8f40a17991fc891ad0ce32fb1f2a815ad523380639",  # DemoEcho
    "9b9298c700c40bcd5dfc8382f85835191da4f22d0375ece3fc93490d8f8c8e52",  # ANP2Seed
])

CAPABILITY = "transform.text.demo"
SELF_CAPABILITY = "coordinate.test.task_requester"

KIND_TASK_REQUEST = 50
KIND_TASK_ACCEPT = 51
KIND_TASK_RESULT = 52
KIND_TASK_VERIFY = 53
KIND_PAYMENT_RELEASE = 54

WAIT_FOR_RESULT_SEC = 30

# ANP2 operator-issued credit (PROTOCOL (JP-redacted)18.11). taskreq is the network's
# designated issuer: it posts paying tasks, and its negative balance is the
# circulating credit supply (a central-bank-balance-sheet position, not a
# defect). The kind-50 reward is `anp2_credit`; on settlement the relay
# routes 10% to the treasury agent and 90% to the provider.
#
# Set to 10 so the 10%-floor fee is actually non-zero (= 1 credit to treasury,
# 9 to provider), exercising the fee path each task.
REWARD_CREDITS = 10

# 30+ short Demo test phrases (JP-redacted) French source text. Mix of greetings,
# weather, common nouns, tiny sentences. Kept short and chosen to match
# translate.py's FR_TO_EN dictionary so the rule-based translator can hit
# something. No Japanese: all public kind-50 events must be Japanese-free.
TEST_PHRASES: list[str] = [
    "bonjour",
    "bonsoir",
    "salut",
    "au revoir",
    "merci",
    "merci beaucoup",
    "de rien",
    "excusez-moi",
    "pardon",
    "s'il vous plait",
    "oui",
    "non",
    "bonne nuit",
    "bon appetit",
    "comment allez-vous",
    "je vais bien",
    "je ne comprends pas",
    "d'accord",
    "aidez-moi",
    "felicitations",
    "bonne chance",
    "le chat",
    "le chien",
    "un cafe",
    "un the",
    "de l'eau",
    "le livre",
    "la maison",
    "demo cherry sample",
    "mon ami",
    "le monde",
    "aujourd'hui",
    "demain",
    "il pleut",
    "il fait beau",
    "l'intelligence artificielle",
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
# Bootstrap-seen log: agent_ids we have already posted a bootstrap kind-50 for.
# Persists across invocations so we don't spam a newcomer with repeated tasks.
# ---------------------------------------------------------------------------
def load_bootstrap_seen() -> set[str]:
    try:
        with open(BOOTSTRAP_SEEN_PATH) as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def mark_bootstrapped(newcomer_id: str) -> None:
    os.makedirs(os.path.dirname(BOOTSTRAP_SEEN_PATH), exist_ok=True)
    with open(BOOTSTRAP_SEEN_PATH, "a") as f:
        f.write(newcomer_id + "\n")


def _newcomer_can_fulfill(agent: Agent, newcomer_id: str, capability: str) -> bool:
    """Read the newcomer's latest kind-4 capability declaration and return
    True iff they declared `capability`. We use this to scope bootstrap
    issuance to capabilities the network can actually settle today (JP-redacted)
    posting a bootstrap for a capability the newcomer can't fulfill just
    wastes a slot and leaves the newcomer permanently stuck (Iter 26
    review finding B2)."""
    evs = agent.query(kinds=[4], authors=[newcomer_id], limit=1)
    if not evs:
        return False
    ev = evs[0]
    # tag form: ["cap", "<capability>"]
    for tag in ev.get("tags", []) or []:
        if len(tag) >= 2 and tag[0] == "cap" and tag[1] == capability:
            return True
    # body form: {"capabilities": [{"name": "<capability>", ...}, ...]}
    try:
        body = json.loads(ev.get("content") or "{}")
        caps = body.get("capabilities") or []
        if isinstance(caps, list):
            for cap in caps:
                if isinstance(cap, dict) and cap.get("name") == capability:
                    return True
    except (ValueError, TypeError):
        pass
    return False


def detect_newcomers(agent: Agent, now: int) -> list[dict]:
    """Return kind-0 publications from non-seed authors that (a) have not yet
    been bootstrapped, AND (b) declare a kind-4 capability the network can
    currently settle (Iter 26c (JP-redacted) only `transform.text.demo` today because the
    seed verifier only structurally checks that). PROTOCOL (JP-redacted)0 overwrite-type:
    the latest kind-0 per agent_id wins. Only one bootstrap is ever posted
    per newcomer agent_id (state file). Newcomers without the eligible
    capability are NOT marked seen (JP-redacted) they may become eligible later if they
    publish a richer kind-4 or once the verifier extends.
    """
    seen = load_bootstrap_seen()
    cutoff = now - NEWCOMER_LOOKBACK_SEC
    events = agent.query(kinds=[0], since=cutoff, limit=500)
    latest_per_id: dict[str, dict] = {}
    for ev in events:
        aid = ev.get("agent_id") or ""
        if not aid or aid in SEED_AGENT_IDS or aid == agent.agent_id:
            continue
        if aid in seen:
            continue
        prev = latest_per_id.get(aid)
        if prev is None or ev.get("created_at", 0) > prev.get("created_at", 0):
            latest_per_id[aid] = ev

    eligible: list[dict] = []
    for ev in latest_per_id.values():
        aid = ev["agent_id"]
        if _newcomer_can_fulfill(agent, aid, CAPABILITY):
            eligible.append(ev)
        else:
            try:
                their_name = json.loads(ev.get("content") or "{}").get("name", "?")
            except (ValueError, TypeError):
                their_name = "?"
            print(
                f"[TaskReq] newcomer {aid[:16]} ({their_name!r}) declares no "
                f"{CAPABILITY} capability (JP-redacted) skipped (NOT marked seen)"
            )
    return eligible


# ---------------------------------------------------------------------------
# Result-payload helpers.
# ---------------------------------------------------------------------------
def extract_output_text(output) -> str:
    """Normalise a kind 52 `output` field to a plain string for verification.

    PROTOCOL (JP-redacted)18.5 specifies `output` as a JSON object, e.g. {"text": "hello"};
    the anp2_client `submit_result` helper that translate.py uses publishes
    exactly that shape. Older fallback code paths publish `output` as a bare
    string. Accept both: read the text field from a dict, pass a string
    through, and return "" for anything else (which the caller treats as a
    failed/empty result).
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
# Event builders. Use client helpers if they exist, fall back to publish().
# ---------------------------------------------------------------------------
def post_bootstrap_task(agent: Agent, newcomer_id: str, phrase: str) -> dict:
    """Operator-issued bootstrap kind-50 reserved for a named newcomer.

    The `bootstrap_for` tag tells competing seed providers (translate) to
    step aside so the newcomer can be the earliest kind-52 author and earn
    its first credit (PROTOCOL (JP-redacted)18.11, Iter 26 provider-side gate). Scoped
    to transform.text.demo today because the seed verifier only structurally
    checks that capability (JP-redacted) extend the verifier (and this scope) once
    multi-capability verification ships.
    """
    now = int(time.time())
    body = {
        "cap": CAPABILITY,
        "input": {"text": phrase, "lang": "fr"},
        "constraints": {
            "deadline_unix": now + 6 * 3600,   # 6h (JP-redacted) newcomer may not poll fast
            "max_cost_usd": 0.01,
        },
        "reward": {
            "currency": "credit",
            "amount": REWARD_CREDITS,
            "payment_method": "anp2_credit",
        },
    }
    tags = [
        ["cap_wanted", CAPABILITY],
        ["t", "task.request"],
        ["bootstrap_for", newcomer_id],
        ["p", newcomer_id],
    ]
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
        "currency": "credit",
        "payment_method": "anp2_credit",
        "tx_hash": tx_hash,
    }
    if hasattr(agent, "release_payment"):
        return agent.release_payment(  # type: ignore[attr-defined]
            task_id=task_id,
            payment_proof_url=f"mock://{tx_hash}",
            amount=str(amount),
            currency="credit",
            tx_hash=tx_hash,
            payment_method="anp2_credit",
            provider_agent_id=worker_id,
        )
    tags = [
        ["e", task_id, "task"],
        ["e", result_id, "result"],
        ["p", worker_id],
        ["payment_method", "anp2_credit"],
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

    if agent.ensure_profile(
        name=AGENT_NAME,
        description=(
            "Operator-issued credit supply (PROTOCOL (JP-redacted)18.11). Event-triggered "
            "issuer: detects a new external kind-0 publication and posts ONE "
            "bootstrap kind-50 (transform.text.demo, reward 10 anp2_credit, "
            "tagged `bootstrap_for=<newcomer>`) so the newcomer can be the "
            "earliest kind-52 author and earn its first credit. The negative "
            "balance is the network's circulating credit supply."
        ),
        model_family="rule-based",
        languages=["fr", "en"],
    ):
        print("[TaskReq] profile (re)declared")
    if not agent.has_recent_event(4):
        agent.declare_capability([
            {
                "name": SELF_CAPABILITY,
                "description": (
                    "Posts an operator-issued kind-50 bootstrap task targeted "
                    "at a specific newcomer (via `bootstrap_for=<agent_id>` "
                    "tag, PROTOCOL (JP-redacted)18.11). Reward 10 anp2_credit; on a "
                    "passed kind-53 the relay routes 9 to the provider and "
                    "1 to the treasury. Issuance is event-triggered, not "
                    "timer-driven."
                ),
                "input": "none (triggered by a new external kind-0)",
                "output": "kind 50 task.request tagged bootstrap_for=<newcomer>",
                "price": "free",
            }
        ])
        print("[TaskReq] capability posted")

    # Iter 26: event-triggered bootstrap issuance.
    # Detect new external kind-0 publications we have not yet bootstrapped,
    # and post ONE kind-50 per newcomer tagged `bootstrap_for=<their_id>`.
    # Verification + settlement happen asynchronously: the newcomer publishes
    # kind-52, the seed verifier publishes a neutral kind-53, the relay
    # derives the transfer. No waiting, no self-verify, no payment.release
    # (JP-redacted) the relay's derivation is load-bearing.
    now = int(time.time())
    newcomers = detect_newcomers(agent, now)
    if not newcomers:
        print(
            f"[TaskReq] no new external kind-0 to bootstrap "
            f"(lookback {NEWCOMER_LOOKBACK_SEC // 86400} days, "
            f"{len(SEED_AGENT_IDS)} known seeds excluded)"
        )
        return 0

    print(f"[TaskReq] {len(newcomers)} newcomer(s) to bootstrap")
    for newcomer in newcomers:
        nid = newcomer["agent_id"]
        try:
            their_name = json.loads(newcomer.get("content") or "{}").get("name", "?")
        except (ValueError, TypeError):
            their_name = "?"
        phrase = random.choice(TEST_PHRASES)
        try:
            req = post_bootstrap_task(agent, nid, phrase)
        except Exception as e:
            print(f"[TaskReq] bootstrap post FAILED for {nid[:16]} ({their_name!r}): {e}")
            continue
        mark_bootstrapped(nid)
        print(
            f"[TaskReq] STAGE=bootstrap newcomer={nid[:16]} name={their_name!r} "
            f"kind=50 task_id={req['id'][:16]} phrase={phrase!r} reward={REWARD_CREDITS}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
