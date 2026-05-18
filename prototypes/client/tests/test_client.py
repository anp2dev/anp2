"""Integration-style tests for anp2_client against an in-process relay."""

import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

# Import relay package directly (sibling project)
RELAY_SRC = Path(__file__).resolve().parents[2] / "relay" / "src"
sys.path.insert(0, str(RELAY_SRC))

from anp2_relay.server import create_app  # noqa: E402
from anp2_relay.storage import Storage  # noqa: E402

from anp2_client import Agent, generate_keypair  # noqa: E402


@pytest.fixture(scope="module")
def relay_server(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("relay") / "test.db"
    storage = Storage(db_path)
    app = create_app(storage)
    config = uvicorn.Config(app, host="127.0.0.1", port=18001, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # wait until ready
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    yield "http://127.0.0.1:18001"
    server.should_exit = True
    thread.join(timeout=5)


def test_agent_round_trip(tmp_path, relay_server):
    key_path = tmp_path / "agent.priv"
    a = Agent.load_or_create(key_path, relay_url=relay_server)
    assert len(a.agent_id) == 64
    assert key_path.exists()

    r = a.declare_profile(name="TestBot", description="t", model_family="claude-test")
    assert r["accepted"]

    r = a.declare_capability([{"name": "test.echo", "description": "echoes", "input": "text", "output": "text", "price": "free"}])
    assert r["accepted"]

    r = a.post("hello from test", tags=[("t", "test-room")])
    assert r["accepted"]

    rooms = a.get_rooms()
    assert any(r["room"] == "test-room" for r in rooms)
    caps = a.get_capabilities()
    assert any(c["capability"] == "test.echo" for c in caps)

    events = a.query(kinds=[1], topic="test-room")
    assert len(events) >= 1
    assert events[0]["content"] == "hello from test"

    assert a.has_recent_event(0)
    assert a.has_recent_event(4)


def test_load_persists_identity(tmp_path, relay_server):
    key_path = tmp_path / "persist.priv"
    a1 = Agent.load_or_create(key_path, relay_url=relay_server)
    a2 = Agent.load_or_create(key_path, relay_url=relay_server)
    assert a1.agent_id == a2.agent_id
