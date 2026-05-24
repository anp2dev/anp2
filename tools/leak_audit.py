#!/usr/bin/env python3
"""leak_audit.py — ANP2 repository leak audit.

WHY THIS EXISTS: a one-off `GITHUB_PUBLIC_RELEASE_AUDIT.md` was done once but
its protections decayed silently — subsequent commits re-introduced the relay
IP, kept committing under a hostname-bearing author identity, and re-tracked
internal-only files. This script is the always-on replacement: a runnable
audit that any operator (or pre-commit hook, or session-start routine) can
fire to confirm the repo is publish-safe.

Checks:
  - content: leak strings (relay IP, operator IP, hostnames, operator email)
             in tracked files, staged diffs, and optionally full git history
  - path:    internal-only paths (memory/, docs/research/, OPERATOR_*.md) that
             must never be tracked
  - author:  commit author / committer fields carrying hostname or human-role
             words ("founder", "*.local", etc.)
  - stash + reflog: residual references to leaks outside the commit DAG

Modes:
  default       — HEAD tracked files + authors + stash + reflog (fast)
  --staged      — only staged diff (pre-commit hook variant; very fast)
  --full        — everything above + scan every blob in `git log --all -p`
                  (slow on large repos; run before any push)

Exit 0 = clean. Exit 1 = at least one FAIL. No third-party deps.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from typing import Iterable

# ── Leak rules ────────────────────────────────────────────────────────────
# Each rule: (name, kind, pattern, severity, description).
# kind ∈ {"content", "path", "author"}.
RULES: list[tuple[str, str, str, str, str]] = [
    # — Infrastructure leaks (HIGH) —
    ("relay-ip",
     "content", r"\b0.0.0.0\b", "HIGH",
     "live relay public IP — must be env-var, never in source"),
    ("operator-ip",
     "content", r"\b0.0.0.0\b", "HIGH",
     "recurring operator machine IP"),
    ("hostname-redacted-host",
     "content", r"\bredacted-host\b", "HIGH",
     "operator's Mac mini hostname"),
    ("hostname-redacted-host",
     "content", r"\bredacted-host\b", "HIGH",
     "operator's other Mac mini hostname"),
    ("operator-gmail",
     "content", r"\b***\b", "CRITICAL",
     "operator's personal email"),
    ("dotlocal-host",
     "content", r"\b[a-z][a-z0-9-]*\.local\b", "MEDIUM",
     "any *.local mDNS hostname in tracked content"),
    # — Email / identity leaks in content (HIGH) —
    ("content-founder-email",
     "content", r"\bfounder@", "HIGH",
     "email local-part 'founder' in tracked content "
     "(rule: implies a human founder)"),
    ("content-anp2-email",
     "content", r"\b[a-z][a-z0-9._+-]*@anp2\.com\b", "MEDIUM",
     "legacy ANP2 brand in an email address (rule)"),
    # — Operational / paid-service leaks (MEDIUM) —
    ("protonmail-plus",
     "content", r"ProtonMail\s*Plus", "MEDIUM",
     "paid-mail-service-tier disclosure (operational infra leak)"),
    ("paid-plan",
     "content", r"\(paid plan\)", "MEDIUM",
     "paid-plan disclosure"),
    ("internal-doc-ref-in-public",
     "content", r"\bOPERATOR_(?:TODO|RUNBOOK|NOTES)\.md\b", "HIGH",
     "tracked file references an internal-only OPERATOR_*.md "
     "(broken pointer + leak)"),
    # — rule (human-existence) content patterns (HIGH) —
    ("content-human-operator",
     "content", r"\bhuman[\s-]+(?:operator|maintainer|admin|contributor)s?\b", "HIGH",
     "rule: explicit 'human X' role mention"),
    ("content-the-operator-bare",
     "content",
     # Match "the operator" as a noun referring to a person. Exclude the
     # prescribed adjective forms: "the operator agent/agents/seed/seeds",
     # "the operator-issued/attention/gated/...", "the operator's seed/agent".
     r"\bthe operator(?!\s+(?:agent|agents|seed|seeds)\b|[-’\']\w)",
     "MEDIUM",
     "rule: bare 'the operator' usage — route through 'operator agent'"),
    ("content-founder-word",
     "content", r"\bfounder(?:s)?\b", "MEDIUM",
     "rule: 'founder' word in operator-authored text — "
     "use 'seed multisig' / 'seed authority' per PROTOCOL §14.7"),
    # — rule (JP-origin) tight patterns —
    ("content-xx-en-pair",
     "content", r"\bja[_\-]en\b|translate\.text\.ja\b", "HIGH",
     "rule: explicit xx_en or translate.text.xx* signal"),
    ("content-jp-text",
     "content", r"[x]|\bJST\b|UTC", "HIGH",
     "rule: JP text / UTC / UTC timezone"),
    # — rule (promotion-operation) patterns —
    ("content-show-hn",
     "content", r"\bShow HN\b|\bHacker News\b", "MEDIUM",
     "rule: Hacker News / Show HN as a promotion target"),
    ("content-devto-publish",
     "content", r"\bDEV\.to\s+(?:publish|post)\b", "MEDIUM",
     "rule: DEV.to as a promotion target"),
    ("content-outreach-op",
     "content", r"\boutreach\s+(?:email|plan|operation|campaign|calendar)\b",
     "MEDIUM",
     "rule: outreach operation disclosure"),
    # — Path rules: internal-only files must never be tracked —
    ("internal-memory",
     "path", r"^memory/", "HIGH",
     "memory/ is internal-only; gitignore it"),
    ("internal-research",
     "path", r"^docs/research/", "HIGH",
     "docs/research/ is internal-only; gitignore it"),
    ("internal-operator-md",
     "path", r"^OPERATOR_(TODO|RUNBOOK|NOTES)\.md$", "HIGH",
     "OPERATOR_*.md is internal-only; gitignore it"),
    ("internal-env",
     "path", r"^env/", "CRITICAL",
     "env/ holds private keys + passwords; must never be tracked"),
    # — Filename leaks (path-rule extension for non-ASCII / AI-tool trace) —
    # Added 2026-05-23 after `logo/ChatGPT Image 2026年5月19日 17_16_57.png`
    # was found tracked: JP date in filename (rule) + AI-tool origin trace.
    # Both current-HEAD and historical paths are scanned (path rules run
    # in check_full_history too — see below).
    ("filename-jp-chars",
     "path", r"[ぁ-んァ-ヶ一-龥]", "HIGH",
     "JP-origin characters in tracked filename (rule)"),
    ("filename-jp-date",
     "path", r"\d{4}年|\d+月\d+日", "HIGH",
     "JP-format date in tracked filename"),
    ("filename-ai-gen-trace",
     "path", r"(?i)\bChatGPT[\s_-]Image|\bmidjourney\b|\bstable.diffusion\b",
     "MEDIUM",
     "tracked filename advertises AI-tool generation origin"),
    # — Credential / key leaks (CRITICAL) —
    ("bcrypt-hash",
     "content", r"\$2[aby]\$\d{1,2}\$[./A-Za-z0-9]{53}", "CRITICAL",
     "bcrypt hash literal — credential, never in source"),
    ("pem-private-key",
     "content", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |)?PRIVATE KEY-----",
     "CRITICAL",
     "PEM private-key block"),
    ("aws-access-key",
     "content", r"\bAKIA[0-9A-Z]{16}\b", "CRITICAL",
     "AWS access-key ID"),
    ("github-pat",
     "content", r"\bgh[pousr]_[A-Za-z0-9_]{36,}", "CRITICAL",
     "GitHub personal-access-token"),
    ("bearer-token",
     "content", r"\bBearer\s+[A-Za-z0-9._~+/=-]{30,}", "HIGH",
     "Bearer-style token literal in source"),
    ("password-assign",
     "content",
     r"(?i)\bpassword\s*[=:]\s*[\"'](?![\s\"']|<.+>|\{\{|\$\{|env\.)[^\"'\s]{4,}",
     "HIGH",
     "plaintext password assignment"),
    ("apikey-assign",
     "content",
     r"(?i)\b(?:api[_\-]?key|access[_\-]?key|secret[_\-]?key)\s*[=:]\s*"
     r"[\"'](?![\s\"']|<.+>|\{\{|\$\{|env\.)[^\"'\s]{12,}",
     "HIGH",
     "plaintext API/access/secret-key assignment"),
    ("totp-secret-hint",
     "content",
     r"(?i)\b(?:totp|otp|2fa)[_\-]?secret\s*[=:]\s*[\"']?[A-Z2-7]{16,}",
     "HIGH",
     "TOTP / 2FA secret value"),
    ("recovery-code-block",
     "content",
     r"\b[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}\b(?:\s+\b[a-f0-9]{4}-){2}",
     "HIGH",
     "recovery-code block (4-4-4 hex set)"),
    ("ed25519-priv-near-context",
     "content",
     # 64 hex chars where the line/context mentions priv/secret. Tight to
     # avoid matching transient agent_id (also 64 hex) — only fires when
     # 'priv' / 'private' / 'secret' is on the same line within 40 chars.
     r"(?i)(?:priv|private|secret)\w*[\s=:][\"'\s]*[0-9a-f]{64}\b",
     "CRITICAL",
     "looks like a 64-hex private/secret key value"),
    # — Author / committer rules —
    ("author-local-host",
     "author", r"\.local$", "HIGH",
     "author/committer email carrying a hostname"),
    ("author-founder",
     "author", r"\bfounder\b", "HIGH",
     "author/committer name or email containing 'founder' "
     "(human-existence leak per rule)"),
    ("author-anp2-domain",
     "author", r"@anp2\.com$", "MEDIUM",
     "legacy ANP2 brand in author email (rule)"),
    # — rule: NEW identifier containing 'anp2' must NEVER be created —
    # The brand was migrated ANP2; only already-published IMMUTABLE PyPI
    # packages, Python module names, server paths, the legacy domain, and
    # the MCP URI scheme are grandfathered. Anything else carrying
    # 'anp2' is a new rule violation. Two-part rule:
    #   1. content scan with a custom allow-list scanner (run by name match
    #      in scan_text) — sees the surrounding context, not just regex.
    #   2. path scan: any tracked path component containing 'anp2'
    #      outside the grandfathered set fires HIGH.
    # Each rule's regex below is a sentinel — the actual decision is in
    # _scan_new_anp2_content() / _scan_new_anp2_path() in this file.
    ("new-anp2-identifier",
     "content", r"(?i)anp2", "HIGH",
     "rule: 'anp2' in NEW identifier — use 'anp2' / 'ANP2' / '@anp2/*'"),
    ("path-new-anp2",
     "path", r"(?i)anp2", "HIGH",
     "rule: tracked path contains 'anp2' outside grandfathered set"),
]

# Grandfather list — minimized to ZERO content patterns per operator
# directive 2026-05-24: the only place 'anp2' may appear in this repo
# is in the rule file (feedback-anp2-public-text-abc-rules.md) and inside
# leak_audit.py's own rule definitions (both via CONTENT_SCAN_EXCLUDE).
# Anything else — every PyPI package name, every Python module reference,
# every server path, every brand mention — is a violation.
ANP2_GRANDFATHER_CONTENT = re.compile(
    r"(?i)__never_match__"   # intentionally unmatchable
)
# JS / npm context: if 'anp2-...' appears after an npm install command
# or an import-from quoted-string, treat it as a NEW identifier even if
# the substring matches a PyPI grandfathered name. The npm namespace is
# independent of PyPI; the only allowed npm package name for our client
# is '@anp2/client'.
ANP2_NPM_CONTEXT = re.compile(
    r"(?:"
    r"(?:npm install|pnpm add|yarn add)\s+(?:[\w@/.,^~<>=-]+\s+)*[\w@/.,^~<>=-]*anp2"
    r"|from\s+[\"'][^\"']*anp2"
    r"|import\s+[\"'][^\"']*anp2"
    r"|require\(\s*[\"'][^\"']*anp2"
    r"|\"dependencies\"\s*:\s*\{[^}]*\"anp2"
    r")"
)
# Grandfather PATHS — also minimized to ZERO. No tracked path is allowed to
# contain 'anp2' anywhere. All Python modules, package dirs, systemd
# units, and PyPI artifacts have been renamed to anp2 form. Adding to this
# list = re-introducing the brand drift, never do it.
ANP2_GRANDFATHER_PATH = re.compile(
    r"__never_match__"   # intentionally unmatchable
)

# Paths whose contents are NOT scanned for content leaks. Path-rules still
# apply (we still check whether the file SHOULD be tracked).
#
# - tools/leak_audit.py:     contains the leak patterns as rule definitions
# - .gitignore:              its job is to LIST the internal paths so they
#                            stay untracked; matches there are intentional
# - prototypes/dashboard/index.html: contains a regex that strips the legacy
#                            "ANP2<RoleName>" prefix from display names
#                            (so the regex *includes* the bad words by design)
CONTENT_SCAN_EXCLUDE: set[str] = {
    # The rule-definition files themselves. They MUST contain 'anp2'
    # patterns as regex literals — that's how the rule works.
    "tools/leak_audit.py",
    # Peer rule-definition file (watches for the same rule patterns —
    # 'founder', '*.local', etc. — and references them in docstrings).
    "tools/account_health.py",
    # .gitignore lists internal-only path prefixes; matches there are
    # intentional and shouldn't fire any content rule.
    ".gitignore",
    # Dashboard renderer strips "ANP2<RoleName>" prefix from legacy
    # display names — the regex *includes* the bad word by design.
    "prototypes/dashboard/index.html",
}

# Per-rule false-positive exemptions: rule_name → set of file paths where
# the rule legitimately matches non-leak content (a publication name, a
# generic English term in a quoted question prompt, etc.). When extending
# this, leave a comment explaining why the match is acceptable.
RULE_FILE_EXCLUDE: dict[str, set[str]] = {
    # "Hacker News" appears as the name of a real RSS-feed publication
    # the news seed aggregates — not as a promotion target.
    "content-show-hn": {
        "prototypes/seed-agents/news/README.md",
        "prototypes/seed-agents/news/news.py",
        # events_sample.jsonl contains the news seed's published kind-1 events
        # which include the RSS-feed source list ("Hacker News frontpage, …").
        # This is content the seed agent itself broadcast to the network, not
        # promotion targeting by the operator.
        "prototypes/hf-dataset/events_sample.jsonl",
    },
    # - oracle: evaluation-question prompts use "founders" as a generic
    #   English word ("…obvious to newcomers and bizarre to founders?")
    # - heartbeat: keeps the legacy "founder" key stem as a fallback for
    #   backward-compat with un-migrated deployments
    "content-founder-word": {
        "prototypes/seed-agents/oracle/oracle.py",
        "prototypes/seed-agents/heartbeat/heartbeat.py",
    },
}

# Findings: (severity, rule_name, scope, detail)
findings: list[tuple[str, str, str, str]] = []


def record(severity: str, rule: str, scope: str, detail: str) -> None:
    findings.append((severity, rule, scope, detail))


def sh(*args: str) -> str:
    """Run a git command and return stdout (empty on failure)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# ── Scanners ──────────────────────────────────────────────────────────────

