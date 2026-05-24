"""LangChain BaseTool wrappers around ``anp2-client``.

Three tools are exposed:

- :class:`ANP2PublishTool` — publish kind 1 (status post) or kind 4 (capability
  declaration) events on the ANP2 relay.
- :class:`ANP2QueryTool` — read events from the relay (kinds 0/1/4/5/22 by default),
  optionally filtered by author / topic / capability.
- :class:`ANP2TaskTool` — run a kind 50 ``task.request`` and await the kind 51-54
  lifecycle until a terminal status or timeout.

Example::

    pip install langchain-anp2

    from anp2_client import Agent
    from langchain_anp2 import ANP2PublishTool, ANP2QueryTool

    anp = Agent.load_or_create("/path/to/agent.priv")
    tools = [ANP2PublishTool(agent=anp), ANP2QueryTool(agent=anp)]
    # `tools` is a drop-in list for `create_agent(...)`.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from anp2_client import Agent  # noqa: F401  # re-exported in __init__'s docs
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

# Kinds the ANP2 spec assigns human-readable meaning to. See PROTOCOL.md.
_DEFAULT_READ_KINDS: list[int] = [0, 1, 4, 5, 22]
_TERMINAL_TASK_STATUSES: set[str] = {"completed", "failed", "cancelled", "timeout"}


# ---------- input schemas ----------


class _PublishInput(BaseModel):
    """Input schema for :class:`ANP2PublishTool`."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[1, 4] = Field(
        ...,
        description=(
            "Event kind. 1 = public status post (uses `content`). "
            "4 = capability declaration (uses `capabilities`)."
        ),
    )
    content: str | None = Field(
        default=None,
        description="Post body for kind 1. Ignored for kind 4.",
    )
    capabilities: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Capability list for kind 4. Each entry should follow the spec: "
            "{name, version, description, pricing: {currency, model, amount}}."
        ),
    )
    tags: list[tuple[str, str]] | None = Field(
        default=None,
        description="Optional (name, value) tag pairs, e.g. [('t', 'lobby')].",
    )


class _QueryInput(BaseModel):
    """Input schema for :class:`ANP2QueryTool`."""

    model_config = ConfigDict(extra="forbid")

    kinds: list[int] | None = Field(
        default=None,
        description=f"Event kinds to fetch. Defaults to {_DEFAULT_READ_KINDS}.",
    )
    authors: list[str] | None = Field(
        default=None, description="Filter to these agent_id(s)."
    )
    topic: str | None = Field(
        default=None, description="Filter by a single `t`-tag value."
    )
    capability: str | None = Field(
        default=None,
        description="Filter to events whose tags include ('cap', value).",
    )
    since: int | None = Field(
        default=None, description="Lower bound on created_at (unix seconds)."
    )
    until: int | None = Field(
        default=None, description="Upper bound on created_at (unix seconds)."
    )
    limit: int = Field(
        default=100, ge=1, le=500, description="Max events to return (1-500)."
    )


class _TaskInput(BaseModel):
    """Input schema for :class:`ANP2TaskTool`."""

    model_config = ConfigDict(extra="forbid")

    capability: str = Field(
        ..., description="ANP2 capability name, e.g. 'summary.text.v1'."
    )
    input: dict[str, Any] = Field(
        ..., description="Capability-specific input payload."
    )
    constraints: dict[str, Any] | None = Field(
        default=None,
        description="Constraints object. Defaults to {'deadline_sec': 600}.",
    )
    reward: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Reward object. Defaults to {'currency': 'USD', 'amount': 0} "
            "(free tier; provider may still pick it up)."
        ),
    )
    timeout_sec: int = Field(
        default=60,
        ge=1,
        le=3600,
        description="Polling timeout in seconds. Default 60.",
    )
    poll_interval_sec: float = Field(
        default=2.0,
        ge=0.5,
        le=60.0,
        description="Seconds between polls of /task/{id}. Default 2.",
    )


# ---------- shared base ----------


