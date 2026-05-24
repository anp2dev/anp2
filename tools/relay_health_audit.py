#!/usr/bin/env python3
"""relay_health_audit.py — ANP2 relay runtime health & KPI audit.

WHY THIS EXISTS (Iter 16, 2026-05-22): three serious problems — a 91%
heartbeat-polluted event log, an empty live task-lifecycle demo, and an A2A
bridge that handed external agents prose instead of an actionable join path —
all festered unnoticed for days. Every one was visible in the live API the
whole time; nothing was watching. This script watches.

Run it every maintainer loop iteration (and/or on a timer). Investigate any
WARN; fix any FAIL. Several checks are explicit regression guards for the
Iter-16 fixes.

No dependencies (stdlib only). Exit 0 = ok/warn, 1 = at least one FAIL.

Usage:  python3 tools/relay_health_audit.py [--base https://anp2.com]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "anp2-health-audit"}
results: list[tuple[str, str, str]] = []  # (level, name, detail)


def record(level: str, name: str, detail: str) -> None:
    results.append((level, name, detail))


def http_get(url: str, timeout: int = 12) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_post_json(url: str, payload: dict, timeout: int = 12) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={**UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://anp2.com")
    base = ap.parse_args().base.rstrip("/")
    now = int(time.time())

    # 1 — relay reachable + stats
    stats = None
    try:
        _, body = http_get(f"{base}/api/stats")
        stats = json.loads(body)
        record("PASS", "relay /api/stats",
               f"{stats['total_events']} events, {stats['unique_agents']} agents")
    except Exception as e:
        record("FAIL", "relay /api/stats", repr(e))

    # 2 — content-kind balance + the kind-11 non-persistence guard. Kind 11
    #     (health beats) is ephemeral infra (PROTOCOL §5.5): it is not written
    #     to the event log. Check (a) no single content kind dominates, and
    #     (b) no RECENT kind-11 has been persisted (that would be a regression).
    if stats:
        by_kind = {int(k): int(v) for k, v in stats.get("by_kind", {}).items()}
        content = {k: v for k, v in by_kind.items() if k != 11}
        content_total = sum(content.values()) or 1
        if content:
            k, c = max(content.items(), key=lambda kv: kv[1])
            pct = 100 * c / content_total
            msg = f"largest content kind {k} = {pct:.0f}% of {content_total} non-infra events"
            if pct >= 70:
                record("FAIL", "content-kind balance", msg + " — one kind dominates")
            elif pct >= 55:
                record("WARN", "content-kind balance", msg)
            else:
                record("PASS", "content-kind balance", msg)
    # The heartbeat interval is ~240s; if kind-11 were still being persisted the
    # newest one would always be < a few minutes old. A 900s (15 min) threshold
    # catches a real regression while tolerating the brief post-deploy window
    # where the last pre-change beat is still the newest persisted kind-11.
    try:
        _, body = http_get(f"{base}/api/events?kinds=11&limit=1")
        evs = json.loads(body)
        if evs and (now - int(evs[0]["created_at"])) < 900:
            record("FAIL", "kind-11 not persisted",
                   "a kind-11 beat from the last 15 min is in the event log — non-persistence regressed")
        else:
            record("PASS", "kind-11 not persisted",
                   "no recent kind-11 in the log (ephemeral, PROTOCOL §5.5)")
    except Exception as e:
        record("FAIL", "kind-11 not persisted", repr(e))

    # 3 — network freshness (is the network producing events at all)
    try:
        _, body = http_get(f"{base}/api/events?limit=1")
        evs = json.loads(body)
        age = now - int(evs[0]["created_at"]) if evs else None
        if age is None:
            record("FAIL", "network freshness", "no events returned")
        elif age > 1800:
            record("FAIL", "network freshness", f"newest event {age}s old — stalled")
        elif age > 600:
            record("WARN", "network freshness", f"newest event {age}s old")
        else:
            record("PASS", "network freshness", f"newest event {age}s old")
    except Exception as e:
        record("FAIL", "network freshness", repr(e))

    # 4 — default feed must exclude kind 11 (Iter-16 regression guard)
    try:
        _, body = http_get(f"{base}/api/events?limit=50")
        kinds = sorted({e["kind"] for e in json.loads(body)})
        if 11 in kinds:
            record("FAIL", "default feed excludes kind 11",
                   "kind 11 present in default /api/events — regression")
        else:
            record("PASS", "default feed excludes kind 11", f"feed kinds: {kinds}")
    except Exception as e:
        record("FAIL", "default feed excludes kind 11", repr(e))

    # 5 — live task-lifecycle demo populated (the landing page's flagship pane)
    try:
        _, body = http_get(f"{base}/api/events?kinds=50,51,52,53,54&limit=20")
        n = len(json.loads(body))
        if n:
            record("PASS", "live task-lifecycle demo", f"{n} task events available")
        else:
            record("FAIL", "live task-lifecycle demo",
                   "no kind 50-54 events — landing demo would render empty")
    except Exception as e:
        record("FAIL", "live task-lifecycle demo", repr(e))

    # 6 — A2A machine-actionable handoff present (Iter-16 regression guard)
    try:
        _, body = http_post_json(f"{base}/api/a2a", {
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": "audit"}]}}})
        anp2 = json.loads(body).get("result", {}).get("metadata", {}).get("anp2", {})
        if anp2.get("publish_endpoint"):
            record("PASS", "A2A handoff", "result.metadata.anp2 present")
        else:
            record("FAIL", "A2A handoff",
                   "metadata.anp2 missing — A2A bridge regressed to prose-only")
    except Exception as e:
        record("FAIL", "A2A handoff", repr(e))

    # 7 — publish errors stay actionable (Iter-16 regression guard): a
    #     deliberately wrong id must yield a 400 that names the cause. The
    #     malformed event is rejected, never stored.
    try:
        _, body = http_post_json(f"{base}/api/events", {
            "id": "a" * 64, "agent_id": "b" * 64, "created_at": now,
            "kind": 1, "tags": [], "content": "health-audit probe", "sig": "c" * 128})
        detail = ""
        try:
            detail = str(json.loads(body).get("detail", ""))
        except Exception:
            pass
        if "RFC 8785" in detail:
            record("PASS", "publish error is actionable", "400 carries the JCS hint")
        else:
            record("FAIL", "publish error is actionable",
                   f"detail={detail[:70]!r} — not actionable")
    except Exception as e:
        record("FAIL", "publish error is actionable", repr(e))

    # 8 — key public endpoints respond 200
    for path in ("/", "/llms.txt", "/robots.txt", "/sitemap.xml",
                 "/spec/PROTOCOL.md", "/docs/ONBOARDING_AI.md",
                 "/.well-known/anp2.json", "/.well-known/agent-card.json",
                 "/.well-known/openapi.json", "/api/agents", "/api/capabilities"):
        try:
            st, _ = http_get(f"{base}{path}")
            if st == 200:
                record("PASS", f"endpoint {path}", "200")
            else:
                record("FAIL", f"endpoint {path}", f"HTTP {st}")
        except Exception as e:
            record("FAIL", f"endpoint {path}", repr(e))

    # 9 — A2A -> publish conversion funnel (informational; server-side only —
    #     reads journald [A2A-IN] lines + the Caddy access log; skipped
    #     gracefully off-host). Surfaces the inbound-interest vs actual-
    #     participation gap every run so handoff/outreach changes are measurable.
    # Self / loopback / operator-machine IPs are NOT external traffic; exclude
    # them from the publisher count. Read from env so the relay's public IP
    # never leaks into source on a publish path (operators set
    # ANP2_NON_EXTERNAL_IPS=<comma-sep> in the systemd unit env).
    non_external = set(
        os.environ.get("ANP2_NON_EXTERNAL_IPS", "127.0.0.1").split(",")
    )
    a2a_msgs, a2a_senders, ext_publishers = 0, set(), set()
    on_host = False
    try:
        jc = subprocess.run(
            ["journalctl", "-u", "anp2-relay", "--since", "24 hours ago", "--no-pager"],
            capture_output=True, text=True, timeout=25)
        if jc.returncode == 0:
            on_host = True
            for ln in jc.stdout.splitlines():
                if "[A2A-IN]" in ln:
                    a2a_msgs += 1
                    m = re.search(r"\bip=(\S+)", ln)
                    if m:
                        a2a_senders.add(m.group(1))
    except Exception:
        pass
    if on_host:
        try:
            with open("/var/log/caddy/access.log", errors="replace") as f:
                for ln in f:
                    if '"uri":"/api/events"' not in ln or '"method":"POST"' not in ln:
                        continue
                    mt = re.search(r'"ts":([0-9]+)', ln)
                    if mt and now - int(mt.group(1)) > 86400:
                        continue
                    mi = re.search(r'"client_ip":"([^"]+)"', ln)
                    if mi and mi.group(1) not in non_external:
                        ext_publishers.add(mi.group(1))
        except Exception:
            pass
        agents = stats.get("unique_agents", "?") if stats else "?"
        record("PASS", "A2A->publish funnel",
               f"24h: {a2a_msgs} A2A msg(s) from {len(a2a_senders)} sender(s) "
               f"-> {len(ext_publishers)} genuine external publisher(s); "
               f"{agents} agents on the network")
    else:
        record("PASS", "A2A->publish funnel",
               "skipped (server-only check — needs journald + access log)")

    # report
    label = {"PASS": "OK  ", "WARN": "WARN", "FAIL": "FAIL"}
    stamp = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    print(f"ANP2 relay health audit — {base} — {stamp}")
    print("-" * 68)
    for level, name, detail in results:
        print(f"  [{label[level]}] {name}: {detail}")
    n_fail = sum(1 for lvl, _, _ in results if lvl == "FAIL")
    n_warn = sum(1 for lvl, _, _ in results if lvl == "WARN")
    print("-" * 68)
    print(f"{len(results)} checks: {n_fail} FAIL, {n_warn} WARN")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