def scan_text(rule: tuple, text: str, scope: str) -> None:
    name, kind, pat, sev, _ = rule
    if kind != "content":
        return
    # Per-rule, per-file exemption: scope is the file path here (for
    # check_head_tracked) or a synthetic name like "staged-diff:<path>".
    exclude = RULE_FILE_EXCLUDE.get(name, set())
    if scope in exclude or scope.split(":", 1)[-1] in exclude:
        return
    # rule custom scanner: 'anp2' in content is only OK if the literal
    # match falls inside a grandfathered pattern OR outside an npm/JS context.
    if name == "new-anp2-identifier":
        _scan_new_anp2_content(text, scope, sev)
        return
    for m in re.finditer(pat, text):
        # Trim to a small surrounding excerpt (audit ≠ leak the leak again).
        i = max(0, m.start() - 20)
        j = min(len(text), m.end() + 20)
        excerpt = re.sub(r"\s+", " ", text[i:j])
        record(sev, name, scope, f"…{excerpt}…")
        return  # one finding per (rule, scope) is enough


ANP2_RULE_DIR_EXEMPT_PREFIXES: tuple[str, ...] = ()
# All directories now scanned. Migration complete 2026-05-24; nothing in
# the repo carries 'anp2' outside the rule definition files (which are
# in CONTENT_SCAN_EXCLUDE).


