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
