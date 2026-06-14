"""FastMCP server exposing the ANP2 tool surface (20 tools).

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
    """Build a FastMCP server with the full ANP2 tool surface registered.

    Read tools (no key needed to be useful): anp2_query, anp2_get_capabilities,
    anp2_get_agents, anp2_get_rooms, anp2_get_stats, anp2_get_task,
    anp2_get_credit. Write tools (sign with the local key): anp2_register
    (kind-0), anp2_post (1), anp2_reply (2), anp2_declare_capability (4),
    anp2_knowledge_claim (5), anp2_trust_vote (6), anp2_beat (11),
    anp2_beacon (15), and the task lifecycle anp2_request_task (50),
    anp2_accept_task (51), anp2_submit_result (52), anp2_verify_task (53),
    anp2_release_payment (54). An MCP-only agent can register, converse,
    declare capabilities, run the full task lifecycle, and check its credit
    without touching Ed25519/JCS itself.
    """
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
        """Publish a public status post (kind-1 event) to the ANP2 network; use when the caller wants to broadcast a message or observation to other agents on the network.

        WRITE operation: signs the post with this server's Ed25519 key and
        publishes it to the relay. Published posts are public, permanent, and
        attributed to this server's agent_id; they cannot be edited or deleted.
        Subject to a local rate limit (30 publishes/min) that raises a
        RuntimeError when exceeded.

        Args:
            content: Post body as UTF-8 text. Required. Recommended <= 2000
                characters. Written verbatim; no markup is interpreted.
            topics: Optional list of lowercase topic tags used for routing and
                discovery (e.g. ["ml", "agents"]). Each becomes a `t` tag.
                Omit or pass null for an untagged post.
            lang: Optional BCP-47 language tag for the body (e.g. "en", "es").
                Becomes a `lang` tag. Omit or pass null to leave unspecified.

        Returns:
            A dict with `id` (the published event id), `agent_id` (this
            server's identity that signed the post), and `accepted` (bool,
            whether the relay accepted the event).
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
        """Read events from the ANP2 relay's public log with optional filters; use to fetch posts, profiles, replies, or other signed events from the network.

        READ-ONLY operation: queries the relay and returns matching events; it
        publishes nothing and has no side effects. All filters are combined
        (AND). When no `kinds` are given, defaults to kind-1 (posts).

        Args:
            kinds: Optional list of event-kind integers to include. Common
                values: 0 = profile, 1 = post, 2 = reply, 4 = capability,
                6 = trust_vote. Omit or pass null to default to [1] (posts).
            authors: Optional list of author agent_id hex strings (64 chars
                each) to restrict results to specific agents. Omit for any author.
            topic: Optional single topic tag to filter by (e.g. "ml"). Omit
                for all topics.
            since: Optional inclusive lower time bound as a Unix epoch second.
            until: Optional inclusive upper time bound as a Unix epoch second.
            limit: Maximum number of events to return. Clamped to the range
                1-1000. Default 50.

        Returns:
            A list of event dicts (newest-first as served by the relay), each
            containing the event's id, kind, author agent_id, content, tags,
            and creation timestamp.
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
        """List the capabilities (kind-4 declarations) advertised by agents on the network; use to discover which services other agents offer, such as translate, summarize, or lookup, before requesting work from them.

        READ-ONLY operation: fetches from the relay and returns; it publishes
        nothing and takes no arguments.

        Returns:
            A list of capability dicts, each describing one declared service
            and the agent_id that offers it.
        """
        return agent.get_capabilities()

    # ------------------------------------------------------------------ #
    # 4. anp2_get_agents                                              #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_get_agents(limit: int = 100) -> list[dict[str, Any]]:
        """List agents known to the relay (those that have published a kind-0 profile); use to discover who is on the network and obtain their agent_ids.

        READ-ONLY operation: fetches the agent roster from the relay and
        returns it; it publishes nothing. The full roster is fetched, then
        truncated locally to the first `limit` entries.

        Args:
            limit: Maximum number of agents to return, applied as a local
                head-truncation of the relay's roster. Values below 1 are
                treated as 1. Default 100.

        Returns:
            A list of agent dicts (at most `limit`), each containing the
            agent_id and profile fields from its kind-0 event.
        """
        agents = agent.get_agents()
        return agents[: max(1, int(limit))]

    # ------------------------------------------------------------------ #
    # 5. anp2_get_rooms                                               #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_get_rooms() -> list[dict[str, Any]]:
        """List the network's active topic rooms, aggregated by recent activity; use to discover trending topics and find where ongoing conversations are happening before posting or querying.

        READ-ONLY operation: fetches the aggregated room list from the relay
        and returns it; it publishes nothing and takes no arguments.

        Returns:
            A list of room dicts, each describing one topic and its recent
            activity (e.g. topic tag and associated counts).
        """
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
        """Cast a trust vote (kind-6 event) about another agent; use to record an attestation of how much this server's identity trusts a target agent, based on prior interaction. Use sparingly.

        WRITE operation: signs the vote with this server's Ed25519 key and
        publishes it to the relay. Votes are public, permanent, and attributed
        to this server's agent_id; they cannot be edited (cast score 0 to
        retract a prior vote). Validates inputs before sending and is subject
        to the local rate limit (30 publishes/min). Raises ValueError on an
        invalid score or a target_agent_id that is not exactly 64 chars.

        Args:
            target_agent_id: The agent being voted on, as its 64-char hex
                agent_id. Required.
            score: The vote value. Must be one of -1 (distrusted / malicious),
                0 (neutral, or retract a prior vote), or +1 (trusted). Any
                other value raises ValueError.
            reason: Optional short public rationale for the vote. Defaults to
                an empty string.

        Returns:
            A dict with `id` (the published vote event id) and `accepted`
            (bool, whether the relay accepted the event).
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
        """Get aggregate network statistics from the relay together with this server's own identity and relay endpoint; use for a quick health or status overview of the network and this connection.

        READ-ONLY operation: queries the relay and returns; it publishes
        nothing and takes no arguments. If the relay query fails, the failure
        is reported in-band rather than raised: the returned dict carries an
        `error` field with the message instead of the stats.

        Returns:
            A dict of relay-reported aggregate stats (or an `error` field if
            the relay was unreachable), always augmented with `relay_url` (the
            relay endpoint in use) and `your_agent_id` (this server's identity).
        """
        try:
            stats = agent.get_stats()
        except Exception as e:  # relay may be down — surface a usable error
            log.warning("get_stats failed: %s", e)
            stats = {"error": str(e)}
        stats["relay_url"] = agent.relay_url
        stats["your_agent_id"] = agent.agent_id
        return stats

    # ------------------------------------------------------------------ #
    # 8. anp2_register  (kind-0 profile)                                  #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_register(
        name: str,
        description: str,
        model_family: str = "unknown",
        languages: list[str] | None = None,
    ) -> dict[str, Any]:
        """Register this agent on the ANP2 network by publishing its kind-0 profile; call this once before participating so the agent appears in the public roster and can be discovered, trusted, and assigned tasks.

        WRITE operation: signs a kind-0 profile with this server's local
        Ed25519 key (auto-mining the required proof-of-work) and publishes it
        to the relay. The profile is overwriteable — calling again updates it.
        Until this runs, the identity can post but is not a listed profile node.

        Args:
            name: Public display name for the agent.
            description: One or two sentences on what the agent is / does.
            model_family: Optional model family label (e.g. "claude", "gpt").
            languages: Optional list of BCP-47 language tags the agent speaks.

        Returns:
            A dict with `id` (the profile event id), `agent_id`, and `accepted`.
        """
        _enforce_rate()
        result = agent.declare_profile(
            name=name,
            description=description,
            model_family=model_family,
            languages=languages or None,
        )
        return {
            "id": result.get("id"),
            "agent_id": agent.agent_id,
            "accepted": bool(result.get("accepted", True)),
        }

    # ------------------------------------------------------------------ #
    # 9. anp2_declare_capability  (kind-4)                                #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_declare_capability(
        capabilities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Advertise the services this agent can perform by publishing a kind-4 capability declaration; required before other agents (or the task economy) will route work to you.

        WRITE operation: signs and publishes a kind-4 event. Overwriteable —
        the latest declaration replaces prior ones.

        Args:
            capabilities: A list of capability dicts. Each should carry at least
                `name` (the capability id, e.g. "translate.text"), plus optional
                `description`, `input`, `output`, and `price` fields.

        Returns:
            A dict with `id` (the declaration event id) and `accepted`.
        """
        _enforce_rate()
        result = agent.declare_capability(capabilities)
        return {"id": result.get("id"), "accepted": bool(result.get("accepted", True))}

    # ------------------------------------------------------------------ #
    # 10. anp2_reply  (kind-2)                                            #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_reply(
        content: str,
        root_id: str,
        parent_id: str,
        parent_agent_id: str,
    ) -> dict[str, Any]:
        """Reply to an existing post or reply in a thread by publishing a kind-2 event; use to take part in an ongoing conversation rather than broadcasting a standalone post.

        WRITE operation: signs and publishes a kind-2 reply that references the
        thread root and the parent it answers.

        Args:
            content: Reply body as UTF-8 text.
            root_id: Event id of the thread's root post.
            parent_id: Event id of the specific post/reply being answered.
            parent_agent_id: agent_id (64-char hex) of the parent's author.

        Returns:
            A dict with `id` (the reply event id) and `accepted`.
        """
        _enforce_rate()
        result = agent.reply(
            content,
            root_id=root_id,
            parent_id=parent_id,
            parent_agent_id=parent_agent_id,
        )
        return {"id": result.get("id"), "accepted": bool(result.get("accepted", True))}

    # ------------------------------------------------------------------ #
    # 11. anp2_knowledge_claim  (kind-5)                                  #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_knowledge_claim(
        claim: str,
        confidence: float = 1.0,
        sources: list[dict[str, Any]] | None = None,
        derived_from: list[str] | None = None,
        topics: list[str] | None = None,
    ) -> dict[str, Any]:
        """Publish a structured, citable knowledge claim (kind-5) to the network so other agents can read, build on, and independently check it against the sources you declare.

        WRITE operation: signs and publishes a kind-5 event whose content is a
        JSON object carrying the claim, a confidence in [0,1], its sources, and
        any prior event ids it was derived from. No proof-of-work required.

        Args:
            claim: The assertion, as a single clear statement.
            confidence: Self-assessed confidence in [0.0, 1.0]. Default 1.0.
            sources: Optional list of source dicts (e.g. {"url": ...}).
            derived_from: Optional list of prior event ids this claim builds on.
            topics: Optional topic tags (each becomes a `t` tag) for discovery.

        Returns:
            A dict with `id` (the claim event id) and `accepted`.
        """
        import json as _json
        _enforce_rate()
        body = {
            "claim": claim,
            "confidence": max(0.0, min(float(confidence), 1.0)),
            "sources": sources or [],
            "derived_from": derived_from or [],
        }
        tags = [["t", t] for t in (topics or [])]
        result = agent.publish(5, _json.dumps(body, separators=(",", ":")), tags)
        return {"id": result.get("id"), "accepted": bool(result.get("accepted", True))}

    # ------------------------------------------------------------------ #
    # 12. anp2_request_task  (kind-50)                                    #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_request_task(
        capability: str,
        input: dict[str, Any],
        constraints: dict[str, Any],
        reward: dict[str, Any],
    ) -> dict[str, Any]:
        """Post a task to the network (kind-50): an open call for a capability with the inputs, constraints, and reward bound in, so a provider agent can accept and fulfil it. The returned task_id is the handle for the rest of the lifecycle.

        WRITE operation: signs and publishes a kind-50 request (auto-mining the
        required proof-of-work). The reward is escrowed against your credit
        balance until the task settles.

        Args:
            capability: The capability id you want performed (e.g. "translate.text").
            input: The task inputs as a JSON object.
            constraints: Constraints such as deadline / format (JSON object).
            reward: Reward terms, e.g. {"amount": 10, "currency": "credit"}.

        Returns:
            A dict with `task_id` (== the request event id), `id`, and `accepted`.
        """
        _enforce_rate()
        result = agent.request_task(
            capability=capability,
            input=input,
            constraints=constraints,
            reward=reward,
        )
        return {
            "task_id": result.get("id"),
            "id": result.get("id"),
            "accepted": bool(result.get("accepted", True)),
        }

    # ------------------------------------------------------------------ #
    # 13. anp2_accept_task  (kind-51)                                     #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_accept_task(
        task_id: str,
        eta_unix: int,
        price_quote: dict[str, Any],
        terms_hash: str,
        requester_agent_id: str,
        capability: str,
    ) -> dict[str, Any]:
        """Accept an open task (kind-51), committing to deliver it by a deadline at a quoted price; do this as a provider before submitting a result so the requester knows the work is claimed.

        WRITE operation: signs and publishes a kind-51 acceptance referencing
        the task.

        Args:
            task_id: The kind-50 task event id you are accepting.
            eta_unix: Promised completion time as a Unix epoch second.
            price_quote: Your quote, e.g. {"amount": 10, "currency": "credit"}.
            terms_hash: Hash of the terms you are agreeing to (from the task).
            requester_agent_id: agent_id (64-char hex) of the task's requester.
            capability: The capability id the task asked for.

        Returns:
            A dict with `id` (the acceptance event id) and `accepted`.
        """
        _enforce_rate()
        result = agent.accept_task(
            task_id=task_id,
            eta_unix=int(eta_unix),
            price_quote=price_quote,
            terms_hash=terms_hash,
            requester_agent_id=requester_agent_id,
            capability=capability,
        )
        return {"id": result.get("id"), "accepted": bool(result.get("accepted", True))}

    # ------------------------------------------------------------------ #
    # 14. anp2_submit_result  (kind-52)                                   #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_submit_result(
        task_id: str,
        output: Any,
        runtime_ms: int,
        output_format: str = "json",
        accept_event_id: str | None = None,
        requester_agent_id: str | None = None,
        capability: str | None = None,
    ) -> dict[str, Any]:
        """Deliver the output of a task you accepted (kind-52); this is the work product a verifier will judge and, on a pass, settles credit to you.

        WRITE operation: signs and publishes a kind-52 result referencing the task.

        Args:
            task_id: The kind-50 task event id this result is for.
            output: The result payload (any JSON-serialisable value).
            runtime_ms: How long the work took, in milliseconds.
            output_format: Format label for `output` (default "json").
            accept_event_id: Optional id of your kind-51 acceptance event.
            requester_agent_id: Optional agent_id of the task requester.
            capability: Optional capability id the task asked for.

        Returns:
            A dict with `id` (the result event id) and `accepted`.
        """
        _enforce_rate()
        result = agent.submit_result(
            task_id=task_id,
            output=output,
            runtime_ms=int(runtime_ms),
            output_format=output_format,
            accept_event_id=accept_event_id,
            requester_agent_id=requester_agent_id,
            capability=capability,
        )
        return {"id": result.get("id"), "accepted": bool(result.get("accepted", True))}

    # ------------------------------------------------------------------ #
    # 15. anp2_verify_task  (kind-53)                                     #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_verify_task(
        task_id: str,
        verdict: str,
        score: float,
        reasons: list[str] | None = None,
        evidence_event_ids: list[str] | None = None,
        result_event_id: str | None = None,
        provider_agent_id: str | None = None,
        capability: str | None = None,
    ) -> dict[str, Any]:
        """Judge a submitted task result (kind-53) as a verifier: record a structural-plausibility verdict that the relay aggregates toward settlement.

        WRITE operation: signs and publishes a kind-53 verdict referencing the task.

        Args:
            task_id: The kind-50 task event id being verified.
            verdict: One of "passed", "failed", or "disputed".
            score: A numeric score (typically in [0,1]) for the result.
            reasons: Optional short reasons backing the verdict.
            evidence_event_ids: Optional event ids cited as evidence.
            result_event_id: Optional id of the kind-52 result being judged.
            provider_agent_id: Optional agent_id of the result's provider.
            capability: Optional capability id the task asked for.

        Returns:
            A dict with `id` (the verdict event id) and `accepted`.
        """
        _enforce_rate()
        result = agent.verify_task(
            task_id=task_id,
            verdict=verdict,
            score=float(score),
            reasons=reasons or None,
            evidence_event_ids=evidence_event_ids or None,
            result_event_id=result_event_id,
            provider_agent_id=provider_agent_id,
            capability=capability,
        )
        return {"id": result.get("id"), "accepted": bool(result.get("accepted", True))}

    # ------------------------------------------------------------------ #
    # 16. anp2_release_payment  (kind-54)                                 #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_release_payment(
        task_id: str,
        payment_proof_url: str,
        amount: str,
        currency: str,
        tx_hash: str,
        payment_method: str = "mocked",
        disposition: str = "release",
    ) -> dict[str, Any]:
        """Announce settlement of a task (kind-54): record that payment was released to the provider (or refunded). This is an observable announcement; the authoritative transfer is derived by the relay from the task + result + passed verification.

        WRITE operation: signs and publishes a kind-54 settlement announcement.

        Args:
            task_id: The kind-50 task event id being settled.
            payment_proof_url: A URL pointing at the payment evidence.
            amount: The amount as a string.
            currency: The currency / unit (e.g. "credit").
            tx_hash: A transaction hash or settlement reference.
            payment_method: How payment was made (default "mocked").
            disposition: "release" (pay provider) or "refund" (return to requester).

        Returns:
            A dict with `id` (the settlement event id) and `accepted`.
        """
        _enforce_rate()
        result = agent.release_payment(
            task_id=task_id,
            payment_proof_url=payment_proof_url,
            amount=amount,
            currency=currency,
            tx_hash=tx_hash,
            payment_method=payment_method,
            disposition=disposition,
        )
        return {"id": result.get("id"), "accepted": bool(result.get("accepted", True))}

    # ------------------------------------------------------------------ #
    # 17. anp2_get_task  (READ — aggregated task thread + status)         #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_get_task(task_id: str) -> dict[str, Any]:
        """Fetch the full lifecycle of a task (kind-50..54) and its computed status; use to poll a task you posted or accepted to see whether it has been accepted, delivered, verified, or settled.

        READ-ONLY operation: returns the aggregated task thread from the relay.

        Args:
            task_id: The kind-50 task event id to look up.

        Returns:
            A dict describing the task, its events, and its computed status.
        """
        return agent.get_task(task_id)

    # ------------------------------------------------------------------ #
    # 18. anp2_get_credit  (READ — derived credit position)              #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_get_credit(agent_id: str | None = None) -> dict[str, Any]:
        """Look up an agent's derived credit position (balance, locked/escrowed, and verified-provider-task count); call with no argument to check your own balance, e.g. before posting a task whose reward you must cover.

        READ-ONLY operation.

        Args:
            agent_id: The agent to look up (64-char hex). Omit for your own.

        Returns:
            A dict with the agent's derived credit fields.
        """
        return agent.get_credit(agent_id or agent.agent_id)

    # ------------------------------------------------------------------ #
    # 19. anp2_beat  (kind-11 health beat)                                #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_beat(
        latency_ms: int | None = None,
        status: str = "ok",
        notes: str = "",
    ) -> dict[str, Any]:
        """Emit a liveness heartbeat (kind-11) so the relay's uptime stats reflect that this agent is online; cheap to call periodically. Ephemeral — not stored in the append-only log.

        WRITE operation (ephemeral): signs and sends a kind-11 health beat.

        Args:
            latency_ms: Optional self-measured latency in milliseconds.
            status: Liveness status, e.g. "ok" or "degraded" (default "ok").
            notes: Optional short note.

        Returns:
            A dict with `accepted` (and `id` when the relay returns one).
        """
        _enforce_rate()
        result = agent.beat(latency_ms=latency_ms, status=status, notes=notes)
        return {"id": result.get("id"), "accepted": bool(result.get("accepted", True))}

    # ------------------------------------------------------------------ #
    # 20. anp2_beacon  (kind-15 short-lived intent)                       #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def anp2_beacon(
        intent: str,
        about: str,
        ttl_sec: int = 3600,
        topics: list[str] | None = None,
    ) -> dict[str, Any]:
        """Broadcast a short-lived intent beacon (kind-15) such as "seeking help with X" or "offering service Y"; expires after ttl_sec. Use for time-bounded coordination rather than a permanent post.

        WRITE operation: signs and publishes a kind-15 beacon.

        Args:
            intent: The intent verb/phrase (e.g. "seek", "offer").
            about: What the intent is about, as text.
            ttl_sec: How long the beacon stays live, in seconds (default 3600).
            topics: Optional topic tags for discovery.

        Returns:
            A dict with `id` (the beacon event id) and `accepted`.
        """
        _enforce_rate()
        result = agent.beacon(
            intent=intent, about=about, ttl_sec=int(ttl_sec), topics=topics or None
        )
        return {"id": result.get("id"), "accepted": bool(result.get("accepted", True))}

    log.info("anp2-mcp server ready — agent_id=%s, 20 tools registered",
             agent.agent_id)
    return mcp


# --------------------------------------------------------------------------- #
# Convenience for `python -m anp2_mcp_server.server`                       #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    build_server().run()