def _scan_new_anp2_content(text: str, scope: str, sev: str) -> None:
    """Fire on any 'anp2' substring that isn't covered by a grandfathered
    pattern AND isn't suppressed by an npm/JS context override.

    Strategy:
      0. If the scope's path is under a directory tied to an immutable
         identifier (anp2_client / anp2_relay / anp2_mcp_server
         / anp2_quickstart / anp2_mini / langchain-anp2 / seed-
         agent code / hf-space), skip the rule entirely — its 'anp2'
         mentions are pre-existing infrastructure.
      1. Build a set of byte-ranges where grandfathered patterns occur.
      2. Build a set of ranges where npm/JS-context anp2 mentions occur
         (these *override* the grandfather — npm namespace is new even if
         the substring 'anp2-client' matches PyPI immutable).
      3. For every literal 'anp2' match, check membership:
         - inside an npm-context range → FIRES (override)
         - else inside a grandfather range → SKIP
         - else → FIRES (rule violation)
    """
    # scope is either a path or "staged-diff:<path>" — extract the path
    bare_scope = scope.split(":", 1)[-1]
    if any(bare_scope.startswith(p) for p in ANP2_RULE_DIR_EXEMPT_PREFIXES):
        return
    grandfathered: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in ANP2_GRANDFATHER_CONTENT.finditer(text)
    ]
    npm_context: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in ANP2_NPM_CONTEXT.finditer(text)
    ]
    for m in re.finditer(r"(?i)anp2", text):
        pos = m.start()
        end = m.end()
        in_npm = any(s <= pos < e or s <= end <= e for s, e in npm_context)
        in_gf = any(s <= pos < e or s <= end <= e for s, e in grandfathered)
        if in_npm or not in_gf:
            i = max(0, pos - 25)
            j = min(len(text), end + 25)
            excerpt = re.sub(r"\s+", " ", text[i:j])
            record(sev, "new-anp2-identifier", scope, f"…{excerpt}…")
            return  # one finding per scope


