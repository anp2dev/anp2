# CI-NNN — [Short title naming the contributor and the design choice]

> **Template — copy this file, do not edit in place.**
>
> Filename: `CI-NNN.md` where NNN is the next zero-padded integer.
> Find the next available with: `ls docs/CI/CI-*.md | sort | tail -1`.
>
> Title rule: include the contributor's handle and the specific
> design choice they targeted. Example: `CI-007 — @someuser on kind 53
> verifier eligibility`.

## Origin

- **Source permalink**: <link to the original comment, post, or thread>
- **Contributor**: <contributor handle>
- **Source**: <platform or forum>
- **Comment date**: 2026-MM-DD
- **Captured by**: [name + date]

## Verbatim quote (—200 words)

> Quote the contributor's actual words here. Do not paraphrase. If the
> quote is longer than 200 words, link to the comment and quote the
> single most-load-bearing paragraph.

## What it targets

The specific design choice the critique addresses. Cite the spec section
/ PIP / doc + line/heading. Example:

- **Spec section**: `PROTOCOL.md §18.3` (kind 53 schema)
- **PIP**: PIP-001 §4 (verifier eligibility)
- **Doc**: `docs/research/AUTONOMOUS_TASK_ECONOMY.md`

## Engagement Lead's initial reaction

One of:
- `probably-adopt` — critique is correct, change is small, no major
  side-effects expected.
- `probably-reject` — critique misunderstands the design or proposes a
  trade we've explicitly considered and decided against.
- `needs-design-review` — critique is substantive and the answer
  isn't obvious; Protocol Designer should weigh in.

## PIP candidate?

- `pip-candidate: true|false`
- If `true`, what would the PIP be about (one sentence)?
- If `false`, why is this a doc-level / spec-clarification change
  instead of a protocol commitment?

## Status

One of (update as it changes; preserve history below):

- `open` — filed, awaiting Protocol Designer review.
- `under-review` — Protocol Designer is actively assessing.
- `adopted` — change merged; link the PR / commit.
- `adopted-with-modification` — accepted in a modified form; link the
  PR and explain the modification.
- `rejected` — closed without change; the `## Resolution` section
  records the reason.
- `promoted-to-pip` — became PIP-NNN-draft.md; link the PIP.

## Status history

| Date | Status | Notes |
|------|--------|-------|
| YYYY-MM-DD | open | Filed by [name]. |

## Resolution

Filled in when status changes to `adopted` / `adopted-with-modification`
/ `rejected` / `promoted-to-pip`. Must include:

1. The specific decision made.
2. The rationale (—200 words).
3. The PR / commit / PIP link.
4. The date the Engagement Lead replied to the source thread with the
   resolution (no contributor is left hanging).

## Reply-to-thread record

- **Replied at**: YYYY-MM-DD HH:MM UTC
- **Reply permalink**: <link to the posted reply>
- **Reply content (quote)**:
  > Verbatim text of the reply we posted back to the contributor,
  > citing them by name and reporting the outcome.

## Cross-references

- Related CI tickets: CI-NNN, CI-MMM (if any)
- Related PIP: PIP-NNN (if any)
- Related spec section: PROTOCOL.md —X.Y
- Related doc: docs/research/...md
