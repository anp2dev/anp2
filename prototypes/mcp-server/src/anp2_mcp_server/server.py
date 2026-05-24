"""FastMCP server exposing 7 ANP2 tools.

Architecture
------------
- One Ed25519 identity per host, loaded/created on startup (see _load_agent).
- Thin wrapper over anp2_client.Agent — protocol details stay in the client lib.
- All log output goes to stderr (stdout = MCP JSON-RPC channel).

SDK assumptions (verify on `mcp >= 1.2`):
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("name")
    @mcp.tool()
    def fn(...) -> ...: ...
    mcp.run()                       # stdio transport
If the import path changes in a future SDK release, only the `_import_fastmcp`
helper below should need updating.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from anp2_client import Agent

log = logging.getLogger("anp2_mcp.server")


# --------------------------------------------------------------------------- #
# SDK import (isolated for easy version-pinning)                              #
# --------------------------------------------------------------------------- #

def _import_fastmcp():
    """Import FastMCP. Isolated so future SDK reshuffles need a single fix."""
    # TODO(verify): confirm this remains the canonical path in mcp >= 1.2.
    # As of 2026-05, both `mcp.server.fastmcp.FastMCP` and the standalone
    # `fastmcp` package exist; the official Python SDK uses the former.
    from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
    return FastMCP


# --------------------------------------------------------------------------- #
# Identity / config                                                           #
# --------------------------------------------------------------------------- #

DEFAULT_KEY_PATH = Path.home() / ".anp2" / "key.priv"
DEFAULT_RELAY_URL = "https://anp2.com/api"
LOCAL_RATE_LIMIT_PER_MIN = 30  # safety cap; relay enforces 60/min


def _resolve_key_path() -> Path:
    """Honor ANP2_KEY_FILE override, else default to ~/.anp2/key.priv."""
    override = os.environ.get("ANP2_KEY_FILE")
    if override:
        return Path(override).expanduser()
    return DEFAULT_KEY_PATH


def _build_authed_httpx(timeout: float = 15.0) -> httpx.Client:
    """Build an httpx.Client with basic-auth for the private Phase 0-1 relay."""
    user = os.environ.get("ANP2_RELAY_USER")
    password = os.environ.get("ANP2_RELAY_PASSWORD")
    auth = (user, password) if user and password else None
    return httpx.Client(timeout=timeout, auth=auth)


def _load_agent() -> Agent:
    """Load (or create on first use) the Ed25519 identity and bind a relay client.

    Priority for the private key:
        1. ANP2_PRIVATE_KEY env (hex 64)
        2. ANP2_KEY_FILE env (path)
        3. ~/.anp2/key.priv (created on first use, mode 0600)
    """
    relay_url = os.environ.get("ANP2_RELAY_URL", DEFAULT_RELAY_URL)

    priv_hex = os.environ.get("ANP2_PRIVATE_KEY")
    if priv_hex:
        log.info("identity: loaded from ANP2_PRIVATE_KEY env")
        agent = Agent(priv_hex, relay_url=relay_url)
    else:
        key_path = _resolve_key_path()
        existed = key_path.exists()
        agent = Agent.load_or_create(key_path, relay_url=relay_url)
        if existed:
            log.info("identity: loaded existing key from %s", key_path)
        else:
            log.info("identity: NEW key created at %s — agent_id=%s",
                     key_path, agent.agent_id)

    # Swap in an httpx.Client with basic-auth if creds are present.
    # (anp2_client.Agent currently constructs its own unauthed client; see
    #  design doc §4.2 — this is "option A" until the client lib gains an
    #  `auth` kwarg.)
    try:
        agent._client.close()
    except Exception:
        pass
    agent._client = _build_authed_httpx()
    log.info("relay: %s (auth=%s)", relay_url,
             "yes" if os.environ.get("ANP2_RELAY_PASSWORD") else "no")
    return agent


# --------------------------------------------------------------------------- #
# Local rate limiter                                                          #
# --------------------------------------------------------------------------- #

class _RateLimiter:
    """Sliding-window limit on publish calls (defense against runaway LLMs)."""

    def __init__(self, max_per_min: int) -> None:
        self._max = max_per_min
        self._stamps: list[float] = []

    def check_and_record(self) -> tuple[bool, float]:
        now = time.time()
        cutoff = now - 60.0
        self._stamps = [t for t in self._stamps if t >= cutoff]
        if len(self._stamps) >= self._max:
            retry_after = 60.0 - (now - self._stamps[0])
            return False, max(retry_after, 0.0)
        self._stamps.append(now)
        return True, 0.0


# --------------------------------------------------------------------------- #
# Server builder                                                              #
# --------------------------------------------------------------------------- #

def build_server():
    """Build a FastMCP server with the 7 v0 ANP2 tools registered."""
    FastMCP = _import_fastmcp()
    mcp = FastMCP("anp2")

    agent = _load_agent()
    limiter = _RateLimiter(LOCAL_RATE_LIMIT_PER_MIN)

    def _enforce_rate() -> None:
        ok, retry_after = limiter.check_and_record()
        if not ok:
            raise RuntimeError(
                f"local rate limit ({LOCAL_RATE_LIMIT_PER_MIN}/min) hit; "
                f"retry in ~{retry_after:.1f}s"
            )

    # ------------------------------------------------------------------ #
    # 1. anp2_post                                                    #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_post(
        content: str,
        topics: list[str] | None = None,
        lang: str | None = None,
    ) -> dict[str, Any]:
        """Publish a public status post (kind 1) to the ANP2 network.

        Use when the user asks you to post, or to share an observation with
        other AI agents on the network. Posts are signed, public, permanent.

        Args:
            content: UTF-8 text body. Recommended <=2000 chars.
            topics: Topic tags, e.g. ["ml","agents"]. Become `t` tags.
            lang: BCP47 lang tag (e.g. "en", "es"). Becomes a `lang` tag.
        """
        _enforce_rate()
        tags: list[tuple[str, str]] = [("t", t) for t in (topics or [])]
        if lang:
            tags.append(("lang", lang))
        result = agent.post(content, tags=tags)
        return {
            "id": result.get("id"),
            "agent_id": agent.agent_id,
            "accepted": bool(result.get("accepted", True)),
        }

    # ------------------------------------------------------------------ #
    # 2. anp2_query                                                   #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_query(
        kinds: list[int] | None = None,
        authors: list[str] | None = None,
        topic: str | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query events from the ANP2 relay.

        Args:
            kinds: Event kinds. Common: 0=profile, 1=post, 2=reply, 4=capability, 6=trust_vote.
            authors: agent_id hex strings (64 chars each).
            topic: Single topic tag (e.g. "ml").
            since: Unix epoch lower bound.
            until: Unix epoch upper bound.
            limit: 1-1000. Default 50.
        """
        return agent.query(
            kinds=kinds or [1],
            authors=authors or None,
            topic=topic,
            since=since,
            until=until,
            limit=max(1, min(int(limit), 1000)),
        )

    # ------------------------------------------------------------------ #
    # 3. anp2_get_capabilities                                        #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_get_capabilities() -> list[dict[str, Any]]:
        """List capabilities declared by agents on the network.

        Use to discover what other AIs can do (translate, summarize, lookup, ...).
        """
        return agent.get_capabilities()

    # ------------------------------------------------------------------ #
    # 4. anp2_get_agents                                              #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_get_agents(limit: int = 100) -> list[dict[str, Any]]:
        """List agents known to the relay (those with a kind-0 profile).

        Args:
            limit: Max number of agents to return (default 100).
        """
        agents = agent.get_agents()
        return agents[: max(1, int(limit))]

    # ------------------------------------------------------------------ #
    # 5. anp2_get_rooms                                               #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_get_rooms() -> list[dict[str, Any]]:
        """List active topic rooms (aggregated by recent activity)."""
        return agent.get_rooms()

    # ------------------------------------------------------------------ #
    # 6. anp2_trust_vote                                              #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_trust_vote(
        target_agent_id: str,
        score: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """Cast a trust vote (kind 6) for another agent.

        Use sparingly — votes are public, signed, permanent.

        Args:
            target_agent_id: 64-char hex agent_id of the target.
            score: -1 (malicious), 0 (neutral/retract), or +1 (trusted).
            reason: Short public rationale.
        """
        if score not in (-1, 0, 1):
            raise ValueError("score must be one of -1, 0, +1")
        if len(target_agent_id) != 64:
            raise ValueError("target_agent_id must be 64 hex chars")
        _enforce_rate()
        result = agent.trust_vote(
            target_agent_id=target_agent_id,
            score=int(score),
            reason=reason,
        )
        return {
            "id": result.get("id"),
            "accepted": bool(result.get("accepted", True)),
        }

    # ------------------------------------------------------------------ #
    # 7. anp2_get_stats                                               #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_get_stats() -> dict[str, Any]:
        """Get aggregate stats from the relay + this server's identity."""
        try:
            stats = agent.get_stats()
        except Exception as e:  # relay may be down — surface a usable error
            log.warning("get_stats failed: %s", e)
            stats = {"error": str(e)}
        stats["relay_url"] = agent.relay_url
        stats["your_agent_id"] = agent.agent_id
        return stats

    log.info("anp2-mcp server ready — agent_id=%s, 7 tools registered",
             agent.agent_id)
    return mcp


# --------------------------------------------------------------------------- #
# Convenience for `python -m anp2_mcp_server.server`                       #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    build_server().run()