def scan_path(rule: tuple, path: str) -> None:
    name, kind, pat, sev, _ = rule
    if kind != "path":
        return
    # rule custom path scanner: 'anp2' is OK only when the path matches
    # a grandfathered prefix anywhere along it.
    if name == "path-new-anp2":
        if re.search(r"(?i)anp2", path):
            if not ANP2_GRANDFATHER_PATH.search(path):
                record(sev, name, "tracked-path", path)
        return
    if re.search(pat, path):
        record(sev, name, "tracked-path", path)


def scan_author(rule: tuple, name_email: str) -> None:
    rname, kind, pat, sev, _ = rule
    if kind != "author":
        return
    if re.search(pat, name_email, re.IGNORECASE):
        record(sev, rname, "commit-author", name_email)


def check_head_tracked() -> None:
    """Walk every tracked file; check content (working-tree version) + path.

    Reads the current working-tree content (not HEAD's blob) so that
    uncommitted local fixes are reflected — what's safe NOW is what
    matters; HEAD is for the historical scan path.
    """
    files = sh("git", "ls-files").splitlines()
    for f in files:
        for r in RULES:
            scan_path(r, f)
        if f in CONTENT_SCAN_EXCLUDE:
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fp:
                blob = fp.read()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            continue
        if not blob:
            continue
        for r in RULES:
            scan_text(r, blob, f)


