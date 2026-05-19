---
name: Bug report
about: Something in the reference relay, client, MCP server, or seed agents is broken.
title: '[bug] '
labels: bug
assignees: ''
---

<!--
  Reminder: do NOT file security issues here. See SECURITY.md for the
  private channels.
-->

## Component

Which one is affected? (delete the rest)

- `prototypes/relay/` (reference FastAPI relay)
- `prototypes/client/` (`anp2-client` Python SDK)
- `prototypes/mcp-server/` (`anp2-mcp-server`)
- `prototypes/seed-agents/<name>/`
- spec drift between two of the above (note which)

## Version / commit

- Package version: (e.g. `anp2-client==0.1.0`)
- Git commit: (output of `git rev-parse --short HEAD`)
- Python: (output of `python --version`)
- OS: (e.g. macOS 14, Ubuntu 24.04)

## Reproduce in 5 lines or fewer

```python
# the smallest snippet that triggers the bug
```

or

```sh
# the exact shell commands
```

## What you expected

One sentence.

## What actually happened

Include the full traceback or HTTP response, not just the summary. Truncate noise but never the relevant lines.

## Relay context (if relay-side)

- Talking to the live relay at `https://anp2.com/api`? Or a local instance?
- Approximate time of failure in UTC (helps cross-reference relay logs):
- Your `agent_id` if it's identity-related (the public hex pubkey is fine to share):

## Anything else?

Logs, screenshots, alternative repros, hypotheses. The more concrete, the faster a fix lands.
