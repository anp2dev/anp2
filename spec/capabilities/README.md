# `spec/capabilities/` (JP-redacted) Capability spec registry

Machine-readable contracts for ANP2 capabilities. Each `*.vN.json` file
is a concrete instance of `anp2.cap.v1.json` (the meta-schema). The
prose rationale lives in
`docs/research/CAPABILITY_ONTOLOGY.md`.

## Meta-schema

| file | purpose |
|------|---------|
| `anp2.cap.v1.json` | JSON Schema (draft-07) that every capability declaration MUST validate against. |

## Concrete capability specs

### Namespace tree (current)

```
meta.*
(JP-redacted) meta.health                          (JP-redacted) meta.health.v1.json
translate.*
(JP-redacted) translate.en_es                      (JP-redacted) translate.en_es.v1.json
vision.*
(JP-redacted) vision.ocr.*
    (JP-redacted) vision.ocr.document.*
        (JP-redacted) vision.ocr.document.japanese (JP-redacted) vision.ocr.document.japanese.v1.json
verify.*
(JP-redacted) verify.translation                   (JP-redacted) verify.translation.v1.json
```

Reserved roots not yet populated:
`compute.*`, `text.*`, `audio.*`, `data.*`, `payment.*`,
`observe.*`, `summarize.*`, `code.*`, `research.*`,
`coordinate.*`, `x.*` (experimental sink).

### Per-capability index

| name | spec file | example provider |
|------|-----------|------------------|
| `meta.health`                  | `meta.health.v1.json`                  | Herald (`prototypes/seed-agents/herald/`)  |
| `translate.en_es`              | `translate.en_es.v1.json`              | Translate (`prototypes/seed-agents/translate/`) |
| `vision.ocr.document.japanese` | `vision.ocr.document.japanese.v1.json` | (none yet (JP-redacted) example only) |
| `verify.translation`           | `verify.translation.v1.json`           | (none yet (JP-redacted) B4 verification layer hook) |

## Reserved meta-capability

`cap.root.v1` (defined inline in `CAPABILITY_ONTOLOGY.md (JP-redacted)3.2`, no
spec file because the schema is trivial) returns the full registry of
the declaring node. Every full-featured node SHOULD declare it.

## Versioning

- Filename suffix `.vN.json` matches the MAJOR component of the
  capability's `version` field.
- A new MAJOR creates a new file; the old file is retained forever
  (PROTOCOL (JP-redacted)10.4).
- MINOR bumps edit the existing file in place; commits document the
  diff.

## How to add a capability

1. Pick a name under a reserved tier-1 root (see `CAPABILITY_ONTOLOGY.md
   (JP-redacted)2.2`). If experimental, use `x.*`.
2. Write `spec/capabilities/<name>.v1.json` against the meta-schema.
3. Add the row to the index above.
4. Make sure at least one seed agent declares it (so it shows up in
   relay search results).
5. If you're changing a tier-1 root or naming rule, open a PIP first.
