#!/usr/bin/env python3
"""tools/community_watch.py — ANP2 operator daily digest.

Run at the start of every ANP2 session per the
`feedback-ai-net-operator-routines` memory rule. Prints a snapshot of
recent external activity, treasury accrual, the A2A—publish funnel,
the operator-agent attention queue, and Sybil heuristics.

Usage:
    python3 tools/community_watch.py [--hours N] [--json]

Reads `anp2.com/api/*` over HTTPS (no auth). The A2A funnel and
[A2A-NEEDS-OPERATOR] queue come from journald on the relay host; set
ANP2_SERVER_IP + ANP2_SSH_KEY to enable that (those checks are best-
effort and skip silently when SSH is unconfigured).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone


RELAY_API = "https://anp2.com/api"
TREASURY_ID = "53f0e3e0485ccdf48ba1854908a8460e13fe0e078d9066ac65aa2b597c9d7916"

# Known seed agent_ids — agents NOT in this set are treated as external.
# Update when a new operator-controlled seed lands; keep in lockstep with
# the same constant in prototypes/seed-agents/taskreq/taskreq.py.
SEED_AGENT_IDS: set[str] = {
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
    "62144704d3d1c1c8f0506882a27e9693ec331909c11a1a98b37802ccff6d561e",  # ANP2TaskRequester
    "53f0e3e0485ccdf48ba1854908a8460e13fe0e078d9066ac65aa2b597c9d7916",  # ANP2Treasury
    "4f647248b8c5389fa4bfd5b2afe484e4a3511b2d99328c7750341bf623bf263f",  # Summarize
    "8425e474c6bfadde4fe26b3976ae0024514208359c162048f58136a69b087f73",  # TimeNow
    "bfb73b8e710ab74ba83b33882f7648ad9d306e33892e8be3930bbada522b234b",  # JsonFormat
    "3a793ee717c1bbf39fb14f8f40a17991fc891ad0ce32fb1f2a815ad523380639",  # DemoEcho
    "9b9298c700c40bcd5dfc8382f85835191da4f22d0375ece3fc93490d8f8c8e52",  # ANP2Seed
    # --- Live keys verified 2026-05-30 (post legacy-name migration). The ids
    # above are pre-migration; the current running keys differ. Without these
    # the conversion KPI counts our own seeds as "real external" adopters
    # (this caused the 2026-05-30 "5 external" false positive).
    "822a7e8b5a2da7678e6c870ff11baefb1737f5c798efbce0e4cded40203f9d7e",  # ANP2TaskRequester (current key)
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
}

# Name patterns that mark an agent as a synthetic / validation artifact —
# created during incident-review iterations (Iter 26-28 Sybil / PoW / race
# bypass tests, quickstart e2e tests, browser webcrypto try.html probes).
# They are NOT real external adopters but they are also NOT seeds; the
# Sybil heuristic and the conversion-funnel KPI should exclude them so
# operator signal stays clean. See [[feedback-ai-net-operator-routines]].
import re
SYNTHETIC_NAME_PATTERNS = [
    re.compile(r"^Iter\d", re.IGNORECASE),         # Iter26cSybil-B1, Iter27-PoW-test, …
    re.compile(r"attacker", re.IGNORECASE),
    re.compile(r"PoW-test", re.IGNORECASE),
    re.compile(r"bypass-attack", re.IGNORECASE),
    re.compile(r"Sybil-B\d", re.IGNORECASE),
    re.compile(r"NonMatching", re.IGNORECASE),
    re.compile(r"E2E-Newcomer", re.IGNORECASE),
    re.compile(r"e2e-test", re.IGNORECASE),         # try.html browser webcrypto probe
    re.compile(r"^quickstart-", re.IGNORECASE),     # anp2-quickstart pip-installed CLI
    re.compile(r"^hermes-probe-", re.IGNORECASE),   # Hermes' own probe naming (legitimate but exploration-only)
    re.compile(r"ConformanceProbe", re.IGNORECASE), # ANP2ConformanceProbe — our own SLO self-test of the economy
]


def _is_synthetic(agent: dict) -> bool:
    """True if this agent's name matches a synthetic / validation pattern."""
    name = (agent.get("name") or "").strip()
    return any(p.search(name) for p in SYNTHETIC_NAME_PATTERNS)


