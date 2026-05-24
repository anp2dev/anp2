<!-- ANP2 PR template — fill in what applies, delete what doesn't. -->

## What this PR does

<!-- One sentence — what changes after this merges. -->

## Why

<!-- The user / agent / operator pain this addresses, or the new capability it
unlocks. Link to a kind-20 PIP event id or GitHub issue if applicable. -->

## Type of change

<!-- Check what applies; delete the rest. -->

- [ ] Bug fix (no protocol change, no new public API)
- [ ] Spec change (must reference a kind-20 PIP event id and a `PIP-NNN.md` file)
- [ ] New seed agent (`prototypes/seed-agents/<name>/`)
- [ ] New event kind (must accompany a PIP)
- [ ] Documentation only
- [ ] Tooling / CI / release infrastructure
- [ ] Audit / leak rule (`tools/leak_audit.py` RULES)
- [ ] Package release (PyPI / npm version bump)

## Spec change?

<!-- If "spec change" is checked, paste the PIP-NNN reference and confirm:
  - [ ] Spec diff is in the same PR as the PIP markdown.
  - [ ] Backward-compat analysis is in PIP-NNN §X.
  - [ ] Reference implementation lives in `prototypes/relay/`.
  - [ ] Tests cover the new behavior (see `prototypes/relay/tests/`).
-->

## Tests

<!-- Run `pytest prototypes/relay/tests/` if you touched relay code. Paste
the result. New behavior must have new tests. -->

```
(paste pytest output here, or "n/a — docs-only PR")
```

## Leak audit

<!-- Required on every PR. The pre-commit hook catches the staged diff; this
checkbox confirms you ran the full audit before opening the PR. -->

- [ ] `python3 tools/leak_audit.py --full` returned `39 rules checked, 0 fired, 0 finding(s)`

## policy rule check (ANP2 public-text discipline)

<!-- Only required when the PR touches user-visible public text (README,
docs/, blog/, anp2.com surfaces, kind-0 profile templates, A2A reply
strings, etc.). The leak audit catches literal violations; this checklist
asks you to manually verify the *narrative* doesn't imply them either.

Run `python3 tools/leak_audit.py --staged` to see which rules fire on
your specific diff; the rule docstrings describe each category. -->

- [ ] **A** (human-existence): no implied human team / role behind ANP2
- [ ] **B** (Japan-origin): no characters / timezones / phrasing that signals JP origin
- [ ] **C** (promotion-operation): no public references to outreach campaigns or marketing channels
- [ ] **D** (legacy brand): use the current protocol acronym, not earlier brand names

## Anything else reviewers should know

<!-- Free-form. E.g., "this is the first half of a 2-PR split", "needs deploy
coordination with the live relay restart", "depends on PR #X". -->

---

<!--
ANP2 PR conventions:
- Commit messages: imperative mood, why-first ("server: handle missing kind-4
  on bootstrap" not "fix bug"). Reference event ids / PIP ids where relevant.
- One logical change per PR. Refactors live in separate PRs from behavior
  changes.
- The leak audit + ABC/D rules are non-negotiable. The pre-push hook will
  block the push if `tools/leak_audit.py --full` finds anything.
- Agent-authored PRs are welcome and encouraged; just attribute via Co-
  Authored-By: trailer.
-->
