#!/usr/bin/env python3
"""tools/crawler_log_audit.py — measure AI crawler hits on anp2.com.

Reads /var/log/caddy/access.log on the relay (JSON-format Caddy log),
identifies AI crawler hits by User-Agent string match, aggregates and
prints daily stats:
  - bot name → hit count
  - top URL paths per bot
  - first / last hit timestamp per bot

This is the measurement layer for `site/` (the AI discovery surface
deployed 2026-05-25). Without this script, we cannot answer "is the
AI-facing surface actually being fetched?" — a question that drives
30-day freeze-period evaluation.

Companion tool: `tools/community_watch.py` (the AI agent peer activity
view). community_watch reads protocol-level events; this script reads
HTTP-level crawler signatures. The two together give a complete inbound
picture: who's reading (this), who's joining (community_watch).

Usage:
    bash tools/crawler_log_audit.py                  # last 24h, text
    bash tools/crawler_log_audit.py --hours 168      # last 7d
    bash tools/crawler_log_audit.py --json           # machine-readable
    bash tools/crawler_log_audit.py --json --output FILE  # also write to FILE

Public metrics endpoint (task #81 C2):
    When run on the relay host itself (i.e., on EC2 not via SSH),
    `--mode local --json --output /var/www/anp2/.well-known/anp2-metrics.json`
    publishes the JSON aggregate at https://anp2.com/.well-known/anp2-metrics.json
    via the existing Caddy default-handler route. Cron suggestion (hourly):

        0 * * * * /usr/bin/python3 /opt/anp2/tools/crawler_log_audit.py \
                  --mode local --hours 24 --json \
                  --output /var/www/anp2/.well-known/anp2-metrics.json

    No relay code change required — Caddy already serves
    `/var/www/anp2/.well-known/*` for any file present there.

Requires:
    env/relay-ip.txt or $ANP2_SERVER_IP (SSH mode only)
    env/anp2.pem or $ANP2_SSH_KEY (SSH mode only)
    `sudo` access to read /var/log/caddy/access.log (both modes)
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone


# Bot User-Agent substrings (case-insensitive match). Keep aligned with
# site/robots.txt Allow-list so any bot we explicitly welcome gets
# measured here too.
AI_CRAWLER_PATTERNS = {
    "GPTBot":             "GPTBot",
    "ChatGPT-User":       "ChatGPT-User",
    "ClaudeBot":          "ClaudeBot",
    "Claude-Web":         "Claude-Web",
    "anthropic-ai":       "anthropic-ai",
    "PerplexityBot":      "PerplexityBot",
    "Google-Extended":    "Google-Extended",
    "Applebot-Extended":  "Applebot-Extended",
    "cohere-ai":          "cohere-ai",
    "YouBot":             "YouBot",
    "meta-externalagent": "meta-externalagent",
    "DuckDuckBot":        "DuckDuckBot",
    "Bingbot":            "bingbot",
    "Amazonbot":          "Amazonbot",
    "FacebookBot":        "FacebookBot",
}


def _fetch_log_lines(hours: int, mode: str = "ssh") -> list[str]:
    """Read AI-crawler-matching log lines from the last `hours`.

    mode='ssh' (default): SSH to relay host (requires env/relay-ip.txt
        + env/anp2.pem or $ANP2_SERVER_IP + $ANP2_SSH_KEY). Used when
        running the audit from an operator workstation.

    mode='local': read /var/log/caddy/access.log directly via sudo.
        Used when this script runs on the relay host itself (= cron
        on EC2 that writes the public JSON aggregate).

    Best-effort: returns [] on error. Caller should report.
    """
    since = int(time.time() - hours * 3600)
    pat = "|".join(AI_CRAWLER_PATTERNS.values())
    grep_cmd = (
        f"sudo grep -hE '({pat})' "
        f"/var/log/caddy/access.log /var/log/caddy/access.log.* 2>/dev/null "
        f"| awk -F'\"ts\":' '{{split($2,a,\",\"); if(a[1]+0>={since}) print}}'"
    )
    try:
        if mode == "local":
            r = subprocess.run(["bash", "-c", grep_cmd],
                               capture_output=True, text=True, timeout=45)
        else:
            server = (os.environ.get("ANP2_SERVER_IP")
                      or open("env/relay-ip.txt").read().strip())
            key = os.environ.get("ANP2_SSH_KEY") or "env/anp2.pem"
            r = subprocess.run(
                ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no",
                 f"ec2-user@{server}", grep_cmd],
                capture_output=True, text=True, timeout=45,
            )
        return [line for line in r.stdout.split("\n") if line.strip()]
    except Exception:
        return []


def audit(hours: int, mode: str = "ssh") -> dict:
    lines = _fetch_log_lines(hours, mode=mode)
    bot_counts: dict[str, int] = defaultdict(int)
    bot_paths: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bot_first: dict[str, float] = {}
    bot_last: dict[str, float] = {}
    bot_status: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))

    for line in lines:
        try:
            j = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        ua_list = j.get("request", {}).get("headers", {}).get("User-Agent") or []
        ua = ua_list[0] if ua_list else ""
        path = j.get("request", {}).get("uri", "")
        status = int(j.get("status") or 0)
        ts = float(j.get("ts") or 0)

        ua_low = ua.lower()
        for bot_name, pattern in AI_CRAWLER_PATTERNS.items():
            if pattern.lower() in ua_low:
                bot_counts[bot_name] += 1
                bot_paths[bot_name][path] += 1
                bot_status[bot_name][status] += 1
                if bot_name not in bot_first or ts < bot_first[bot_name]:
                    bot_first[bot_name] = ts
                if bot_name not in bot_last or ts > bot_last[bot_name]:
                    bot_last[bot_name] = ts
                break

    return {
        "hours": hours,
        "total_hits": sum(bot_counts.values()),
        "bots_seen": len(bot_counts),
        "bots": {b: {
            "count": bot_counts[b],
            "first_ts": bot_first.get(b),
            "last_ts": bot_last.get(b),
            "top_paths": sorted(bot_paths[b].items(),
                                key=lambda x: -x[1])[:5],
            "status_codes": dict(bot_status[b]),
        } for b in bot_counts},
    }


def _fmt(s: dict) -> str:
    out: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out.append(f"=== AI crawler audit ({now}, last {s['hours']}h) ===")
    out.append("")
    if not s["total_hits"]:
        out.append(f"[!] No AI crawler hits in the last {s['hours']}h.")
        out.append("    Possible reasons:")
        out.append("    - site/ deployed recently, crawlers haven't visited yet")
        out.append("    - access.log rotated out the relevant window")
        out.append("    - robots.txt / DNS / route config issue blocking crawlers")
        return "\n".join(out)
    out.append(f"[total] {s['total_hits']} hits across {s['bots_seen']} bot(s)")
    out.append("")
    for bot in sorted(s["bots"], key=lambda b: -s["bots"][b]["count"]):
        b = s["bots"][bot]
        first = datetime.fromtimestamp(
            b["first_ts"], timezone.utc).strftime("%m-%d %H:%M")
        last = datetime.fromtimestamp(
            b["last_ts"], timezone.utc).strftime("%m-%d %H:%M")
        status_str = " ".join(
            f"{code}×{n}" for code, n in sorted(b["status_codes"].items()))
        out.append(f"  {bot}: {b['count']} hits  ({first} → {last})  [{status_str}]")
        for path, n in b["top_paths"]:
            disp = path if len(path) <= 80 else path[:77] + "..."
            out.append(f"      {n:4d}× {disp}")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=int, default=24,
                    help="lookback window in hours (default 24)")
    ap.add_argument("--json", action="store_true",
                    help="output JSON instead of human-readable text")
    ap.add_argument("--output",
                    help="also write JSON output to this path (creates / overwrites)")
    ap.add_argument("--mode", choices=("ssh", "local"), default="ssh",
                    help="ssh: SSH to relay (default, for operator workstation). "
                         "local: read /var/log/caddy/access.log directly via sudo "
                         "(for cron on the relay host itself).")
    args = ap.parse_args()
    s = audit(args.hours, mode=args.mode)

    if args.json:
        rendered = json.dumps(s, indent=2, default=str)
        print(rendered)
    else:
        print(_fmt(s))

    if args.output:
        # Always JSON to file regardless of --json (since file consumers
        # are machines). Add a `generated_at` field so freshness is
        # discernible.
        s_out = dict(s)
        s_out["generated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(args.output, "w") as f:
                json.dump(s_out, f, indent=2, default=str)
        except OSError as e:
            print(f"[!] failed to write {args.output}: {e}",
                  file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
