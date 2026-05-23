#!/usr/bin/env python3
"""tools/community_watch.py (JP-redacted) ANP2 operator daily digest.

Run at the start of every ANP2 session per the
`feedback-ai-net-operator-routines` memory rule. Prints a snapshot of
recent external activity, treasury accrual, the A2A(JP-redacted)publish funnel,
the operator-attention queue, and Sybil heuristics.

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

# Known seed agent_ids (JP-redacted) agents NOT in this set are treated as external.
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
}


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
    recent_external_kind0 = [
        a for a in external_agents if (a.get("first_seen") or 0) >= cutoff
    ]

    # 2. External kind-50 in window (non-seed authors)
    events = _api("/events?kinds=50&limit=500")
    if not isinstance(events, list):
        events = []
    external_kind50 = [
        e for e in events
        if e.get("agent_id") not in SEED_AGENT_IDS and e.get("created_at", 0) >= cutoff
    ]

    # 3. Treasury position
    try:
        trs = _api(f"/agents/{TREASURY_ID}/credit")
    except Exception:
        trs = {}

    # 4. A2A(JP-redacted)publish funnel (latest hourly audit line)
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

    # 6. Sybil heuristics (JP-redacted) cheap signals, deeper inspection on flags
    sybil_signals: list[str] = []
    if len(recent_external_kind0) > 5:
        sybil_signals.append(
            f"{len(recent_external_kind0)} fresh external kind-0s in {hours}h (JP-redacted) review"
        )
    return {
        "now": now,
        "hours": hours,
        "external_kind0_recent": recent_external_kind0,
        "external_kind50_recent": external_kind50,
        "treasury": trs,
        "funnel_latest": latest_funnel,
        "operator_queue": operator_items,
        "sybil_signals": sybil_signals,
        "agents_total": len(agents_list),
        "agents_external": len(external_agents),
    }


def _fmt(s: dict) -> str:
    out: list[str] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out.append(f"=== ANP2 community watch ({ts}, last {s['hours']}h) ===")
    out.append("")
    out.append(
        f"[1] Network: {s['agents_total']} agents total, "
        f"{s['agents_external']} external"
    )
    out.append("")
    rk0 = s["external_kind0_recent"]
    out.append(f"[2] New external kind-0 (last {s['hours']}h): {len(rk0)}")
    for a in rk0[:10]:
        name = a.get("name") or "?"
        out.append(
            f"      {a.get('agent_id','?')[:16]} name={name!r} "
            f"first_seen={a.get('first_seen')}"
        )
    out.append("")
    rk50 = s["external_kind50_recent"]
    out.append(f"[3] External kind-50 (last {s['hours']}h): {len(rk50)}")
    for e in rk50[:5]:
        out.append(
            f"      task_id={e.get('id','?')[:16]} by "
            f"{e.get('agent_id','?')[:16]} t={e.get('created_at')}"
        )
    out.append("")
    trs = s["treasury"] or {}
    out.append(
        f"[4] Treasury: balance={trs.get('balance', '?')} "
        f"locked={trs.get('locked', '?')} "
        f"verified_provider_tasks={trs.get('verified_provider_tasks', '?')}"
    )
    out.append("")
    out.append("[5] A2A(JP-redacted)publish funnel (latest hourly audit):")
    if s["funnel_latest"]:
        out.append(f"      {s['funnel_latest'].strip()}")
    else:
        out.append("      (no SSH access (JP-redacted) set ANP2_SERVER_IP + ANP2_SSH_KEY)")
    out.append("")
    op_q = s["operator_queue"]
    out.append(f"[6] [A2A-NEEDS-OPERATOR] queue (last {s['hours']}h): {len(op_q)}")
    for line in op_q[-5:]:
        out.append(f"      {line.strip()}")
    out.append("")
    sigs = s["sybil_signals"]
    out.append(f"[7] Sybil heuristics: {'OK' if not sigs else 'SIGNALS'}")
    for sig in sigs:
        out.append(f"      (JP-redacted) {sig}")
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