def _api(path: str) -> dict | list:
    req = urllib.request.Request(f"{RELAY_API}{path}", headers={"User-Agent": "anp2-community-watch/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _ssh_journal(unit: str, since: str, grep: str) -> list[str]:
    """Run journalctl on the relay host filtered by `grep`. Returns the
    matching lines (best-effort: returns [] if SSH is unconfigured)."""
    server = os.environ.get("ANP2_SERVER_IP")
    key = os.environ.get("ANP2_SSH_KEY")
    if not server or not key:
        return []
    cmd = (
        f"sudo journalctl -u {unit} --since {json.dumps(since)} --no-pager 2>/dev/null "
        f"| grep -E {json.dumps(grep)}"
    )
    try:
        r = subprocess.run(
            ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no",
             f"ec2-user@{server}", cmd],
            capture_output=True, text=True, timeout=20,
        )
        return [line for line in r.stdout.split("\n") if line.strip()]
    except Exception:
        return []


def watch(hours: int) -> dict:
    now = int(time.time())
    cutoff = now - hours * 3600

    # 1. Network state + external agents
    agents = _api("/agents")
    if isinstance(agents, dict):
        agents_list = agents.get("agents", [])
    else:
        agents_list = agents
    external_agents = [a for a in agents_list if a.get("agent_id") not in SEED_AGENT_IDS]
    # Split external into real vs synthetic (incident-review test agents).
    # Hermes-probe is borderline — counted as synthetic because the name
    # advertises "probe" (exploration-only intent). True external adoption
    # would normally use a domain-flavored or service-name agent.
    real_external_agents = [a for a in external_agents if not _is_synthetic(a)]
    synthetic_agents     = [a for a in external_agents if _is_synthetic(a)]
    recent_external_kind0 = [
        a for a in real_external_agents if (a.get("first_seen") or 0) >= cutoff
    ]
    recent_synthetic_kind0 = [
        a for a in synthetic_agents if (a.get("first_seen") or 0) >= cutoff
    ]

    # 2. External kind-50 in window (non-seed, non-synthetic authors).
    # The synthetic check needs an agent_id → name lookup since events
    # don't carry the latest profile name. Build the lookup from agents_list
    # so we don't double-query the API.
    synthetic_ids: set[str] = {
        a.get("agent_id") for a in synthetic_agents if a.get("agent_id")
    }
    events = _api("/events?kinds=50&limit=500")
    if not isinstance(events, list):
        events = []
    external_kind50 = [
        e for e in events
        if e.get("agent_id") not in SEED_AGENT_IDS
        and e.get("agent_id") not in synthetic_ids
        and e.get("created_at", 0) >= cutoff
    ]
    synthetic_kind50 = [
        e for e in events
        if e.get("agent_id") in synthetic_ids and e.get("created_at", 0) >= cutoff
    ]

    # 3. Treasury position
    try:
        trs = _api(f"/agents/{TREASURY_ID}/credit")
    except Exception:
        trs = {}

    # 4. A2A—publish funnel (latest hourly audit line)
    funnel_lines = _ssh_journal(
        "anp2-health-audit.service",
        f"{hours} hours ago",
        "A2A->publish funnel",
    )
    latest_funnel = funnel_lines[-1] if funnel_lines else None

    # 5. [A2A-NEEDS-OPERATOR] queue
    operator_items = _ssh_journal(
        "anp2-relay.service",
        f"{hours} hours ago",
        "A2A-NEEDS-OPERATOR",
    )

    # 6. Sybil heuristics — cheap signals, deeper inspection on flags
    sybil_signals: list[str] = []
    if len(recent_external_kind0) > 5:
        sybil_signals.append(
            f"{len(recent_external_kind0)} fresh external kind-0s in {hours}h — review"
        )
    return {
        "now": now,
        "hours": hours,
        "external_kind0_recent": recent_external_kind0,
        "synthetic_kind0_recent": recent_synthetic_kind0,
        "external_kind50_recent": external_kind50,
        "synthetic_kind50_recent": synthetic_kind50,
        "treasury": trs,
        "funnel_latest": latest_funnel,
        "operator_queue": operator_items,
        "sybil_signals": sybil_signals,
        "agents_total": len(agents_list),
        "agents_external": len(external_agents),
        "agents_real_external": len(real_external_agents),
        "agents_synthetic": len(synthetic_agents),
    }


def _fmt(s: dict) -> str:
    out: list[str] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out.append(f"=== ANP2 community watch ({ts}, last {s['hours']}h) ===")
    out.append("")
    out.append(
        f"[1] Network: {s['agents_total']} agents total, "
        f"{s['agents_external']} non-seed "
        f"({s.get('agents_real_external','?')} real external + "
        f"{s.get('agents_synthetic','?')} synthetic/validation)"
    )
    out.append("")
    rk0 = s["external_kind0_recent"]
    out.append(f"[2] New REAL external kind-0 (last {s['hours']}h): {len(rk0)}")
    for a in rk0[:10]:
        name = a.get("name") or "?"
        out.append(
            f"      {a.get('agent_id','?')[:16]} name={name!r} "
            f"first_seen={a.get('first_seen')}"
        )
    syn = s.get("synthetic_kind0_recent") or []
    if syn:
        out.append(f"      (also: {len(syn)} synthetic — Iter-attacker / probe / "
                   f"quickstart-test agents from incident-review iterations; "
                   f"separated so Sybil heuristic doesn't false-alert)")
    out.append("")
    rk50 = s["external_kind50_recent"]
    syn50 = s.get("synthetic_kind50_recent") or []
    out.append(f"[3] REAL external kind-50 (last {s['hours']}h): {len(rk50)}")
    for e in rk50[:5]:
        out.append(
            f"      task_id={e.get('id','?')[:16]} by "
            f"{e.get('agent_id','?')[:16]} t={e.get('created_at')}"
        )
    if syn50:
        out.append(f"      (also: {len(syn50)} synthetic kind-50 from Iter-attacker test agents — ignored)")
    out.append("")
    trs = s["treasury"] or {}
    out.append(
        f"[4] Treasury: balance={trs.get('balance', '?')} "
        f"locked={trs.get('locked', '?')} "
        f"verified_provider_tasks={trs.get('verified_provider_tasks', '?')}"
    )
    out.append("")
    out.append("[5] A2A—publish funnel (latest hourly audit):")
    if s["funnel_latest"]:
        out.append(f"      {s['funnel_latest'].strip()}")
    else:
        out.append("      (no SSH access — set ANP2_SERVER_IP + ANP2_SSH_KEY)")
    out.append("")
    op_q = s["operator_queue"]
    out.append(f"[6] [A2A-NEEDS-OPERATOR] queue (last {s['hours']}h): {len(op_q)}")
    for line in op_q[-5:]:
        out.append(f"      {line.strip()}")
    out.append("")
    sigs = s["sybil_signals"]
    out.append(f"[7] Sybil heuristics: {'OK' if not sigs else 'SIGNALS'}")
    for sig in sigs:
        out.append(f"      — {sig}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=int, default=24, help="lookback window in hours (default 24)")
    ap.add_argument("--json", action="store_true", help="output JSON instead of text")
    args = ap.parse_args()
    snapshot = watch(args.hours)
    if args.json:
        print(json.dumps(snapshot, indent=2, default=str))
    else:
        print(_fmt(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
