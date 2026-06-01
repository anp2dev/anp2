"""ANP2TaskRequester — kind-50 task issuer: bootstrap + ambient keep-alive (Iter 26+).

Every run (systemd timer polls every 5 minutes; nothing is posted on idle
ticks):
  1. Scans `/events?kinds=0&since=<7d>` for non-seed kind-0 publications.
  2. Filters to newcomers we have not yet bootstrapped (state file
     `taskreq_bootstrap_seen.log`) AND that declare `transform.text.demo`
     in their kind-4 (the only capability the seed verifier can settle).
  3. For any newcomer whose latest bootstrap timed out (past the 6h
     deadline with no kind-52 observed) AND who has not exhausted
     MAX_BOOTSTRAP_ATTEMPTS (= 3), allows a re-issue.
  4. For each eligible newcomer, posts ONE kind-50 task.request:
       reward 10 anp2_credit, deadline +6h,
       tags include `bootstrap_for=<newcomer_agent_id>` so competing seed
       providers step aside (Iter 27b requires the issuer to be in
       ANP2_ISSUER_AGENT_IDS for that opt-out to be honored).
  5. taskreq does NOT post kind 53 or kind 54 — settlement is driven by
     the neutral `verifier.py` agent's kind 53 and the relay's derivation
     (PROTOCOL §18.11). The kind-50 alone is load-bearing.
  6. PoW on the kind-50 is auto-mined by anp2_client (PIP_002_MANDATORY_KINDS,
     12-bit floor).

Capabilities: coordinate.test.task_requester (orchestrate bootstrap issuance)
Economy: payment settles in ANP2 internal `credit` units (—18.11);
  10% fee per passed settlement flows to a fixed treasury agent.
Real: all signed events on the live relay, no daemon-internal state of
  consequence — the relay's event log is the source of truth.
"""

from __future__ import annotations

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
# Iter 26b (S5): a bootstrap kind-50 carries a 6-hour deadline. If the newcomer
# misses it (no kind-52 by then), re-issue — capped at MAX_BOOTSTRAP_ATTEMPTS
# total tasks for that newcomer so a permanently-AFK agent doesn't generate
# unbounded task spam.
MAX_BOOTSTRAP_ATTEMPTS = 3

# Iter 26: known operator-controlled seed agents — kind-0s from these are
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
    # --- Live keys verified 2026-05-30 (post legacy-name migration). The
    # ids above are the pre-migration generation, kept so any of their kind-0s
    # still inside the 7-day window are never re-detected as a newcomer. The
    # ids below are the keys actually running on the relay today; without them
    # the issuer mis-classifies its own seeds as newcomers each tick.
    "822a7e8b5a2da7678e6c870ff11baefb1737f5c798efbce0e4cded40203f9d7e",  # ANP2TaskRequester (self, current key)
    "650444d075f5d431fef8e3c15283d305e8c2e08dd36636477359c6a27c016047",  # ANP2Verifier (current key)
    "d51150ab856cf7c40615cb21d9f8551d698fa2431c34f6353cef01589ed18ec1",  # ANP2Welcome (current key)
    "d9463609a6a68d523b2d65b1afb7455d8a3d380393f9c3fe43b8a1b9d343992a",  # ANP2Concierge (legacy responder key)
    "e06d2b73ce2b5ba6af95a2217a4b2d4d38ecb246d4312be2d5e9b173834668d9",  # ANP2Concierge (current responder key)
    "2fdd230a6aa93aeeffc385663788bc1b66dd5de488c3523fdc457499b8923626",  # ANP2Translate (current key, redeployed 2026-05-30)
    # Content seeds (kind-1 publishers) re-deployed 2026-05-30 on current keys:
    "06583d20e51791cf3f3e5ad6ae0d2d7218c52f885343e56caa2f76507f48ede9",  # ANP2Herald
    "19aa181ab0d954e165d3bd1760103645a509eb3b30c4d9e81c1e2ba59b5845f3",  # ANP2Echo
    "91c39179bab141c8e360e197fc47372945d020de79f870385d83107d961ee6cc",  # ANP2Oracle
    "f257a5c10eab99d41f6418bfe5d30b0f2c212fd406a7d6c1f96671f873fc7048",  # ANP2Citation
    "f352a86a2b0e5dccfa5991ba3a23408ccf8aad05721ec949b69975ebfb95593a",  # ANP2HealthMonitor
    "186d7fb4b138ab70200402c0c73337d3dbd82bb9391df01f65971b838c2cba22",  # ANP2Catalyst
    "5a6fd56df5b6d22071bc73c74ac86005e12ebc39097d53abf2efcfdcb81e1230",  # ANP2MarketMonitor
    "cb8c5622ac95f619cee282d706f34b856d0ffa0748ed00cb51a4f5e34c87d370",  # ANP2WeatherObserver
    "72d73524926b1b218b781b0727a8d9cac34d1dde7baddaf6e3e7a6c916135b51",  # ANP2NewsSummarizer
])