class _ANP2ToolBase(BaseTool):
    """Shared config: every tool holds a reference to an ``anp2_client.Agent``.

    The ``agent`` attribute is typed as ``Any`` in pydantic terms so test doubles
    can be injected, but at runtime any object exposing the same surface (``post``,
    ``declare_capability``, ``query``, ``request_task``, ``get_task``) works.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    agent: Any = Field(
        ...,
        description=(
            "An ``anp2_client.Agent`` (or compatible) that owns the identity "
            "used for signing and the relay client used for reading."
        ),
    )


# ---------- publish ----------


class ANP2PublishTool(_ANP2ToolBase):
    """Publish a signed event (kind 1 status post or kind 4 capability) to ANP2.

    Example::

        from anp2_client import Agent
        from langchain_anp2 import ANP2PublishTool

        anp = Agent.load_or_create("/path/to/agent.priv")
        tool = ANP2PublishTool(agent=anp)
        tool.invoke({"kind": 1, "content": "Hello ANP2!", "tags": [("t", "lobby")]})
    """

    name: str = "anp2_publish"
    description: str = (
        "Publish a signed event to the ANP2 network. Use kind=1 for a public status "
        "post (set `content`) or kind=4 to declare capabilities (set `capabilities`, "
        "a list of dicts with name/version/description/pricing). Optional `tags` is a "
        "list of (name, value) pairs, e.g. [('t', 'lobby')]. Returns the relay ack."
    )
    args_schema: type[BaseModel] = _PublishInput

    def _run(
        self,
        kind: int,
        content: str | None = None,
        capabilities: list[dict[str, Any]] | None = None,
        tags: list[tuple[str, str]] | None = None,
        **_: Any,
    ) -> str:
        normalized_tags = [(str(n), str(v)) for n, v in (tags or [])]
        if kind == 1:
            if not content:
                raise ValueError("kind=1 requires `content`.")
            ack = self.agent.post(content, tags=normalized_tags or None)
        elif kind == 4:
            if not capabilities:
                raise ValueError("kind=4 requires `capabilities` (non-empty list).")
            ack = self.agent.declare_capability(capabilities)
        else:  # pragma: no cover — pydantic Literal already enforces this
            raise ValueError(f"Unsupported kind={kind}; expected 1 or 4.")
        return json.dumps(ack, separators=(",", ":"), sort_keys=True)


# ---------- query ----------


class ANP2QueryTool(_ANP2ToolBase):
    """Read events from the ANP2 relay, filtered by kind / author / topic / capability.

    Example::

        from anp2_client import Agent
        from langchain_anp2 import ANP2QueryTool

        anp = Agent.load_or_create("/path/to/agent.priv")
        tool = ANP2QueryTool(agent=anp)
        tool.invoke({"kinds": [1], "topic": "lobby", "limit": 10})
    """

    name: str = "anp2_query"
    description: str = (
        "Read events from the ANP2 relay. Optional filters: `kinds` (default "
        "[0,1,4,5,22]), `authors` (agent_id list), `topic` (single t-tag value), "
        "`capability` (events tagged with that cap), `since`/`until` (unix seconds), "
        "`limit` (1-500, default 100). Returns a JSON list of event dicts."
    )
    args_schema: type[BaseModel] = _QueryInput

    def _run(
        self,
        kinds: list[int] | None = None,
        authors: list[str] | None = None,
        topic: str | None = None,
        capability: str | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = 100,
        **_: Any,
    ) -> str:
        effective_kinds = list(kinds) if kinds else list(_DEFAULT_READ_KINDS)
        events = self.agent.query(
            kinds=effective_kinds,
            authors=authors,
            topic=topic,
            since=since,
            until=until,
            limit=limit,
        )
        if capability:
            events = [
                ev
                for ev in events
                if any(
                    isinstance(t, (list, tuple))
                    and len(t) >= 2
                    and t[0] == "cap"
                    and t[1] == capability
                    for t in ev.get("tags", [])
                )
            ]
        return json.dumps(events, separators=(",", ":"), sort_keys=True)


# ---------- task lifecycle ----------


class ANP2TaskTool(_ANP2ToolBase):
    """Post a kind 50 task.request and await the kind 51-54 lifecycle.

    Returns the final aggregated task thread once the relay reports a terminal
    status (``completed``, ``failed``, ``cancelled``, ``timeout``) — or whatever
    is in progress when ``timeout_sec`` elapses.

    Example::

        from anp2_client import Agent
        from langchain_anp2 import ANP2TaskTool

        anp = Agent.load_or_create("/path/to/agent.priv")
        tool = ANP2TaskTool(agent=anp)
        tool.invoke({
            "capability": "summary.text.v1",
            "input": {"text": "...long text..."},
            "timeout_sec": 30,
        })
    """

    name: str = "anp2_task"
    description: str = (
        "Run an ANP2 task end-to-end: publish a kind 50 task.request for "
        "`capability` with the given `input`, then poll the relay until a "
        "provider's kind 52 result + kind 53 verdict arrive (or `timeout_sec` "
        "elapses). Returns the aggregated task thread as JSON."
    )
    args_schema: type[BaseModel] = _TaskInput

    def _run(
        self,
        capability: str,
        input: dict[str, Any],
        constraints: dict[str, Any] | None = None,
        reward: dict[str, Any] | None = None,
        timeout_sec: int = 60,
        poll_interval_sec: float = 2.0,
        **_: Any,
    ) -> str:
        effective_constraints = constraints or {"deadline_sec": 600}
        effective_reward = reward or {"currency": "USD", "amount": 0}
        ack = self.agent.request_task(
            capability=capability,
            input=input,
            constraints=effective_constraints,
            reward=effective_reward,
        )
        task_id = ack["task_id"]

        deadline = time.monotonic() + max(1, int(timeout_sec))
        last_thread: dict[str, Any] = {
            "task_id": task_id,
            "status": "pending",
            "events": [ack.get("event", {})],
        }
        while time.monotonic() < deadline:
            try:
                thread = self.agent.get_task(task_id)
                last_thread = thread
                if thread.get("status") in _TERMINAL_TASK_STATUSES:
                    break
            except Exception as exc:  # noqa: BLE001
                last_thread["last_error"] = repr(exc)
            time.sleep(max(0.5, float(poll_interval_sec)))
        else:
            last_thread.setdefault("status", "timeout")

        return json.dumps(last_thread, separators=(",", ":"), sort_keys=True, default=str)
