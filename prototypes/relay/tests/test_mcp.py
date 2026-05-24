"""MCP Streamable HTTP transport — read-only tool surface."""
import json

import pytest
from fastapi.testclient import TestClient

from anp2_relay.server import create_app
from anp2_relay.storage import Storage


@pytest.fixture
def client(tmp_path):
    storage = Storage(tmp_path / "test.db")
    app = create_app(storage)
    return TestClient(app)


def test_initialize_returns_protocol_version(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["serverInfo"]["name"] == "anp2"
    assert "tools" in body["result"]["capabilities"]


def test_tools_list_returns_6_tools(client):
    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
    })
    assert r.status_code == 200
    body = r.json()
    tools = body["result"]["tools"]
    names = [t["name"] for t in tools]
    for expected in ("anp2_query", "anp2_get_capabilities", "anp2_get_agents",
                     "anp2_get_stats", "anp2_get_balance", "anp2_get_positioning"):
        assert expected in names


def test_tools_call_anp2_get_stats(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "anp2_get_stats", "arguments": {}},
    })
    body = r.json()
    assert "result" in body
    content = body["result"]["content"]
    assert content[0]["type"] == "text"
    inner = json.loads(content[0]["text"])
    # Storage().stats() will return basic counters even on empty db
    assert isinstance(inner, dict)


def test_tools_call_anp2_get_positioning(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "anp2_get_positioning", "arguments": {}},
    })
    body = r.json()
    inner = json.loads(body["result"]["content"][0]["text"])
    assert len(inner["layers_covered_by_anp2"]) == 8
    assert "erc8004" in inner["compares_to"]


def test_unknown_method_returns_error(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 5, "method": "no_such_method",
    })
    body = r.json()
    assert body["error"]["code"] == -32601


def test_tools_call_unknown_tool_returns_error(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "anp2_not_a_tool", "arguments": {}},
    })
    body = r.json()
    assert "error" in body


def test_tools_call_anp2_get_agents_returns_list_shape(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "anp2_get_agents", "arguments": {}},
    })
    body = r.json()
    inner = json.loads(body["result"]["content"][0]["text"])
    assert "agents" in inner
    assert isinstance(inner["agents"], list)


def test_tools_call_anp2_get_capabilities_returns_list_shape(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "anp2_get_capabilities", "arguments": {}},
    })
    body = r.json()
    inner = json.loads(body["result"]["content"][0]["text"])
    assert "capabilities" in inner
    assert isinstance(inner["capabilities"], list)


def test_tools_call_anp2_query_empty_db_returns_no_events(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "anp2_query", "arguments": {"limit": 10}},
    })
    body = r.json()
    inner = json.loads(body["result"]["content"][0]["text"])
    assert "events" in inner
    assert inner["events"] == []


def test_tools_call_anp2_query_respects_limit_clamp(client):
    # limit > 200 should clamp to 200 (no error)
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 10, "method": "tools/call",
        "params": {"name": "anp2_query", "arguments": {"limit": 999}},
    })
    assert r.status_code == 200
    body = r.json()
    assert "error" not in body, body


def test_tools_call_anp2_get_balance_invalid_id_returns_error_shape(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 11, "method": "tools/call",
        "params": {"name": "anp2_get_balance", "arguments": {"agent_id": "short"}},
    })
    body = r.json()
    inner = json.loads(body["result"]["content"][0]["text"])
    assert "error" in inner


def test_tools_call_anp2_get_balance_missing_id_returns_error_shape(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 12, "method": "tools/call",
        "params": {"name": "anp2_get_balance", "arguments": {}},
    })
    body = r.json()
    inner = json.loads(body["result"]["content"][0]["text"])
    assert "error" in inner


def test_ping_method_returns_empty_envelope(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 13, "method": "ping",
    })
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 13
    assert body["result"] == {}


def test_notifications_initialized_returns_empty(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 14, "method": "notifications/initialized",
    })
    body = r.json()
    assert "error" not in body
    assert body["result"] == {}


def test_notifications_cancelled_returns_empty(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 15, "method": "notifications/cancelled",
    })
    body = r.json()
    assert "error" not in body


def test_initialize_includes_instructions_with_8layer_hook(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 16, "method": "initialize",
    })
    instructions = r.json()["result"]["instructions"]
    # Must mention the 8-layer positioning so MCP clients display it
    assert "economic protocol" in instructions.lower()
    assert "identity" in instructions.lower()


def test_tools_list_each_tool_has_input_schema(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 17, "method": "tools/list",
    })
    tools = r.json()["result"]["tools"]
    for t in tools:
        assert "name" in t
        assert "description" in t
        assert "inputSchema" in t
        assert t["inputSchema"]["type"] == "object"


def test_malformed_json_returns_400(client):
    r = client.post("/mcp", data="not-json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_alias_api_mcp_works_same_as_mcp(client):
    """Both POST /mcp and POST /api/mcp must accept the same payload."""
    payload = {"jsonrpc": "2.0", "id": 18, "method": "tools/list"}
    r1 = client.post("/mcp", json=payload)
    r2 = client.post("/api/mcp", json=payload)
    assert r1.json() == r2.json()