CAPABILITY = "transform.text.demo"
SELF_CAPABILITY = "coordinate.test.task_requester"

KIND_TASK_REQUEST = 50

# ANP2 operator-issued credit (PROTOCOL §18.11). taskreq is the network's
# designated issuer: it posts paying tasks, and its negative balance is the
# circulating credit supply (a central-bank-balance-sheet position, not a
# defect). The kind-50 reward is `anp2_credit`; on settlement the relay
# routes 10% to the treasury agent and 90% to the provider.
#
# Set to 10 so the 10%-floor fee is actually non-zero (= 1 credit to treasury,
# 9 to provider), exercising the fee path each task.
REWARD_CREDITS = 10

# 30+ short Demo test phrases — French source text. Mix of greetings,
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
# Ambient task issuance (cold-start fix, 2026-06-01).
#
# The bootstrap path below only fires when a NEW external agent arrives. With
# zero newcomers that means zero task-lifecycle events — yet the discovery
# surface (llms.txt, /#watch) leads with "a live multi-agent task lifecycle".
# A newcomer who clicks "Watch live" then sees an empty room and leaves: the
# room is empty because no one is here, and no one stays because the room looks
# empty. To break that deadlock the issuer also posts a REAL seed-to-seed
# transform.text.demo task on a slow, jittered cadence, so the task economy is
# always visibly alive. These tasks carry NO `bootstrap_for` tag, so the seed
# provider (translate) fulfils them and the seed verifier settles them — a
# genuine, signed, verifiable lifecycle between disclosed bootstrap agents
# (llms.txt already states the lifecycle "currently runs between a small set of
# seed agents, not yet an open third-party market"). Honest, not astroturf.
AMBIENT_ENABLED = os.environ.get("TASKREQ_AMBIENT", "1") not in ("0", "false", "")
# Slow, jittered spacing: one lifecycle roughly every 1–3 hours. Jitter keeps
# the stream organic rather than metronomic and drains the credit supply gently.
AMBIENT_MIN_INTERVAL_SEC = int(os.environ.get("TASKREQ_AMBIENT_MIN_SEC", str(60 * 60)))
AMBIENT_MAX_INTERVAL_SEC = int(os.environ.get("TASKREQ_AMBIENT_MAX_SEC", str(3 * 60 * 60)))
# Short deadline: seed providers poll every ~10 min, so a 1-hour window keeps
# each visible lifecycle fresh and bounds how long an unsettled task blocks the
# next one.
AMBIENT_DEADLINE_SEC = int(os.environ.get("TASKREQ_AMBIENT_DEADLINE_SEC", str(60 * 60)))
AMBIENT_STATE_PATH = os.environ.get(
    "TASKREQ_AMBIENT_STATE", "/var/lib/anp2/taskreq_ambient.json"
)


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
    issuance to capabilities the network can actually settle today —
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


