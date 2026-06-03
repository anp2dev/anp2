"""CORS regression tests.

ANP2 is a browser-first, permissionless agent protocol: the public API must be
callable from ANY web origin (a browser-spawned agent on any page, the /try
on-ramp, third-party Spaces/playgrounds). Before 2026-06-03 the relay had no
CORS, so cross-origin fetch() preflighted 405 and the response body was
unreadable to JS on any origin other than anp2.com — a silent adoption blocker
for exactly the browser audience the on-ramp targets.

These lock the permissive-CORS behavior. Safe because the API carries no ambient
authority for CORS to protect: every write is gated by an Ed25519 signature +
PIP-002 PoW the caller must compute; there are no cookies / session auth.
"""

from fastapi.testclient import TestClient

from anp2_relay.server import create_app
from anp2_relay.storage import Storage

OTHER_ORIGIN = "https://huggingface.co"


def test_preflight_allows_cross_origin_post(tmp_path):
    client = TestClient(create_app(Storage(tmp_path / "t.db")))
    r = client.options(
        "/events",
        headers={
            "Origin": OTHER_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "*"
    allow_methods = r.headers.get("access-control-allow-methods", "")
    assert "POST" in allow_methods or allow_methods == "*"


def test_simple_get_echoes_allow_origin(tmp_path):
    client = TestClient(create_app(Storage(tmp_path / "t.db")))
    r = client.get("/events?limit=1", headers={"Origin": OTHER_ORIGIN})
    # Whatever the route status, the CORS header must be present so JS can read it.
    assert r.headers.get("access-control-allow-origin") == "*"


def test_no_credentials_with_wildcard(tmp_path):
    # allow_credentials must be False when origins=* (browsers reject the combo,
    # and the API has no cookies to send anyway).
    client = TestClient(create_app(Storage(tmp_path / "t.db")))
    r = client.get("/events?limit=1", headers={"Origin": OTHER_ORIGIN})
    assert r.headers.get("access-control-allow-credentials") != "true"
