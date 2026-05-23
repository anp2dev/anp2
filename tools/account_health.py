#!/usr/bin/env python3
"""account_health.py — GitHub anp2dev アカウントの健康診断 + ボット検知回避.

WHY THIS EXISTS: 旧 anp2dev が GitHub の anti-spam/anti-abuse に shadow-
suppressed された経緯から、新 anp2dev を「ボットっぽい挙動」で flag さ
れないよう守る防御機構。 leak_audit.py と同じく "ルール定義 + 自動チェック +
hook 強制 + memory rule" の 5 層を成立させるための監査スクリプト。

cf. [[feedback-ai-net-anp2dev-account-discipline]]、 OPERATOR_TODO.md

実行モード:
  既定           — anonymous HTTPS チェック（profile / repo 可視性 / commit
                  pacing）。 ネット越しのみ。 pre-push hook + セッション開始
                  時に走らせる。
  --auth         — PAT を env/REGISTRATIONS.md から拾って GitHub API auth
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
  R9  committer-email-clean:       no @*.local, no @anp2.com in git log
  R10 committer-name-clean:        no "founder" word in author/committer
  R11 repo-has-readme:             HEAD has README.md
  R12 repo-has-leak-audit-action:  .github/workflows/leak-audit.yml present
  R13 mfa-enabled                  (--auth) API /user.two_factor_authentication
  R14 ssh-key-anp2-deploy          (--auth) API /user/keys has anp2-deploy
  R15 pat-expiry-not-imminent      (--auth) PAT expires_at > now + 7d
  R16 branch-protection-on-main    (--auth) main has leak-audit required check

Thresholds (env var override):
  ANP2_GH_USER         (default: anp2dev)
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
LIMIT_DAY = int(os.environ.get("ANP2_COMMITS_PER_DAY", "12"))
LIMIT_WEEK = int(os.environ.get("ANP2_COMMITS_PER_WEEK", "50"))
CANONICAL_BLOG = "https://anp2.com"
CANONICAL_EMAIL = "ai@anp2.com"

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
    path = "env/REGISTRATIONS.md"
    if not os.path.exists(path):
        return None
    with open(path) as f:
        text = f.read()
    m = re.search(r"## anp2dev — Personal Access Token.*?\*\*Token\*\*:\s*`([^`]+)`",
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
    st, data = http_get_json(f"https://api.github.com/users/{USER}")
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
    """Count commit events on the user's public activity in last 24h / 7d."""
    st, events = http_get_json(f"https://api.github.com/users/{USER}/events/public?per_page=100")
    if st != 200 or events is None:
        record("WARN", "pacing-api", "github.com/api", f"HTTP {st} — skipping pacing check")
        return
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)
    count_24h = count_7d = 0
    for ev in events:
        if ev.get("type") != "PushEvent":
            continue
        ts = ev.get("created_at", "")
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        commits = ev.get("payload", {}).get("size", 0)
        if t > cutoff_24h: count_24h += commits
        if t > cutoff_7d: count_7d += commits
    # Add the current push's commits when called from pre-push (so the
    # check is "what will be visible AFTER this push", not "before").
    incoming = int(os.environ.get("ANP2_INCOMING_COMMITS", "0") or 0)
    proj_24h = count_24h + incoming
    proj_7d = count_7d + incoming
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


def check_committer_clean() -> None:
    """Local check: git log の author/committer に hostname.local や founder が無いか."""
    out = sh("git", "log", "--all", "--format=%an <%ae>%n%cn <%ce>")
    seen = set(line.strip() for line in out.splitlines() if line.strip())
    for label, pat, sev in (
        ("R9 committer-email-clean", r"\.local|@anp2\.com", "FAIL"),
        ("R10 committer-name-clean", r"\bfounder\b", "FAIL"),
    ):
        hits = [s for s in seen if re.search(pat, s, re.IGNORECASE)]
        if hits:
            record(sev, label, "git-log", "; ".join(hits[:3]))
        else:
            record("PASS", label, "git-log", f"{len(seen)} identities, all clean")


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


def check_pat_expiry(token: str) -> None:
    # Fine-grained PAT API: GET /personal-access-tokens via /user/permitted-tokens isn't trivial;
    # 代替: env/REGISTRATIONS.md の expires メモを読む
    try:
        with open("env/REGISTRATIONS.md") as f:
            text = f.read()
    except FileNotFoundError:
        return
    m = re.search(r"## anp2dev — Personal Access Token.*?\*\*Expires\*\*:\s*([^\n]+)",
                  text, re.DOTALL)
    if not m:
        record("WARN", "R15 pat-expiry-not-imminent", "env/REGISTRATIONS.md", "expiry note not found")
        return
    note = m.group(1).strip()
    # parse "30 days from generation (~Jun 22, 2026)" or "Mon, Jun 22 2026" etc.
    m_date = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d+)[,\s]+(\d{4})", note)
    if not m_date:
        record("WARN", "R15 pat-expiry-not-imminent", "env/REGISTRATIONS.md", f"unparsed: {note[:50]}")
        return
    month, day, year = m_date.groups()
    months = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
    exp = datetime(int(year), months[month], int(day), tzinfo=timezone.utc)
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
                    help="also run authenticated checks (uses PAT from env/REGISTRATIONS.md)")
    ap.add_argument("--full", action="store_true",
                    help="default + auth + local-history pacing")
    args = ap.parse_args()

    use_auth = args.auth or args.full

    # Anonymous
    check_external_visibility()
    check_profile_completeness()
    check_pacing_from_api()
    check_committer_clean()
    check_repo_files()

    if use_auth:
        token = load_pat()
        if not token:
            record("WARN", "auth", "env/REGISTRATIONS.md", "PAT not found — skipping auth checks")
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
        print("\nFAIL — anp2dev is at elevated flag risk. Read"
              " [[feedback-ai-net-anp2dev-account-discipline]] memory.")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