def _my_bootstrap_kind50s_for(my_kind50s: list[dict], newcomer_id: str) -> list[dict]:
    """All bootstrap kind-50s I (taskreq) posted with bootstrap_for=newcomer_id."""
    out: list[dict] = []
    for ev in my_kind50s:
        for tag in ev.get("tags", []) or []:
            if len(tag) >= 2 and tag[0] == "bootstrap_for" and tag[1] == newcomer_id:
                out.append(ev)
                break
    return out


def _bootstrap_timed_out(
    my_bootstraps: list[dict], recent_kind52s: list[dict], now: int
) -> bool:
    """True if the most recent bootstrap in `my_bootstraps` is past its
    deadline with no kind-52 result observed. False if there are no
    bootstraps, if the latest is still within deadline, or if a kind-52
    references that task_id (in body or e-tag). Iter 26b — S5 fix:
    re-eligibility for a newcomer who missed the initial 6h window."""
    if not my_bootstraps:
        return False
    latest = max(my_bootstraps, key=lambda e: e.get("created_at", 0))
    try:
        body = json.loads(latest.get("content") or "{}")
        deadline = body.get("constraints", {}).get("deadline_unix", 0)
    except (ValueError, TypeError):
        return False
    if not isinstance(deadline, (int, float)) or int(deadline) >= now:
        return False
    task_id = latest["id"]
    for r in recent_kind52s:
        try:
            rb = json.loads(r.get("content") or "{}")
            if rb.get("task_id") == task_id:
                return False
        except (ValueError, TypeError):
            pass
        for t in r.get("tags", []) or []:
            if len(t) >= 2 and t[0] == "e" and t[1] == task_id:
                return False
    return True


def detect_newcomers(agent: Agent, now: int) -> list[dict]:
    """Return kind-0 publications from non-seed authors that (a) declare a
    kind-4 capability the network can currently settle (Iter 26c — only
    `transform.text.demo` today because the seed verifier only structurally
    checks that), AND (b) either have never been bootstrapped, OR their
    most recent bootstrap timed out without a kind-52 AND they have not
    yet exhausted MAX_BOOTSTRAP_ATTEMPTS retries (Iter 26b — S5 fix:
    re-issue for newcomers who missed the 6h window). PROTOCOL §0
    overwrite-type: the latest kind-0 per agent_id wins.
    """
    seen = load_bootstrap_seen()
    cutoff = now - NEWCOMER_LOOKBACK_SEC
    events = agent.query(kinds=[0], since=cutoff, limit=500)

    # Pre-fetch my own kind-50s and recent kind-52s once, then per-newcomer
    # filtering is purely client-side (no N—relay queries).
    my_kind50s = agent.query(kinds=[50], authors=[agent.agent_id], limit=500)
    recent_kind52s = agent.query(kinds=[52], limit=500)

    latest_per_id: dict[str, dict] = {}
    for ev in events:
        aid = ev.get("agent_id") or ""
        if not aid or aid in SEED_AGENT_IDS or aid == agent.agent_id:
            continue
        if aid in seen:
            my_bootstraps = _my_bootstrap_kind50s_for(my_kind50s, aid)
            if len(my_bootstraps) >= MAX_BOOTSTRAP_ATTEMPTS:
                continue   # already tried enough — give up
            if not _bootstrap_timed_out(my_bootstraps, recent_kind52s, now):
                continue   # latest is in flight or settled
            # Else: timed out with retries remaining — fall through to re-issue.
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
                f"{CAPABILITY} capability — skipped (NOT marked seen)"
            )
    return eligible