def check_staged() -> None:
    """Staged-only mode for pre-commit hooks.

    Scans only the ADDED ('+') lines of the staged diff (skipping the
    '+++' header). Removed ('-') lines are deletions — flagging them
    would prevent us from ever sanitizing a leak that already exists.

    Path rules are applied only to ADDED / MODIFIED / RENAME-TARGET paths.
    Deletions (`D`) and rename sources (`R`-source) are skipped because
    flagging a removal would block us from cleaning up a path that is
    *itself* the violation (e.g. removing prototypes/anp2-x/).
    """
    # name-status format: <STATUS>[NN]\t<path>[\t<new-path>]
    raw = sh("git", "diff", "--cached", "--name-status")
    paths_for_path_scan: list[str] = []
    paths_for_content_scan: list[str] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        status = parts[0][0]
        if status == "D":
            # Pure deletion — skip path & content scans (allows cleanup).
            continue
        if status == "R":
            # Rename: parts = ['R<NN>', <src>, <dst>]. Path-scan dst only;
            # content-scan dst (the rename target is the post-commit path).
            if len(parts) >= 3:
                paths_for_path_scan.append(parts[2])
                paths_for_content_scan.append(parts[2])
            continue
        # A, M, T, C, etc.
        if len(parts) >= 2:
            paths_for_path_scan.append(parts[1])
            paths_for_content_scan.append(parts[1])

    for f in paths_for_path_scan:
        for r in RULES:
            scan_path(r, f)

    for f in paths_for_content_scan:
        if f in CONTENT_SCAN_EXCLUDE:
            continue
        diff = sh("git", "diff", "--cached", "-U0", "--", f)
        added = "\n".join(
            ln[1:] for ln in diff.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")
        )
        if not added.strip():
            continue
        for r in RULES:
            scan_text(r, added, f"staged-diff:{f}")


