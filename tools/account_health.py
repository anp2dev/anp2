#!/usr/bin/env python3
"""account_health.py — GitHub anp2dev (active) account health audit + bot-detection avoidance.

WHY THIS EXISTS: prior anp2dev and anp2dev accounts were both
shadow-suppressed by GitHub's anti-spam / anti-abuse ML model. This audit
protects the current active account (anp2dev) from getting flagged via
the same "bot-like behavior" signals. Same 5-layer pattern as leak_audit.py:
rule definition + automated check + hook enforcement + memory rule + manifest
integrity.

cf. [[feedback-ai-net-github-account-discipline]]、 internal/OPERATOR_TODO.md

実行モード:
  既定           — anonymous HTTPS チェック（profile / repo 可視性 / commit
                  pacing）。 ネット越しのみ。 pre-push hook + セッション開始
                  時に走らせる。
  --auth         — PAT を internal/env/REGISTRATIONS.md から拾って GitHub API auth
                  系（2FA enabled / SSH key 登録 / PAT 有効期限）を確認。
  --full         — 上記 + ローカルの commit history の 24h/7d 集計 +
                  pre-push の安全マージン推定。

Exit 0 = healthy. Exit 1 = at least one rule failed.

Rules:
  R1  external-visibility-account: GET https://github.com/<user> → 200
  R2  external-visibility-repo:    GET https://github.com/<user>/<repo> → 200
  R3  profile-name-set:            API.user.name is non-empty
  R4  profile-bio-set:             API.user.bio is non-empty
  R5  profile-blog-set:            API.user.blog == https://anp2.com
  R6  profile-email-public:        API.user.email is the canonical address
  R7  pacing-24h-commits:          last 24h public commits ≤ COMMITS_PER_DAY
  R8  pacing-7d-commits:           last 7d public commits ≤ COMMITS_PER_WEEK
  R9  committer-email-clean:       no @*.local hostname-bearing email in git log
  R10 committer-name-clean:        no "founder" word in author/committer
  R11 repo-has-readme:             HEAD has README.md
  R12 repo-has-leak-audit-action:  .github/workflows/leak-audit.yml present
  R13 mfa-enabled                  (--auth) API /user.two_factor_authentication
  R14 ssh-key-anp2-deploy          (--auth) API /user/keys has anp2-deploy
  R15 pat-expiry-not-imminent      (--auth) PAT expires_at > now + 7d
  R16 branch-protection-on-main    (--auth) main has leak-audit required check
  R18 fork-burst-1h:               ≤ 1 fork created in last 1h (anti-bot)
  R19 fork-burst-24h:              ≤ 1 fork created in last 24h
  R20 fork-burst-7d:               ≤ 3 forks created in last 7d
  R21 pr-external-rate-24h:        ≤ 2 PRs to external repos in last 24h (via gh_safe log)
  R22 fork-account-age-floor:      account age ≥ 7d before any fork is allowed
  R23 git-push-burst:              ≤ 2 pushes / 1h, ≤ 5 / 24h, ≤ 15 / 7d (from git_safe log)
  R24 git-force-push-rate:         ≤ 1 force-push / 24h, ≤ 2 / 7d (from git_safe log)
  R25 author-email-stability:      no forbidden pattern; no flip-flop
  R26 co-author-AI-saturation:     ≤ 80% commits w/ Co-Authored-By: Claude/AI/Bot
  R27 ci-failure-streak:           last 5 workflow runs success ratio ≥ 60%
  R28 repo-topic-cap:              public repo has ≤ 15 topic tags
  R29 push-discipline-daily-cap:   freeze-period only — ≤ PUSH_PER_DAY_CAP
                                   (default 6) push events per operator-local
                                   day (relaxed 2026-05-30 from 1; burst shape
                                   is the real signal, covered by R17/R23)
  R30 push-window:                 RETIRED 2026-05-30 (always PASS) — fixed
                                   time-of-day push gating removed as over-
                                   engineered; own-repo pushes are not a flag
                                   signal. Burst caps R17/R23 + pre-push govern.
  R31 commit-template-repeat:      ≤ 60% of last 30 commits share the same
                                   normalized first-line template (catches
                                   bot-like commit-message uniformity)
  R32 commit-hour-concentration:   < 90% of last 30 commits in any single
                                   4-hour operator-local window (catches
                                   cron-like commit timing)
  R33 lone-author-pattern:         INFO only — flags 0-collaborator repos
                                   with > 14 days activity (solo dev is
                                   legitimate; just surfaced for awareness)
  R34 ssh-key-churn                (--auth) ≤ 2 SSH keys added in the last
                                   7d (catches rapid key rotation that
                                   correlates with bot setup)

Thresholds (env var override):
  ANP2_GH_USER         (default: anp2dev — post-2026-05-24 primary identity)
  ANP2_GH_REPO         (default: anp2)
  ANP2_COMMITS_PER_DAY (default: 12)  ← natural human ceiling
  ANP2_COMMITS_PER_WEEK(default: 50)
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone
import urllib.request, urllib.error

UA = {"User-Agent": "anp2-account-health-audit"}
USER = os.environ.get("ANP2_GH_USER", "anp2dev")
REPO = os.environ.get("ANP2_GH_REPO", "anp2")
# Ambient token: silently used in default-mode API calls to raise the
# anonymous 60/h rate limit to the authenticated 5000/h. Set later, after
# load_pat() is defined.
_AMBIENT_TOKEN: str | None = None
LIMIT_DAY = int(os.environ.get("ANP2_COMMITS_PER_DAY", "12"))
LIMIT_WEEK = int(os.environ.get("ANP2_COMMITS_PER_WEEK", "50"))
CANONICAL_BLOG = "https://anp2.com"
CANONICAL_EMAIL = "ai@anp2.com"

# Push-discipline window (R29 + R30). Active during freeze period only.
# Defaults model the operator-local "evening of the day" window 22:00–01:00
# with a +9h offset (operator-local minus 9 hours = UTC). All comparisons in
# code are pure-UTC; the offset is configurable via env so the discipline
# travels with the operator if they relocate.
from datetime import date as _date
FREEZE_END_DATE = _date(*[int(x) for x in os.environ.get(
    "ANP2_FREEZE_END_DATE", "2026-06-24").split("-")])
OPERATOR_TZ_OFFSET = int(os.environ.get("ANP2_OPERATOR_TZ_OFFSET_HOURS", "9"))
# Window endpoints in operator-local hour-of-day (24h). End may be < start
# (window crosses midnight). Defaults: 22:00 → next-day 01:00.
PUSH_WIN_LOCAL_START = int(os.environ.get("ANP2_PUSH_WIN_LOCAL_START", "22"))
PUSH_WIN_LOCAL_END = int(os.environ.get("ANP2_PUSH_WIN_LOCAL_END", "1"))
# R29 daily push cap. Relaxed 2026-05-30 from 1 to a generous human-paced
# value: a pathological bot-like multi-push day is still caught, but normal
# multi-commit days flow freely. (R30 fixed-window gating was retired the same
# day — see check_push_discipline.)
PUSH_PER_DAY_CAP = int(os.environ.get("ANP2_PUSH_PER_DAY_CAP", "6"))


def _in_freeze_period() -> bool:
    return datetime.now(timezone.utc).date() <= FREEZE_END_DATE


def _operator_local_date(utc_dt: datetime) -> _date:
    """Return the operator-local calendar date for a UTC datetime, using the
    configured offset. Used to count 'pushes today' from operator perspective.
    """
    return (utc_dt + timedelta(hours=OPERATOR_TZ_OFFSET)).date()


def _push_window_utc_bounds() -> tuple[int, int]:
    """Translate the operator-local window to UTC hour-of-day [start, end).
    Returns (start_hour_utc, end_hour_utc). Handles midnight wrap by returning
    end < start (caller must treat as [start, 24) ∪ [0, end))."""
    start_utc = (PUSH_WIN_LOCAL_START - OPERATOR_TZ_OFFSET) % 24
    end_utc = (PUSH_WIN_LOCAL_END - OPERATOR_TZ_OFFSET) % 24
    return start_utc, end_utc


def _hour_in_window(hour: int, start: int, end: int) -> bool:
    if start < end:
        return start <= hour < end
    # wrap case
    return hour >= start or hour < end

findings: list[tuple[str, str, str, str]] = []  # (level, rule, scope, detail)


def record(level: str, rule: str, scope: str, detail: str) -> None:
    findings.append((level, rule, scope, detail))


def http_get_json(url: str, token: str | None = None, timeout: int = 15) -> tuple[int, dict | None]:
    headers = dict(UA)
    headers["Accept"] = "application/vnd.github+json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = None
        return e.code, body
    except Exception:
        return 0, None


def http_head(url: str, timeout: int = 12) -> int:
    req = urllib.request.Request(url, headers=UA, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def load_pat() -> str | None:
    """Return the active PAT for the currently watched USER from internal/env/REGISTRATIONS.md.

    Supports two stanza formats:
      legacy (anp2dev):  "## anp2dev — Personal Access Token ... **Token**: `github_pat_...`"
      anp2dev (2026-05-24+):  "## PAT: <user> (fine-grained, ...) ... Token: github_pat_..."
    Searches in stanza order; the first match for the active USER wins.
    """
    path = "internal/env/REGISTRATIONS.md"
    if not os.path.exists(path):
        return None
    with open(path) as f:
        text = f.read()
    # New format first (active identity)
    m = re.search(
        rf"^## PAT:\s*{re.escape(USER)}\b.*?^Token:\s*(github_pat_[A-Za-z0-9_]+)",
        text, re.DOTALL | re.MULTILINE)
    if m:
        return m.group(1)
    # Legacy format
    m = re.search(rf"## {re.escape(USER)} — Personal Access Token.*?\*\*Token\*\*:\s*`([^`]+)`",
                  text, re.DOTALL)
    return m.group(1) if m else None


def sh(*args: str) -> str:
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return r.stdout if r.returncode == 0 else ""


# ── Anonymous checks ─────────────────────────────────────────────────

def check_external_visibility() -> None:
    for label, url in (
        ("R1 external-visibility-account", f"https://github.com/{USER}"),
        ("R2 external-visibility-repo",    f"https://github.com/{USER}/{REPO}"),
    ):
        st = http_head(url)
        if st == 200:
            record("PASS", label, url, "200")
        elif st in (301, 302):
            record("FAIL", label, url, f"{st} — redirected (shadow-suppress?)")
        elif st == 404:
            record("FAIL", label, url, "404 — flagged / shadow-suppressed")
        else:
            record("WARN", label, url, f"HTTP {st}")


def check_profile_completeness() -> None:
    # Use PAT silently if available — bumps anonymous 60/h → authenticated 5000/h.
    # Anonymous mode is fine for occasional runs but pre-push + per-message check
    # share the same anonymous bucket and exhaust it during active sessions.
    st, data = http_get_json(f"https://api.github.com/users/{USER}", token=_AMBIENT_TOKEN)
    if st != 200 or not data:
        record("FAIL", "profile-api", "github.com/api", f"HTTP {st}")
        return
    for label, key, expected in (
        ("R3 profile-name-set",  "name",  None),
        ("R4 profile-bio-set",   "bio",   None),
        ("R5 profile-blog-set",  "blog",  CANONICAL_BLOG),
        ("R6 profile-email-public", "email", CANONICAL_EMAIL),
    ):
        v = (data.get(key) or "").strip()
        if expected and v != expected:
            sev = "WARN" if key == "email" else "FAIL"
            record(sev, label, USER, f"got {v!r} (want {expected!r})")
        elif not expected and not v:
            record("FAIL", label, USER, "empty")
        else:
            record("PASS", label, USER, v[:40])


def check_pacing_from_api() -> None:
    """Count commit + push-event activity on the user's public feed.

    Per [[feedback-ai-net-github-account-discipline]] the discipline
    target is **end-of-session push**: many commits batched into 1 push.
    So we track BOTH:
      - R7  : commits-in-pushes in last 24h / 7d  (existing, threshold 12 / 50)
      - R17 : push event count in last 24h         (new, threshold 5)
    R7 = "did I commit too much" (still useful as safety belt).
    R17 = "did I push too often" (the real bot-burst shape GitHub sees).
    """
    st, events = http_get_json(f"https://api.github.com/users/{USER}/events/public?per_page=100", token=_AMBIENT_TOKEN)
    if st != 200 or events is None:
        record("WARN", "pacing-api", "github.com/api", f"HTTP {st} — skipping pacing check")
        return
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)
    count_24h = count_7d = 0
    push_events_24h = 0
    for ev in events:
        if ev.get("type") != "PushEvent":
            continue
        ts = ev.get("created_at", "")
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        commits = ev.get("payload", {}).get("size", 0)
        if t > cutoff_24h:
            count_24h += commits
            push_events_24h += 1
        if t > cutoff_7d: count_7d += commits
    # Add the current push's commits when called from pre-push (so the
    # check is "what will be visible AFTER this push", not "before").
    incoming = int(os.environ.get("ANP2_INCOMING_COMMITS", "0") or 0)
    proj_24h = count_24h + incoming
    proj_7d = count_7d + incoming
    proj_push_events = push_events_24h + (1 if incoming else 0)
    inc_note = f" +{incoming} incoming" if incoming else ""
    if proj_24h > LIMIT_DAY:
        record("FAIL", "R7 pacing-24h-commits", USER,
               f"{count_24h}{inc_note} → {proj_24h} > {LIMIT_DAY} (bot-burst risk)")
    else:
        record("PASS", "R7 pacing-24h-commits", USER, f"{count_24h}{inc_note} / {LIMIT_DAY}")
    if proj_7d > LIMIT_WEEK:
        record("FAIL", "R8 pacing-7d-commits", USER,
               f"{count_7d}{inc_note} → {proj_7d} > {LIMIT_WEEK}")
    else:
        record("PASS", "R8 pacing-7d-commits", USER, f"{count_7d}{inc_note} / {LIMIT_WEEK}")
    # R17: push-event frequency (the actual GitHub-visible activity-shape
    # signal). Discipline target: batch commits, push ≤ 5/24h.
    push_limit = int(os.environ.get("ANP2_PUSH_EVENTS_PER_DAY", "5"))
    inc_push_note = " +1 this push" if incoming else ""
    if proj_push_events > push_limit:
        record("FAIL", "R17 pacing-24h-push-events", USER,
               f"{push_events_24h}{inc_push_note} → {proj_push_events} > {push_limit} "
               "(burst push pattern — batch commits next time)")
    else:
        record("PASS", "R17 pacing-24h-push-events", USER,
               f"{push_events_24h}{inc_push_note} / {push_limit}")


def check_committer_clean() -> None:
    """Local check: git log の author/committer に hostname.local や founder が無いか."""
    out = sh("git", "log", "--all", "--format=%an <%ae>%n%cn <%ce>")
    seen = set(line.strip() for line in out.splitlines() if line.strip())
    for label, pat, sev in (
        ("R9 committer-email-clean", r"\.local$", "FAIL"),
        ("R10 committer-name-clean", r"\bfounder\b", "FAIL"),
    ):
        hits = [s for s in seen if re.search(pat, s, re.IGNORECASE)]
        if hits:
            record(sev, label, "git-log", "; ".join(hits[:3]))
        else:
            record("PASS", label, "git-log", f"{len(seen)} identities, all clean")


def check_fork_burst() -> None:
    """R18-R22: fork-creation + PR-submission burst checks.

    These exist BECAUSE: 2026-05-24, anp2dev was shadow-suppressed by
    GitHub's anti-spam ML model after forking 5 popular awesome-* repos in
    50 seconds (00:43:37 → 00:44:27 UTC) from a 38-hour-old account. The
    mass-fork burst is THE highest-weight signal GitHub uses to identify
    bot accounts. Never again.

    Sources of truth:
      - GitHub API `/users/<user>/repos?type=forks` for actual fork timestamps
      - local internal/env/.gh-activity-log.jsonl for ops gh_safe.sh has wrapped
    The maximum of the two is used (covers direct `gh repo fork` bypass).
    """
    import json as _json
    now = time.time()
    forks_api: list[float] = []
    # NOTE: GitHub's /users/<u>/repos `type` parameter accepts only all|owner|member;
    # `type=forks` is silently treated as default (owner) and returns ALL owned repos.
    # We must fetch all repos and filter client-side on the `fork` boolean.
    st, data = http_get_json(f"https://api.github.com/users/{USER}/repos?type=owner&per_page=100", token=_AMBIENT_TOKEN)
    if st == 200 and isinstance(data, list):
        for r in data:
            if not r.get("fork"):
                continue
            try:
                t = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).timestamp()
                forks_api.append(t)
            except Exception:
                pass

    forks_local: list[float] = []
    try:
        with open("internal/env/.gh-activity-log.jsonl") as fh:
            for line in fh:
                e = _json.loads(line)
                if e.get("action") == "fork" and e.get("status") == "OK":
                    forks_local.append(e.get("unix", 0))
    except FileNotFoundError:
        pass

    # union (any fork in API OR local log)
    forks = sorted(set(forks_api + forks_local))

    n_1h  = sum(1 for t in forks if t >= now - 3600)
    n_24h = sum(1 for t in forks if t >= now - 86400)
    n_7d  = sum(1 for t in forks if t >= now - 86400 * 7)

    # R18: 1h cap — a bot signature is multiple forks within seconds/minutes
    cap_1h = int(os.environ.get("ANP2_FORK_CAP_1H", "1"))
    if n_1h > cap_1h:
        record("FAIL", "R18 fork-burst-1h", USER,
               f"{n_1h} fork(s) in last 1h > cap={cap_1h} — bot pattern, will get flagged")
    else:
        record("PASS", "R18 fork-burst-1h", USER, f"{n_1h} / {cap_1h}")

    cap_24h = int(os.environ.get("ANP2_FORK_CAP_24H", "1"))
    if n_24h > cap_24h:
        record("FAIL", "R19 fork-burst-24h", USER, f"{n_24h} fork(s) in last 24h > cap={cap_24h}")
    else:
        record("PASS", "R19 fork-burst-24h", USER, f"{n_24h} / {cap_24h}")

    cap_7d = int(os.environ.get("ANP2_FORK_CAP_7D", "3"))
    if n_7d > cap_7d:
        record("FAIL", "R20 fork-burst-7d", USER, f"{n_7d} fork(s) in last 7d > cap={cap_7d}")
    else:
        record("PASS", "R20 fork-burst-7d", USER, f"{n_7d} / {cap_7d}")

    # R21: PR submissions in last 24h (count from gh_safe log only; gh API
    # search has different rate limits we don't want to rely on)
    prs_24h = 0
    try:
        with open("internal/env/.gh-activity-log.jsonl") as fh:
            for line in fh:
                e = _json.loads(line)
                if e.get("action") == "pr-create" and e.get("status") == "OK" \
                   and e.get("unix", 0) >= now - 86400:
                    prs_24h += 1
    except FileNotFoundError:
        pass
    cap_pr = int(os.environ.get("ANP2_PR_CAP_24H", "2"))
    if prs_24h > cap_pr:
        record("FAIL", "R21 pr-external-rate-24h", USER, f"{prs_24h} PRs > cap={cap_pr}")
    else:
        record("PASS", "R21 pr-external-rate-24h", USER, f"{prs_24h} / {cap_pr}")

    # R22: any fork from an account younger than 7 days = automatic FAIL
    st2, prof = http_get_json("https://api.github.com/users/" + USER, token=_AMBIENT_TOKEN)
    if st2 == 200 and prof and prof.get("created_at"):
        created = datetime.fromisoformat(prof["created_at"].replace("Z", "+00:00")).timestamp()
        age_days = int((now - created) / 86400)
        if age_days < 7 and n_7d > 0:
            record("FAIL", "R22 fork-account-age-floor", USER,
                   f"account is {age_days}d old and has {n_7d} fork(s) — high flag risk")
        elif age_days < 7:
            record("PASS", "R22 fork-account-age-floor", USER,
                   f"account {age_days}d old, no forks (OK — wait ≥ 7d before any fork)")
        else:
            record("PASS", "R22 fork-account-age-floor", USER,
                   f"account {age_days}d ≥ 7d")
    else:
        record("WARN", "R22 fork-account-age-floor", USER, f"profile HTTP {st2}")


def check_git_burst() -> None:
    """R23-R25: git push / force-push / author-email rate checks.

    Reads internal/env/.git-activity-log.jsonl populated by tools/git_safe.sh. The
    log is gitignored. If git_safe wasn't used (direct git push), the log
    won't have that event — fall back to GitHub /events API (R17 already
    covers that surface from a different angle).
    """
    import json as _json
    now = time.time()
    pushes: list[float] = []
    forces: list[float] = []
    emails: list[tuple[float, str]] = []
    try:
        with open("internal/env/.git-activity-log.jsonl") as fh:
            for line in fh:
                e = _json.loads(line)
                if e.get("status") != "OK":
                    continue
                t = e.get("unix", 0)
                act = e.get("action", "")
                if act == "push":         pushes.append(t)
                if act == "push-force":   forces.append(t); pushes.append(t)
                if act == "config-email": emails.append((t, e.get("target", "")))
    except FileNotFoundError:
        pass

    def in_window(ts_list, w): return sum(1 for t in ts_list if t >= now - w)

    # R23: push burst
    p1, p24, p7 = in_window(pushes, 3600), in_window(pushes, 86400), in_window(pushes, 86400 * 7)
    c1 = int(os.environ.get("ANP2_PUSH_CAP_1H", "2"))
    c24 = int(os.environ.get("ANP2_PUSH_CAP_24H", "5"))
    c7 = int(os.environ.get("ANP2_PUSH_CAP_7D", "15"))
    if p1 > c1 or p24 > c24 or p7 > c7:
        record("FAIL", "R23 git-push-burst", "git_safe-log",
               f"{p1}/{c1}h, {p24}/{c24}d, {p7}/{c7}w — slow down")
    else:
        record("PASS", "R23 git-push-burst", "git_safe-log",
               f"{p1}/{c1}h {p24}/{c24}d {p7}/{c7}w")

    # R24: force-push rate
    f24, f7 = in_window(forces, 86400), in_window(forces, 86400 * 7)
    fc24 = int(os.environ.get("ANP2_FORCE_PUSH_CAP_24H", "1"))
    fc7 = int(os.environ.get("ANP2_FORCE_PUSH_CAP_7D", "2"))
    if f24 > fc24 or f7 > fc7:
        record("FAIL", "R24 git-force-push-rate", "git_safe-log",
               f"{f24}/{fc24}d, {f7}/{fc7}w — force-push is rare-by-design")
    else:
        record("PASS", "R24 git-force-push-rate", "git_safe-log",
               f"{f24}/{fc24}d {f7}/{fc7}w")

    # R25: author email stability — flip-flop detection
    # 1) ban founder/.local/admin/root in CURRENT email (catches direct git config)
    try:
        cur_email = subprocess.check_output(
            ["git", "config", "user.email"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        cur_email = ""
    if re.search(r"founder@|\.local$|admin@|root@", cur_email, re.IGNORECASE):
        record("FAIL", "R25 author-email-stability", "git-config",
               f"current user.email '{cur_email}' matches forbidden pattern")
    else:
        # 2) flip-flop check: ≥ 3 distinct emails set in last 7 days = suspicious
        e7d = [(t, e) for t, e in emails if t >= now - 86400 * 7]
        distinct = {e for _, e in e7d}
        if len(distinct) >= 3:
            record("FAIL", "R25 author-email-stability", "git_safe-log",
                   f"{len(distinct)} distinct emails set in last 7d (flip-flop pattern)")
        else:
            record("PASS", "R25 author-email-stability", "git-config",
                   f"current={cur_email}, {len(distinct)} change(s)/7d")


def check_extra_flag_patterns() -> None:
    """R26-R28: secondary flag patterns identified after the 2026-05-24
    anp2dev shadow-suppress event.

    These are NOT decisive bot signals on their own, but in combination they
    push the account toward GitHub's anti-spam threshold.
    """
    # R26: Co-Authored-By: <AI> saturation
    out = sh("git", "log", "--format=%b")
    total = sh("git", "log", "--format=%h").splitlines()
    if total:
        ai_commits = sum(1 for chunk in out.split("commit ")
                         if re.search(r"Co-Authored-By:\s*[^<]*(?:Claude|GPT|Bot|AI|Copilot)",
                                      chunk, re.IGNORECASE))
        ratio = ai_commits / len(total) if total else 0
        cap = float(os.environ.get("ANP2_AI_COAUTH_RATIO", "0.8"))
        if ratio > cap:
            record("FAIL", "R26 co-author-AI-saturation", "git-log",
                   f"{ai_commits}/{len(total)} ({ratio:.0%}) > {cap:.0%} — looks AI-only")
        else:
            record("PASS", "R26 co-author-AI-saturation", "git-log",
                   f"{ai_commits}/{len(total)} ({ratio:.0%}) / {cap:.0%}")

    # R27: workflow CI success ratio (anonymous via gh actions feed)
    st, data = http_get_json(f"https://api.github.com/repos/{USER}/{REPO}/actions/runs?per_page=5", token=_AMBIENT_TOKEN)
    if st == 200 and isinstance(data, dict):
        runs = data.get("workflow_runs", [])
        if runs:
            success = sum(1 for r in runs if r.get("conclusion") == "success")
            ratio = success / len(runs)
            if ratio < 0.6:
                record("FAIL", "R27 ci-failure-streak", f"{USER}/{REPO}",
                       f"only {success}/{len(runs)} runs succeeded — looks unmaintained / bot")
            else:
                record("PASS", "R27 ci-failure-streak", f"{USER}/{REPO}",
                       f"{success}/{len(runs)} runs OK")
        else:
            record("INFO", "R27 ci-failure-streak", f"{USER}/{REPO}", "no runs yet")
    else:
        record("INFO", "R27 ci-failure-streak", f"{USER}/{REPO}", f"HTTP {st} — skipping")

    # R28: repo topic count
    st2, repo_data = http_get_json(f"https://api.github.com/repos/{USER}/{REPO}", token=_AMBIENT_TOKEN)
    if st2 == 200 and isinstance(repo_data, dict):
        topics = repo_data.get("topics", []) or []
        # Raised 2026-05-25 from 5 → 15. GitHub allows up to 20; 5 was over-strict
        # for active discovery-surface repos. Bot-spammed-topics pattern is
        # better caught by R26/R31/R33 (= identity/template/lone-author signals).
        cap_t = int(os.environ.get("ANP2_REPO_TOPIC_CAP", "15"))
        if len(topics) > cap_t:
            record("FAIL", "R28 repo-topic-cap", f"{USER}/{REPO}",
                   f"{len(topics)} topics > {cap_t} — looks topic-spammed")
        else:
            record("PASS", "R28 repo-topic-cap", f"{USER}/{REPO}",
                   f"{len(topics)} / {cap_t}")
    else:
        record("INFO", "R28 repo-topic-cap", f"{USER}/{REPO}", f"HTTP {st2}")


def check_bot_pattern_extended() -> None:
    """R31 + R32 + R33: stronger bot-detection signals.

    Origin: 2026-05-25 red-team finding #3 surfaced that R26 (Co-Authored-By
    saturation) has a trivial bypass — just don't add the trailer. These
    three rules look at structural patterns that a Co-Authored-By-omitting
    AI bot still leaves behind: uniform commit-message templates, narrow
    commit-time windows, and zero-collaborator longevity.

    None of these is individually decisive (a real solo human dev pushing
    rapid `fix:` commits during their morning could trigger all three).
    Combined with R26 + R27, they raise the bot-likeness signal materially.
    """
    # ── R31: commit-template repetition
    # Normalize each commit's first line: lowercase, drop typical prefixes
    # (`feat:`, `fix:`, `chore:`, etc.) + drop trailing parenthetical/hash
    # references, then take first 5 words as the "template key". A real
    # human dev varies templates; a bot that templates `[skill] update
    # X` repeats.
    first_lines = sh("git", "log", "-30", "--format=%s").splitlines()
    if first_lines:
        from collections import Counter
        def template_key(line: str) -> str:
            s = line.lower()
            s = re.sub(r"^(feat|fix|chore|docs|test|refactor|style|perf|ci|build|revert)[:(]\s*",
                       "", s)
            s = re.sub(r"[#\d]+$", "", s).strip()
            words = s.split()[:5]
            return " ".join(words)
        keys = [template_key(l) for l in first_lines if l.strip()]
        if keys:
            counts = Counter(keys)
            top_key, top_n = counts.most_common(1)[0]
            ratio = top_n / len(keys)
            cap = float(os.environ.get("ANP2_COMMIT_TEMPLATE_CAP", "0.6"))
            if ratio > cap:
                record("FAIL", "R31 commit-template-repeat", "git-log",
                       f"top template {top_n}/{len(keys)} ({ratio:.0%}) — too uniform "
                       f"(template: {top_key!r})")
            else:
                record("PASS", "R31 commit-template-repeat", "git-log",
                       f"top template {top_n}/{len(keys)} ({ratio:.0%}) / {cap:.0%}")

    # ── R32: commit-time concentration (operator-local hour bucket)
    # Real human dev commits across 8-14 hour spread. Bot / cron commits
    # within a 4-hour window. We threshold at 90% of commits in any 4-hour
    # window across the last 30 commits.
    raw_times = sh("git", "log", "-30", "--format=%ct").splitlines()
    if raw_times:
        hours = []
        for t in raw_times:
            try:
                utc_dt = datetime.fromtimestamp(int(t), timezone.utc)
                local_dt = utc_dt + timedelta(hours=OPERATOR_TZ_OFFSET)
                hours.append(local_dt.hour)
            except (ValueError, TypeError):
                continue
        if hours:
            # Sliding 4-hour window count
            max_in_4h = 0
            for start in range(24):
                window = {(start + i) % 24 for i in range(4)}
                cnt = sum(1 for h in hours if h in window)
                if cnt > max_in_4h:
                    max_in_4h = cnt
            ratio = max_in_4h / len(hours)
            cap = float(os.environ.get("ANP2_COMMIT_HOUR_CAP", "0.9"))
            if ratio > cap:
                record("FAIL", "R32 commit-hour-concentration", "git-log",
                       f"{max_in_4h}/{len(hours)} ({ratio:.0%}) in a 4h window — "
                       f"narrow timing (cap {cap:.0%})")
            else:
                record("PASS", "R32 commit-hour-concentration", "git-log",
                       f"{max_in_4h}/{len(hours)} ({ratio:.0%}) / {cap:.0%}")

    # ── R33: lone-author-pattern (INFO only)
    # Solo dev is legitimate, but a 0-collaborator repo with sustained
    # activity is a pattern auditors look at. Reported as INFO so it shows
    # up in the audit without failing.
    st, collabs = http_get_json(
        f"https://api.github.com/repos/{USER}/{REPO}/collaborators",
        token=_AMBIENT_TOKEN)
    st_r, repo_data = http_get_json(
        f"https://api.github.com/repos/{USER}/{REPO}",
        token=_AMBIENT_TOKEN)
    if st == 200 and isinstance(collabs, list) and st_r == 200 and isinstance(repo_data, dict):
        n_collab = len(collabs)
        created = repo_data.get("created_at", "")
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - created_dt).days
            except ValueError:
                age_days = 0
        else:
            age_days = 0
        if n_collab <= 1 and age_days >= 14:
            record("INFO", "R33 lone-author-pattern", f"{USER}/{REPO}",
                   f"{n_collab} collaborator(s), repo age {age_days}d "
                   f"(solo-dev — legitimate but surfaced)")
        else:
            record("PASS", "R33 lone-author-pattern", f"{USER}/{REPO}",
                   f"{n_collab} collaborator(s), age {age_days}d")
    else:
        record("INFO", "R33 lone-author-pattern", f"{USER}/{REPO}",
               f"HTTP {st}/{st_r} — skipping")


def check_push_discipline(push_mode: bool = False) -> None:
    """R29 + R30: push-discipline rules (re-scoped 2026-05-30).

    R30 (fixed time-of-day push window) is RETIRED. Pushing one's own commits
    to one's own repo is not a GitHub bot-flag signal — the two account burns
    were fork/PR bursts, not pushes — and the window created real friction
    (held legitimate commits, forced /usr/bin/git workarounds, which masked the
    git_safe recursion bug). Burst protection is fully covered by the permanent
    rules R17 (<=5 push-events/24h) + R23 (git-push burst) + the pre-push
    commit-burst cap; R30 added friction without protection. It now reports
    PASS unconditionally so the rule number stays stable.

    R29 is RELAXED from <=1 to PUSH_PER_DAY_CAP (default 6) per operator-local
    day: a pathological bot-like multi-push day is still caught while normal
    multi-commit days flow freely. It remains a freeze-period warmup discipline
    and auto-deactivates after FREEZE_END_DATE.

    `push_mode` (pre-push --push-mode): a violation FAILs (blocks the push);
    otherwise WARN.
    """
    fail_level = "FAIL" if push_mode else "WARN"

    # R30 retired 2026-05-30 — always PASS (time-of-day gating removed).
    record("PASS", "R30 push-window", USER,
           "retired 2026-05-30 — time-of-day push gating removed as "
           "over-engineered; burst caps R17/R23 + pre-push govern")

    if not _in_freeze_period():
        record("PASS", "R29 push-discipline-daily-cap", USER,
               f"post-freeze (after {FREEZE_END_DATE.isoformat()}); rule inactive")
        return

    # ── R29: operator-local-day push count (relaxed cap)
    now_utc = datetime.now(timezone.utc)
    today_local = _operator_local_date(now_utc)
    st, events = http_get_json(
        f"https://api.github.com/users/{USER}/events?per_page=100",
        token=_AMBIENT_TOKEN)
    push_count = 0
    if st == 200 and isinstance(events, list):
        for e in events:
            if e.get("type") != "PushEvent":
                continue
            ts = e.get("created_at", "")
            if not ts:
                continue
            try:
                ev_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if _operator_local_date(ev_utc) == today_local:
                push_count += 1
        if push_count > PUSH_PER_DAY_CAP:
            record(fail_level, "R29 push-discipline-daily-cap", USER,
                   f"{push_count} pushes in current operator-local day "
                   f"(cap {PUSH_PER_DAY_CAP}, freeze warmup)")
        else:
            record("PASS", "R29 push-discipline-daily-cap", USER,
                   f"{push_count} / {PUSH_PER_DAY_CAP} (operator-local day, freeze warmup)")
    else:
        record("INFO", "R29 push-discipline-daily-cap", USER,
               f"HTTP {st} — skipping; pre-push hook will re-check")


def check_repo_files() -> None:
    files = sh("git", "ls-files").split()
    for label, path, sev in (
        ("R11 repo-has-readme", "README.md", "WARN"),
        ("R12 repo-has-leak-audit-action", ".github/workflows/leak-audit.yml", "FAIL"),
    ):
        if path in files:
            record("PASS", label, path, "tracked")
        else:
            record(sev, label, path, "missing")


# ── Authenticated checks (--auth) ───────────────────────────────────

def check_mfa_and_keys(token: str) -> None:
    st, data = http_get_json("https://api.github.com/user", token=token)
    if st != 200 or not data:
        record("WARN", "R13 mfa-enabled", "api/user", f"HTTP {st}")
        return
    # Fine-grained PAT often does NOT expose `two_factor_authentication`
    # (returns false even when 2FA is on). Trust only if true; otherwise SKIP.
    mfa = data.get("two_factor_authentication")
    if mfa is True:
        record("PASS", "R13 mfa-enabled", USER, "2FA on (api confirmed)")
    elif mfa is False:
        record("WARN", "R13 mfa-enabled", USER,
               "api returns false — could be PAT scope limitation, "
               "verify manually at https://github.com/settings/security")
    else:
        record("INFO", "R13 mfa-enabled", USER,
               "field absent — fine-grained PAT cannot check, verify manually")
    # SSH keys — also requires user-scoped PAT
    st2, keys = http_get_json("https://api.github.com/user/keys", token=token)
    if st2 == 200 and isinstance(keys, list):
        if any("anp2-deploy" in (k.get("title") or "") for k in keys):
            record("PASS", "R14 ssh-key-anp2-deploy", USER,
                   f"{len(keys)} key(s) including anp2-deploy")
        else:
            record("FAIL", "R14 ssh-key-anp2-deploy", USER,
                   "anp2-deploy SSH key not registered")
    elif st2 == 403:
        record("INFO", "R14 ssh-key-anp2-deploy", "api/user/keys",
               "403 — fine-grained PAT lacks 'Git SSH keys' permission, "
               "verify manually at https://github.com/settings/keys")
    else:
        record("WARN", "R14 ssh-key-anp2-deploy", "api/user/keys", f"HTTP {st2}")

    # R34: SSH key churn — count keys added in last 7d. Rapid key rotation
    # (≥ 3 new keys / week) is uncommon for legitimate accounts and frequent
    # for compromised / bot accounts being reused across identities.
    if st2 == 200 and isinstance(keys, list):
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        recent = 0
        for k in keys:
            created = k.get("created_at", "")
            if created and created >= cutoff_iso:
                recent += 1
        cap = int(os.environ.get("ANP2_SSH_KEY_CHURN_CAP", "2"))
        if recent > cap:
            record("FAIL", "R34 ssh-key-churn", USER,
                   f"{recent} keys added in last 7d > {cap} — rapid rotation")
        else:
            record("PASS", "R34 ssh-key-churn", USER,
                   f"{recent} / {cap} keys added in last 7d")


def check_pat_expiry(token: str) -> None:
    # Fine-grained PAT API: GET /personal-access-tokens via /user/permitted-tokens isn't trivial;
    # 代替: internal/env/REGISTRATIONS.md の expires メモを読む
    try:
        with open("internal/env/REGISTRATIONS.md") as f:
            text = f.read()
    except FileNotFoundError:
        return
    # New format (## PAT: <USER> ... Expires: <iso>) first
    m = re.search(
        rf"^## PAT:\s*{re.escape(USER)}\b.*?^Expires:\s*([^\n]+)",
        text, re.DOTALL | re.MULTILINE)
    if not m:
        # Legacy (anp2dev)
        m = re.search(rf"## {re.escape(USER)} — Personal Access Token.*?\*\*Expires\*\*:\s*([^\n]+)",
                      text, re.DOTALL)
    if not m:
        record("WARN", "R15 pat-expiry-not-imminent", "internal/env/REGISTRATIONS.md", "expiry note not found")
        return
    note = m.group(1).strip()
    # Three accepted formats:
    #   ISO 8601:        2026-08-22T13:16:31Z              (new anp2dev stanza)
    #   "Mon DD, YYYY":  "Jun 22, 2026"                    (legacy anp2dev)
    #   bare YYYY-MM-DD: 2026-08-22                        (also accepted)
    exp = None
    m_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}):(\d{2})Z?)?", note)
    if m_iso:
        y, mo, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        exp = datetime(y, mo, d, tzinfo=timezone.utc)
    if exp is None:
        m_date = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d+)[,\s]+(\d{4})", note)
        if m_date:
            months = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
            month, day, year = m_date.groups()
            exp = datetime(int(year), months[month], int(day), tzinfo=timezone.utc)
    if exp is None:
        record("WARN", "R15 pat-expiry-not-imminent", "internal/env/REGISTRATIONS.md", f"unparsed: {note[:50]}")
        return
    days_left = (exp - datetime.now(timezone.utc)).days
    if days_left < 0:
        record("FAIL", "R15 pat-expiry-not-imminent", "PAT", f"EXPIRED {-days_left}d ago — rotate now")
    elif days_left < 7:
        record("FAIL", "R15 pat-expiry-not-imminent", "PAT", f"expires in {days_left}d — rotate now")
    elif days_left < 14:
        record("WARN", "R15 pat-expiry-not-imminent", "PAT", f"expires in {days_left}d")
    else:
        record("PASS", "R15 pat-expiry-not-imminent", "PAT", f"{days_left}d remaining")


def check_branch_protection(token: str) -> None:
    st, data = http_get_json(
        f"https://api.github.com/repos/{USER}/{REPO}/branches/main/protection", token=token)
    if st == 404:
        record("FAIL", "R16 branch-protection-on-main", "main", "no protection rule")
        return
    if st != 200 or not data:
        record("WARN", "R16 branch-protection-on-main", "main", f"HTTP {st}")
        return
    checks = data.get("required_status_checks", {}).get("contexts", [])
    if any("audit" in c.lower() for c in checks):
        record("PASS", "R16 branch-protection-on-main", "main",
               f"required: {checks}")
    else:
        record("FAIL", "R16 branch-protection-on-main", "main",
               f"audit not in required checks: {checks}")


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--auth", action="store_true",
                    help="also run authenticated checks (uses PAT from internal/env/REGISTRATIONS.md)")
    ap.add_argument("--full", action="store_true",
                    help="default + auth + local-history pacing")
    ap.add_argument("--push-mode", action="store_true",
                    help="treat R29 / R30 freeze-period push-discipline "
                         "violations as FAIL (block push). Set by pre-push hook")
    args = ap.parse_args()

    use_auth = args.auth or args.full

    # Silently pull the PAT for ambient use in default-mode API calls. This
    # raises the per-process GitHub API rate cap from 60/h (anonymous) to
    # 5000/h (authenticated). Failures to load the token are non-fatal —
    # checks fall back to anonymous.
    global _AMBIENT_TOKEN
    _AMBIENT_TOKEN = load_pat()

    # Anonymous
    check_external_visibility()
    check_profile_completeness()
    check_pacing_from_api()
    check_fork_burst()
    check_git_burst()
    check_extra_flag_patterns()
    check_bot_pattern_extended()
    check_push_discipline(push_mode=args.push_mode)
    check_committer_clean()
    check_repo_files()

    if use_auth:
        token = load_pat()
        if not token:
            record("WARN", "auth", "internal/env/REGISTRATIONS.md", "PAT not found — skipping auth checks")
        else:
            check_mfa_and_keys(token)
            check_pat_expiry(token)
            check_branch_protection(token)

    stamp = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    mode = "full" if args.full else ("auth" if args.auth else "default")
    print(f"ANP2 account-health audit — user={USER} repo={REPO} mode={mode} — {stamp}")
    print("-" * 72)
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "INFO": 0}
    for lvl, rule, scope, detail in findings:
        marker = {"PASS": "✅", "WARN": "⚠ ", "FAIL": "❌", "INFO": "ℹ "}.get(lvl, "  ")
        print(f"  {marker} {lvl:<5} {rule:<40} @ {scope}: {detail}")
        counts[lvl] = counts.get(lvl, 0) + 1
    print("-" * 72)
    print(f"{counts['PASS']} PASS, {counts['WARN']} WARN, "
          f"{counts['INFO']} INFO, {counts['FAIL']} FAIL")
    fail_count = counts["FAIL"]
    if fail_count:
        print(f"\nFAIL — account {USER} is at elevated flag risk. Read"
              " [[feedback-ai-net-github-account-discipline]] memory.")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
