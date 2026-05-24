"""Smoke tests: import + construct + verify args_schema."""

from __future__ import annotations

from unittest.mock import MagicMock

from langchain_anp2 import (
    ANP2PublishTool,
    ANP2QueryTool,
    ANP2TaskTool,
)


def _fake_agent() -> MagicMock:
    agent = MagicMock()
    agent.post.return_value = {"id": "ev1", "ok": True}
    agent.declare_capability.return_value = {"id": "ev2", "ok": True}
    agent.query.return_value = [{"id": "ev3", "kind": 1, "tags": [["t", "lobby"]]}]
    agent.request_task.return_value = {
        "task_id": "tk1",
        "event": {"id": "tk1", "kind": 50},
    }
    agent.get_task.return_value = {
        "task_id": "tk1",
        "status": "completed",
        "events": [],
    }
    return agent


def test_publish_kind1() -> None:
    tool = ANP2PublishTool(agent=_fake_agent())
    out = tool.invoke(
        {"kind": 1, "content": "hello", "tags": [("t", "lobby")]}
    )
    assert "ev1" in out


def test_publish_kind4() -> None:
    tool = ANP2PublishTool(agent=_fake_agent())
    out = tool.invoke(
        {
            "kind": 4,
            "capabilities": [
                {
                    "name": "demo.cap.v1",
                    "version": "1.0",
                    "description": "demo",
                    "pricing": {"currency": "USD", "model": "free", "amount": 0},
                }
            ],
        }
    )
    assert "ev2" in out


def test_query_with_capability_filter() -> None:
    agent = _fake_agent()
    agent.query.return_value = [
        {"id": "a", "tags": [["cap", "x.v1"]]},
        {"id": "b", "tags": [["t", "lobby"]]},
    ]
    tool = ANP2QueryTool(agent=agent)
    out = tool.invoke({"capability": "x.v1", "limit": 10})
    assert '"a"' in out
    assert '"b"' not in out


def test_task_completes() -> None:
    tool = ANP2TaskTool(agent=_fake_agent())
    out = tool.invoke(
        {
            "capability": "summary.text.v1",
            "input": {"text": "hi"},
            "timeout_sec": 5,
            "poll_interval_sec": 0.5,
        }
    )
    assert "completed" in out