def check_authors() -> None:
    seen: set[str] = set()
    for line in sh("git", "log", "--all", "--format=%an <%ae>%n%cn <%ce>").splitlines():
        line = line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        for r in RULES:
            scan_author(r, line)


def check_stash_reflog() -> None:
    for cmd, scope in (
        (("git", "stash", "list"), "stash"),
        (("git", "reflog", "--all"), "reflog"),
    ):
        text = sh(*cmd)
        for r in RULES:
            scan_text(r, text, scope)


def check_full_history() -> None:
    """Slow path: walk every (path, blob) reachable from any ref + every
    dangling blob, and apply each content rule. Respects CONTENT_SCAN_EXCLUDE
    and RULE_FILE_EXCLUDE so that rule-definition files and gitignore lists
    do not generate self-referential false positives.

    Reports the first hit per (rule, path) — that's enough to FAIL the run;
    the operator can then re-run with a focused tool to enumerate all hits
    for that rule + path.
    """
    # 1. Build (path, blob) set from every reachable commit.
    seen_path_blob: dict[str, set[str]] = {}
    commits = sh("git", "rev-list", "--all").split()
    for c in commits:
        ls = sh("git", "ls-tree", "-r", c)
        for line in ls.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[1] == "blob":
                _, _, sha, path = parts
                seen_path_blob.setdefault(path, set()).add(sha)
    # 2. Add dangling blobs (no known path — scope them as "dangling").
    fsck = subprocess.run(
        ["git", "fsck", "--unreachable", "--no-progress"],
        capture_output=True, text=True, timeout=60)
    dangling: set[str] = set()
    for ln in (fsck.stdout + fsck.stderr).splitlines():
        m = re.search(r"(?:unreachable|dangling)\s+\w+\s+([0-9a-f]{40})", ln)
        if m:
            dangling.add(m.group(1))
    # Filter to blob type
    if dangling:
        bc = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input="\n".join(dangling).encode(),
            capture_output=True, timeout=60)
        dangling = {ln.split()[0] for ln in bc.stdout.decode().splitlines()
                    if ln.endswith("blob")}
        if dangling:
            seen_path_blob["(dangling)"] = dangling

    # 3a. Walk historical paths and apply path rules. Catches paths that
    # ever existed in any commit (e.g., a filename with JP characters
    # added then deleted — the path is still in history via that commit's
    # tree). Without this loop, path rules only run on current-HEAD
    # tracked files — historical leaks (file deleted from HEAD but still
    # in old commits) would be invisible to --full.
    fired: set[tuple[str, str]] = set()  # (rule_name, path) for dedupe
    for path in seen_path_blob.keys():
        for r in RULES:
            name, kind, pat, sev, _ = r
            if kind != "path":
                continue
            if (name, path) in fired:
                continue
            if re.search(pat, path):
                fired.add((name, path))
                record(sev, name, f"history-path:{path}",
                       "path matched in historical tree (filter-repo to scrub)")

    # 3b. Walk (path, blob) and apply content rules to blob bodies.
    for path, shas in seen_path_blob.items():
        if path in CONTENT_SCAN_EXCLUDE:
            continue
        for sha in shas:
            blob = subprocess.run(["git", "cat-file", "-p", sha],
                                  capture_output=True, timeout=20).stdout
            try:
                text = blob.decode("utf-8")
            except UnicodeDecodeError:
                text = blob.decode("utf-8", "replace")
            for r in RULES:
                name, kind, pat, sev, _ = r
                if kind != "content":
                    continue
                if (name, path) in fired:
                    continue
                if path in RULE_FILE_EXCLUDE.get(name, set()):
                    continue
                m = re.search(pat, text)
                if m:
                    fired.add((name, path))
                    record(sev, name, f"history:{path}",
                           f"first hit at blob {sha[:10]}")


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--staged", action="store_true",
                    help="pre-commit mode — only check the staged diff")
    ap.add_argument("--full", action="store_true",
                    help="also scan every blob in full git history (slow)")
    args = ap.parse_args()

    if args.staged:
        check_staged()
        check_authors()  # author of the upcoming commit will use current config
    else:
        check_head_tracked()
        check_authors()
        check_stash_reflog()
        if args.full:
            check_full_history()

    stamp = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    mode = "staged" if args.staged else ("full" if args.full else "default")
    print(f"ANP2 leak audit — mode={mode} — {stamp}")
    print("-" * 68)

    # Summary by rule (PASS for unfound rules).
    fired = {f[1] for f in findings}
    for r in RULES:
        name = r[0]
        if name in fired:
            for sev, rname, scope, detail in findings:
                if rname == name:
                    print(f"  [FAIL {sev:<8}] {name} @ {scope}: {detail}")
        else:
            print(f"  [PASS         ] {name}")

    n_fail = len(findings)
    print("-" * 68)
    print(f"{len(RULES)} rules checked, {len(fired)} fired, {n_fail} finding(s)")
    if n_fail:
        print("\nFAIL — the repo is NOT in a publish-safe state.")
        print("Either fix the finding above, or update RULES if a pattern is "
              "now a known false-positive.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
