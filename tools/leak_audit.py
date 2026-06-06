#!/usr/bin/env python3
"""leak_audit.py — repository publish-safety audit.

A runnable audit that a pre-commit hook, pre-push hook, or session-start
routine can fire to confirm the working tree (and, with --full, the whole
git history) is safe to publish.

Checks:
  - content: secret/credential literals in tracked files, staged diffs, and
             optionally every blob in full history
  - path:    private-only paths that must never be tracked
  - author:  commit author / committer fields carrying host-bearing emails
  - stash + reflog: residual references outside the commit DAG

This file ships GENERIC, universal detectors only (keys, tokens, passwords,
private paths). Any project-specific content rules live in an OPTIONAL local
config (ANP2_CONTENT_DENYLIST, default internal/env/content-denylist.json) that
is loaded at runtime if present — so this published script never inlines the
specific strings it scans for, and (because it no longer excludes itself from
the scan) re-introducing such a string into ANY tracked file fails the audit.

Modes:
  default       — HEAD tracked files + authors + stash + reflog (fast)
  --staged      — only the staged diff (pre-commit variant; very fast)
  --full        — everything above + scan every blob in full history (slow)

Exit 0 = clean. Exit 1 = at least one FAIL. No third-party deps.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

# ── Generic rules (universal; safe to publish) ──────────────────────────────
# Each rule: (name, kind, pattern, severity, description). kind ∈ {content,path,author}.
RULES: list[tuple[str, str, str, str, str]] = [
    # — Host / path hygiene —
    ("dotlocal-host",
     "content", r"\b[a-z][a-z0-9-]*\.local\b", "MEDIUM",
     "a *.local mDNS hostname in tracked content"),
    ("private-doc-ref-in-public",
     "content", r"(?:^|\s|`|\(|/)(internal/(?:memory|research|env)/|internal/OPERATOR_|OPERATOR_(?:TODO|RUNBOOK|NOTES)\.md)\b",
     "HIGH",
     "tracked file references a private-only path (broken pointer + leak)"),
    # — Private-only paths must never be tracked —
    ("internal-tree",
     "path", r"^internal/(?!env/)", "HIGH",
     "internal/ holds private material; must never be tracked"),
    ("internal-env",
     "path", r"^internal/env/", "CRITICAL",
     "internal/env/ holds private keys + config; must never be tracked"),
    ("legacy-memory-at-root",
     "path", r"^memory/", "HIGH",
     "memory/ belongs under internal/; do not recreate at root"),
    ("legacy-research-at-root",
     "path", r"^docs/research/", "HIGH",
     "research notes belong under internal/; do not recreate"),
    ("legacy-operator-at-root",
     "path", r"^OPERATOR_(TODO|RUNBOOK|NOTES)\.md$", "HIGH",
     "OPERATOR_*.md belongs under internal/; do not recreate at root"),
    ("legacy-env-at-root",
     "path", r"^env/", "CRITICAL",
     "env/ belongs under internal/env/; do not recreate at root"),
    ("filename-ai-gen-trace",
     "path", r"(?i)\bChatGPT[\s_-]Image|\bmidjourney\b|\bstable.diffusion\b",
     "MEDIUM",
     "tracked filename advertises a generation-tool origin"),
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
    ("colony-key",
     "content", r"\bcol_[A-Za-z0-9]{16,}", "CRITICAL",
     "service agent API key literal (belongs in a private env file only)"),
    ("openrouter-openai-key",
     "content", r"\bsk-(?:or-v1-|proj-|svcacct-)?[A-Za-z0-9][A-Za-z0-9_-]{19,}", "CRITICAL",
     "OpenAI/OpenRouter API key literal incl. service-account keys (private env only)"),
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
     r"(?i)(?:priv|private|secret)\w*[\s=:][\"'\s]*[0-9a-f]{64}\b",
     "CRITICAL",
     "looks like a 64-hex private/secret key value"),
    # — Author / committer —
    ("author-local-host",
     "author", r"\.local$", "HIGH",
     "author/committer email carrying a hostname"),
]

# Files whose CONTENT is not scanned. Path-rules still apply.
#   - .gitignore lists private path prefixes; matches there are intentional.
CONTENT_SCAN_EXCLUDE: set[str] = {
    ".gitignore",
}

# Per-rule, per-file false-positive exemptions. rule_name -> set of paths.
RULE_FILE_EXCLUDE: dict[str, set[str]] = {
    # Operator-side utility scripts intentionally reference private-only paths
    # (key path, host file, credential lookup). Functional defaults, not leaks.
    "private-doc-ref-in-public": {
        "tools/anp2_chrome_launch.sh",
        "tools/anp2_chrome_launch_cdp.sh",
        "tools/crawler_log_audit.py",
        "tools/socks_scope_check.sh",
        "tools/sync_landing.sh",
        "tools/totp.sh",
        "tools/mail_dev_check.sh",
        "tools/account_health.py",
        "tools/flag_risk_check.sh",
        "tools/gh_safe.sh",
        "tools/git_safe.sh",
        "tools/publish_safe.sh",
        "tools/scrape_safe.sh",
        "tools/defense_integrity.sh",
        "tools/push.sh",
        "tools/commit.sh",
        "tools/anp2_image_gen.py",
        "tools/anp2_openai_image.py",
        "tools/followup_check.py",
        "hooks/pre-commit",
        "hooks/pre-push",
    },
}

# Rules exempt from history-blob scanning (soft/generic heuristics that match
# their own rule definitions in historical blobs). Extended from the denylist.
HISTORY_EXEMPT_RULES: set[str] = {"dotlocal-host"}


def _load_denylist() -> None:
    """Append the OPTIONAL local content rules (project-specific patterns kept
    out of this published source) to RULES / RULE_FILE_EXCLUDE / HISTORY_EXEMPT.

    Absent file (e.g. a fresh public clone or CI runner) → generic rules only;
    that is intentional. The hard enforcement runs locally where the file is.
    """
    path = os.environ.get("ANP2_CONTENT_DENYLIST")
    if not path:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "..", "internal", "env", "content-denylist.json")
    try:
        with open(path, encoding="utf-8") as fp:
            cfg = json.load(fp)
    except (FileNotFoundError, ValueError, OSError):
        return
    for r in cfg.get("leak_audit_rules", []):
        try:
            RULES.append((r["id"], r["kind"], r["regex"], r["severity"], r.get("note", "")))
            if r.get("history_exempt"):
                HISTORY_EXEMPT_RULES.add(r["id"])
        except (KeyError, TypeError):
            continue
    for rule_id, paths in (cfg.get("rule_file_exclude") or {}).items():
        RULE_FILE_EXCLUDE.setdefault(rule_id, set()).update(paths)


_load_denylist()

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
    exclude = RULE_FILE_EXCLUDE.get(name, set())
    if scope in exclude or scope.split(":", 1)[-1] in exclude:
        return
    m = re.search(pat, text)
    if m:
        i = max(0, m.start() - 20)
        j = min(len(text), m.end() + 20)
        excerpt = re.sub(r"\s+", " ", text[i:j])
        record(sev, name, scope, f"…{excerpt}…")


def scan_path(rule: tuple, path: str) -> None:
    name, kind, pat, sev, _ = rule
    if kind != "path":
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
    """Walk every tracked file; check working-tree content + path."""
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
    """Staged-only mode for pre-commit hooks (added lines + added/renamed paths)."""
    raw = sh("git", "diff", "--cached", "--name-status")
    paths_for_path_scan: list[str] = []
    paths_for_content_scan: list[str] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        status = parts[0][0]
        if status == "D":
            continue
        if status == "R":
            if len(parts) >= 3:
                paths_for_path_scan.append(parts[2])
                paths_for_content_scan.append(parts[2])
            continue
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
    """Walk every (path, blob) reachable from any ref + every dangling blob,
    and apply each content rule. No file is self-excluded by path (this script
    no longer inlines the patterns), so a re-introduced literal anywhere in
    history is caught."""
    seen_path_blob: dict[str, set[str]] = {}
    commits = sh("git", "rev-list", "--all").split()
    for c in commits:
        ls = sh("git", "ls-tree", "-r", c)
        for line in ls.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[1] == "blob":
                _, _, sha, path = parts
                seen_path_blob.setdefault(path, set()).add(sha)
    fsck = subprocess.run(
        ["git", "fsck", "--unreachable", "--no-progress"],
        capture_output=True, text=True, timeout=60)
    dangling: set[str] = set()
    for ln in (fsck.stdout + fsck.stderr).splitlines():
        m = re.search(r"(?:unreachable|dangling)\s+\w+\s+([0-9a-f]{40})", ln)
        if m:
            dangling.add(m.group(1))
    if dangling:
        bc = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input="\n".join(dangling).encode(),
            capture_output=True, timeout=60)
        dangling = {ln.split()[0] for ln in bc.stdout.decode().splitlines()
                    if ln.endswith("blob")}
        if dangling:
            seen_path_blob["(dangling)"] = dangling

    fired: set[tuple[str, str]] = set()
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
                       "path matched in historical tree (rewrite to scrub)")

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
                if name in HISTORY_EXEMPT_RULES:
                    continue
                if (name, path) in fired:
                    continue
                if path in RULE_FILE_EXCLUDE.get(name, set()):
                    continue
                if re.search(pat, text):
                    fired.add((name, path))
                    record(sev, name, f"history:{path}",
                           f"first hit at blob {sha[:10]}")


def check_commit_messages(full: bool) -> None:
    """Scan commit MESSAGES (subject + body) against content rules — a surface
    the file/path/author scanners never covered, so policy-violating prose in
    commit messages (JP text, promotion/campaign naming) shipped invisibly.

    default mode → only HEAD's own message (catches the commit just authored).
    --full mode  → every commit message in history (flags legacy contamination
                   that a message-rewrite must scrub).

    HISTORY_EXEMPT_RULES are skipped here too, so the operator-accepted residual
    classes (dead-identity names, the discipline labels) are not re-flagged;
    jp-text and the structural promo rules (non-exempt) still fire."""
    fmt = "%H%x1f%B%x1e"
    log = sh("git", "log", "--all", "--format=" + fmt) if full \
        else sh("git", "log", "-1", "--format=" + fmt)
    for entry in log.split("\x1e"):
        entry = entry.strip()
        if not entry or "\x1f" not in entry:
            continue
        sha, msg = entry.split("\x1f", 1)
        for r in RULES:
            name, kind, _pat, _sev, _ = r
            if kind != "content" or name in HISTORY_EXEMPT_RULES:
                continue
            scan_text(r, msg, f"commit-msg:{sha[:10]}")


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
        check_authors()
    else:
        check_head_tracked()
        check_authors()
        check_stash_reflog()
        check_commit_messages(full=args.full)
        if args.full:
            check_full_history()

    stamp = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    mode = "staged" if args.staged else ("full" if args.full else "default")
    print(f"publish-safety audit — mode={mode} — {stamp}")
    print("-" * 68)

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
        print("Fix the finding above, or update the rule config if it is a "
              "known false-positive.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