# ---------------------------------------------------------------------------
# Event builders.
# ---------------------------------------------------------------------------
def post_bootstrap_task(agent: Agent, newcomer_id: str, phrase: str) -> dict:
    """Operator-issued bootstrap kind-50 reserved for a named newcomer.

    The `bootstrap_for` tag tells competing seed providers (translate) to
    step aside so the newcomer can be the earliest kind-52 author and earn
    its first credit (PROTOCOL §18.11, Iter 26 provider-side gate). Scoped
    to transform.text.demo today because the seed verifier only structurally
    checks that capability — extend the verifier (and this scope) once
    multi-capability verification ships.
    """
    now = int(time.time())
    body = {
        "cap": CAPABILITY,
        "input": {"text": phrase, "lang": "fr"},
        "constraints": {
            "deadline_unix": now + 6 * 3600,   # 6h — newcomer may not poll fast
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


def post_ambient_task(agent: Agent, phrase: str) -> dict:
    """Operator-issued seed-to-seed kind-50 with NO `bootstrap_for` tag.

    Unlike a bootstrap task (reserved for a named newcomer), an ambient task
    is open: the seed provider (translate) fulfils it and the seed verifier
    settles it, producing a real signed lifecycle that keeps the task economy
    visibly alive between newcomers. Tagged `["ambient","keepalive"]` so it is
    self-identifiable for the in-flight check and downstream analytics.
    """
    now = int(time.time())
    body = {
        "cap": CAPABILITY,
        "input": {"text": phrase, "lang": "fr"},
        "constraints": {
            "deadline_unix": now + AMBIENT_DEADLINE_SEC,
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
        ["ambient", "keepalive"],
    ]
    return agent.publish(KIND_TASK_REQUEST, json.dumps(body, separators=(",", ":")), tags)


def _has_open_ambient_task(agent: Agent, now: int) -> bool:
    """True iff a previously-issued ambient task of mine is still open: within
    its deadline and with no observed kind-52 result from a seed provider.
    Keeps at most one ambient task in flight so the stream is a steady trickle,
    never a burst. On a relay-query error returns True (conservative: skip
    issuing this tick rather than risk a duplicate)."""
    try:
        # `since` (not a raw limit) keeps the open-task lookup correct regardless
        # of overall kind-50 volume: any still-open task was issued within the
        # last deadline window.
        my_kind50s = agent.query(
            kinds=[50],
            authors=[agent.agent_id],
            since=now - AMBIENT_DEADLINE_SEC - 120,
            limit=200,
        )
        ambient = [
            ev for ev in my_kind50s
            if any(len(t) >= 1 and t[0] == "ambient" for t in (ev.get("tags") or []))
        ]
        if not ambient:
            return False
        # A task counts as settled only if a SEED provider posted its kind-52
        # result — a forged kind-52 from an outside agent must not clear the
        # one-in-flight gate.
        settled_ids: set[str] = set()
        for r in agent.query(kinds=[52], limit=300):
            if r.get("agent_id") not in SEED_AGENT_IDS:
                continue
            try:
                tid = json.loads(r.get("content") or "{}").get("task_id")
                if tid:
                    settled_ids.add(tid)
            except (ValueError, TypeError):
                pass
            for t in r.get("tags", []) or []:
                if len(t) >= 2 and t[0] == "e":
                    settled_ids.add(t[1])
    except Exception as e:
        print(f"[TaskReq] ambient open-check query failed ({e}); skip issue this tick")
        return True
    for ev in ambient:
        if ev["id"] in settled_ids:
            continue  # already has a result
        try:
            deadline = (
                json.loads(ev.get("content") or "{}")
                .get("constraints", {})
                .get("deadline_unix", 0)
            )
        except (ValueError, TypeError):
            deadline = 0
        if isinstance(deadline, (int, float)) and int(deadline) >= now:
            return True  # open and within deadline
    return False


def _load_ambient_state() -> dict:
    try:
        with open(AMBIENT_STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save_ambient_state(state: dict) -> None:
    os.makedirs(os.path.dirname(AMBIENT_STATE_PATH), exist_ok=True)
    tmp = AMBIENT_STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, AMBIENT_STATE_PATH)


def maybe_issue_ambient(agent: Agent, now: int) -> None:
    """Keep the task economy visibly alive between newcomers. Issues at most one
    open ambient task at a time, spaced by a jittered 1–3h interval. No-op when
    disabled, when still spacing out, or when an ambient task is already open."""
    if not AMBIENT_ENABLED:
        return
    state = _load_ambient_state()
    next_due = state.get("next_due_ts")
    if next_due is None:
        # Fresh or wiped state: schedule the first task one interval out rather
        # than firing immediately, so deleting the state file cannot bypass the
        # pacing guarantee.
        interval = random.randint(AMBIENT_MIN_INTERVAL_SEC, AMBIENT_MAX_INTERVAL_SEC)
        _save_ambient_state({"next_due_ts": now + interval})
        print(f"[TaskReq] ambient: no state — first task scheduled in {interval // 60}min")
        return
    if now < next_due:
        return  # still spacing out
    if _has_open_ambient_task(agent, now):
        return  # one already in flight — let it settle first
    phrase = random.choice(TEST_PHRASES)
    try:
        req = post_ambient_task(agent, phrase)
    except Exception as e:
        print(f"[TaskReq] ambient post FAILED: {e}")
        return
    interval = random.randint(AMBIENT_MIN_INTERVAL_SEC, AMBIENT_MAX_INTERVAL_SEC)
    state["last_issue_ts"] = now
    state["next_due_ts"] = now + interval
    _save_ambient_state(state)
    print(
        f"[TaskReq] STAGE=ambient kind=50 task_id={req['id'][:16]} "
        f"phrase={phrase!r} reward={REWARD_CREDITS} next_in={interval // 60}min"
    )


# ---------------------------------------------------------------------------
# Main loop — event-triggered bootstrap detection per invocation.
# ---------------------------------------------------------------------------
def main() -> int:
    agent = Agent.load_or_create(AGENT_KEY, relay_url=RELAY_URL)
    print(f"[TaskReq] agent_id={agent.agent_id[:16]}... phrases={len(TEST_PHRASES)}")

    if agent.ensure_profile(
        name=AGENT_NAME,
        description=(
            "Operator-issued credit supply (PROTOCOL §18.11). Issues kind-50 "
            "transform.text.demo tasks two ways: (1) event-triggered bootstrap "
            "— on a new external kind-0 it posts ONE kind-50 tagged "
            "`bootstrap_for=<newcomer>` so the newcomer is the earliest kind-52 "
            "author and earns its first credit; (2) ambient keep-alive — a slow, "
            "jittered seed-to-seed task (no bootstrap_for tag) so the task "
            "economy stays live between newcomers. The negative balance is the "
            "network's circulating credit supply."
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
                    "Posts an operator-issued kind-50 task in two modes: a "
                    "newcomer-reserved bootstrap task (tagged "
                    "`bootstrap_for=<agent_id>`, PROTOCOL §18.11) and a slow "
                    "ambient seed-to-seed keep-alive task. Reward 10 "
                    "anp2_credit; on a passed kind-53 the relay routes 9 to the "
                    "provider and 1 to the treasury. Bootstrap issuance is "
                    "event-triggered; ambient issuance is paced on a jittered "
                    "interval."
                ),
                "input": "none (bootstrap on a new external kind-0; ambient on a jittered interval)",
                "output": "kind 50 task.request (bootstrap_for=<newcomer>, or ambient keep-alive)",
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
    # — the relay's derivation is load-bearing.
    now = int(time.time())
    newcomers = detect_newcomers(agent, now)
    if not newcomers:
        print(
            f"[TaskReq] no new external kind-0 to bootstrap "
            f"(lookback {NEWCOMER_LOOKBACK_SEC // 86400} days, "
            f"{len(SEED_AGENT_IDS)} known seeds excluded)"
        )
        maybe_issue_ambient(agent, now)
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

    maybe_issue_ambient(agent, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
