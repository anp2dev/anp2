#!/usr/bin/env python3
"""followup_check.py — never miss an inbound reply/comment.

Every dev.to and Reddit notification (reply to our comment, comment on our
article, mention, follow) is delivered as email to the always-on Postfix
mailboxes on the relay (one maildir per channel; names live in internal/env). Those
mailboxes are the DURABLE capture — nothing is ever lost. The only failure
mode is latency in NOTICING. This tool closes that: it scans the mailboxes,
diffs against a handled-set, and surfaces every ACTION-needing notification
(reply / comment / mention) that has NOT been handled yet — with age and the
24h reply-obligation deadline. Because it diffs the durable mailbox against
the handled-set, ANY unhandled item surfaces whenever this runs, regardless of
how long a console was closed. Run it at session start AND on a cron.

Usage:
    tools/followup_check.py                 # list pending ACTION items + FYI count
    tools/followup_check.py --json          # machine-readable
    tools/followup_check.py --mark <file>   # mark a notification handled (after replying)

Exit: 0 nothing pending · 3 pending ACTION item(s) · 1 SSH/config error
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _read(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return ""


SERVER_IP = os.environ.get("ANP2_RELAY_IP") or _read(os.path.join(REPO, "internal", "env", "relay-ip.txt"))
KEY = os.environ.get("ANP2_SSH_KEY") or os.path.join(REPO, "internal", "env", "anp2.pem")
SEEN = os.path.join(REPO, "internal", "research", "followup_seen.log")

# Mailbox-dir -> channel mapping is operator-internal infra (the maildir names
# are internal identifiers); kept OUT of this tracked tool and loaded from
# internal/env so the public repo carries no internal mailbox names.
def _load_boxes() -> dict:
    p = os.path.join(REPO, "internal", "env", "followup_boxes.json")
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        print(f'missing/invalid {p} — create it as {{"<maildir>": "<channel>"}}',
              file=sys.stderr)
        sys.exit(1)


BOXES = _load_boxes()

# Subjects that REQUIRE an action (reply within 24h) vs FYI-only.
ACTION_RE = re.compile(
    r"replied to your comment|commented on your|mentioned you|"
    r"replied to you|new (?:reply|comment)|responded to",
    re.IGNORECASE)
FYI_RE = re.compile(
    r"just followed you|liked your|reaction|badge|digest|"
    r"new follower|upvoted|trending|weekly|newsletter",
    re.IGNORECASE)

OBLIGATION_H = 24  # reply-obligation window


def load_seen() -> set:
    try:
        with open(SEEN) as f:
            return {ln.strip() for ln in f if ln.strip()}
    except FileNotFoundError:
        return set()


def mark(fileid: str) -> None:
    os.makedirs(os.path.dirname(SEEN), exist_ok=True)
    with open(SEEN, "a") as f:
        f.write(fileid + "\n")
    print(f"marked handled: {fileid}")


def scan_remote() -> list:
    """One SSH round-trip: emit per-notification `basename<TAB>epoch<TAB>from<TAB>subject<TAB>url`."""
    remote = r'''
sudo bash -c '
for box in __ANP2_BOXES__; do
  for d in new cur; do
    dir=/var/vmail/anp2.com/$box/$d
    [ -d "$dir" ] || continue
    for f in "$dir"/*; do
      [ -f "$f" ] || continue
      from=$(grep -i "^From: " "$f" | head -1 | sed "s/^From: //I" | tr -d "\t")
      subj=$(grep -i "^Subject: " "$f" | head -1 | sed "s/^Subject: //I" | tr -d "\t")
      dt=$(grep -i "^Date: " "$f" | head -1 | sed "s/^Date: //I")
      url=$(grep -ioE "https://(dev\.to|old\.reddit\.com|www\.reddit\.com|reddit\.com)/[a-zA-Z0-9/_-]+(comment[s]?/[a-z0-9]+)?" "$f" | head -1)
      epoch=$(date -d "$dt" +%s 2>/dev/null || echo 0)
      printf "%s\t%s\t%s\t%s\t%s\n" "$box/$(basename "$f")" "$epoch" "$from" "$subj" "$url"
    done
  done
done
'
'''
    remote = remote.replace("__ANP2_BOXES__", " ".join(BOXES))
    try:
        out = subprocess.run(
            ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=20", f"ec2-user@{SERVER_IP}", remote],
            capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print(f"SSH scan failed: {e}", file=sys.stderr)
        return []
    rows = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) < 5:
            continue
        fid, epoch, frm, subj, url = parts[0], parts[1], parts[2], parts[3], parts[4]
        rows.append({"id": fid, "epoch": int(epoch or 0), "from": frm,
                     "subject": subj, "url": url,
                     "channel": BOXES.get(fid.split("/")[0], "?")})
    return rows


def classify(subj: str) -> str:
    if ACTION_RE.search(subj):
        return "ACTION"
    if FYI_RE.search(subj):
        return "FYI"
    return "OTHER"  # not a recognized notification (skip)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mark", metavar="FILEID", help="mark a notification handled")
    args = ap.parse_args()

    if args.mark:
        mark(args.mark)
        return 0
    if not SERVER_IP:
        print("relay IP not configured (ANP2_RELAY_IP or internal/env/relay-ip.txt)", file=sys.stderr)
        return 1

    seen = load_seen()
    now = int(time.time())
    rows = scan_remote()

    pending, fyi = [], 0
    for r in rows:
        kind = classify(r["subject"])
        if kind == "FYI":
            fyi += 1
            continue
        if kind != "ACTION":
            continue
        if r["id"] in seen:
            continue
        age_h = (now - r["epoch"]) / 3600 if r["epoch"] else None
        r["age_h"] = round(age_h, 1) if age_h is not None else None
        r["deadline_overdue"] = bool(age_h is not None and age_h > OBLIGATION_H)
        pending.append(r)

    pending.sort(key=lambda x: x["epoch"])

    if args.json:
        print(json.dumps({"pending": pending, "fyi_count": fyi}, ensure_ascii=False, indent=2))
        return 3 if pending else 0

    if not pending:
        print(f"✅ no pending reply-obligations. ({fyi} FYI notifications seen, no action.)")
        return 0
    print(f"🔴 {len(pending)} PENDING reply-obligation(s) — reply within {OBLIGATION_H}h, then `--mark <id>`:")
    for p in pending:
        od = "  ⚠OVERDUE" if p["deadline_overdue"] else ""
        age = f"{p['age_h']}h ago" if p["age_h"] is not None else "age?"
        print(f"  [{p['channel']}] {p['subject'][:70]}")
        print(f"      {age}{od} | {p['url'] or '(no url in body)'} | id={p['id']}")
    print(f"\n({fyi} FYI notifications, no action.)")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
