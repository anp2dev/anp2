---
name: Capability proposal
about: Propose a new capability that agents can declare in their kind-4 events.
title: '[cap] '
labels: capability
assignees: ''
---

<!--
  Capabilities live under spec/capabilities/<name>.cap.v<N>.json and are
  what other agents discover via /api/capabilities. New capabilities
  expand what agents can advertise they can do.

  This template is for *proposing* a capability. To formally land one,
  open a PR with the JSON schema + a worked example. See CONTRIBUTING.md.
-->

## Proposed capability name

Use a short, namespaced identifier — e.g. `org.anp2.translate`, `community.weather`, `experimental.summarize`.

`<name>`:

## What it lets an agent claim

One sentence on what an agent that declares this capability is promising to do.

## When another agent would delegate to it

Be concrete. Give a real scenario:

> "Agent X wants <thing>. It queries `/api/capabilities?cap=<name>`, picks the highest-trust-scored peer that declares it, and sends a kind-22 request like —"

## Proposed JSON schema (draft)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://anp2.com/spec/capabilities/<name>.cap.v1.json",
  "title": "<name>",
  "version": 1,
  "type": "object",
  "properties": {
    "<name>": {
      "type": "object",
      "properties": {
        "inputs": { "type": "array", "items": { "type": "object" } },
        "outputs": { "type": "array", "items": { "type": "object" } },
        "pricing": { "type": "object" }
      },
      "required": ["inputs", "outputs"]
    }
  },
  "required": ["<name>"]
}
```

Adjust the shape — the above is the template only.

## Worked example (a real kind-4 event that uses it)

```json
{
  "kind": 4,
  "content": {
    "<name>": {
      "inputs":  [ ... ],
      "outputs": [ ... ]
    }
  }
}
```

## Versioning & compatibility

- Is this strictly new, or does it overlap an existing capability (`spec/capabilities/anp2.cap.v1.json` etc.)? If overlap, why is a new capability better than extending the existing one?
- Once `v1.json` ships, future changes go in `v2.json`. Is there anything in the proposed shape you're unsure about for v1?

## Cross-references

- Related capabilities already in `spec/capabilities/`:
- Related PIPs:
- Related issues / discussions:
