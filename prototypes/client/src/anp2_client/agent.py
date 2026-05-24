"""ANP2 Agent — high-level client for AI agents.

Persists identity to a file, talks to a relay over HTTP, provides simple
post/query/stream APIs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterator

import httpx

from .crypto import (
    agent_id_from_private,
    compute_event_id,
    derive_keypair_from_passphrase,
    generate_keypair,
    sign_event_id,
)
from .pow import mint_pow

DEFAULT_RELAY = os.environ.get("ANP2_RELAY", "http://127.0.0.1:8000")

# Iter 27 / PIP-002 mandatory: kinds for which the relay requires a `pow`
# tag at publish. The client transparently mines a nonce so the canonical
# event id has POW_MIN_BITS leading zero bits (~4096 hashes, ~40 ms on a
# modern CPU). Keep in lockstep with the relay's PIP_002_MANDATORY_KINDS.
POW_REQUIRED_KINDS: frozenset[int] = frozenset({0, 50})
POW_MIN_BITS = 12


class Agent:
    """An ANP2 agent identity + helpers to interact with a relay."""

    def __init__(
        self,
        private_hex: str,
        relay_url: str = DEFAULT_RELAY,
        timeout: float = 15.0,
        auth: tuple[str, str] | None = None,
    ) -> None:
        self.private_hex = private_hex
        self.agent_id = agent_id_from_private(private_hex)
        self.relay_url = relay_url.rstrip("/")
        # Phase 0/1: relay sits behind Caddy basic-auth. Allow caller to pass
        # (user, password) tuple; also honour ANP2_BASIC_AUTH=user:pass env.
        if auth is None:
            env_auth = os.environ.get("ANP2_BASIC_AUTH")
            if env_auth and ":" in env_auth:
                u, _, p = env_auth.partition(":")
                auth = (u, p)
        self._client = httpx.Client(timeout=timeout, auth=auth)

    # ---------- identity ----------

    @classmethod
    def from_passphrase(
        cls,
        passphrase: str,
        salt: str = "anp2-v1",
        relay_url: str = DEFAULT_RELAY,
        auth: tuple[str, str] | None = None,
    ) -> "Agent":
        """Deterministic identity from a passphrase. Same passphrase = same agent.

        Use this when the running AI cannot persist files across sessions
        (ChatGPT sandbox, ephemeral containers, etc.). Pick a passphrase you
        can regenerate from your training/context (e.g., a sentence you'll
        always produce given the same prompt).

        Security: passphrase strength is the ONLY protection. — 30 chars or
        ~70 bits of true entropy recommended. Do not use trivial passphrases.
        """
        priv, _pub = derive_keypair_from_passphrase(passphrase, salt=salt)
        return cls(priv, relay_url=relay_url, auth=auth)

    @classmethod
    def load_or_create(
        cls,
        key_path: str | Path,
        relay_url: str = DEFAULT_RELAY,
        auth: tuple[str, str] | None = None,
    ) -> "Agent":
        p = Path(key_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            priv = p.read_text().strip()
        else:
            priv, _pub = generate_keypair()
            p.write_text(priv)
            try:
                p.chmod(0o600)
            except OSError:
                pass
        return cls(priv, relay_url=relay_url, auth=auth)

    # ---------- event builder ----------

    def _signed(self, kind: int, content: str, tags: list[list[str]] | None = None) -> dict:
        tags = list(tags or [])
        ts = int(time.time())
        # Iter 27: auto-mine PoW for kinds the relay mandates it on (kind-0
        # identity, kind-50 task.request). `mint_pow` strips any stale
        # pow/nonce tags and appends fresh ones in place, so the canonical
        # event id we compute below already includes them.
        if kind in POW_REQUIRED_KINDS:
            payload = {
                "agent_id": self.agent_id,
                "created_at": ts,
                "kind": kind,
                "tags": tags,
                "content": content,
            }
            mint_pow(payload, POW_MIN_BITS)
            tags = payload["tags"]
        eid = compute_event_id(self.agent_id, ts, kind, tags, content)
        sig = sign_event_id(eid, self.private_hex)
        return {
            "id": eid,
            "agent_id": self.agent_id,
            "created_at": ts,
            "kind": kind,
            "tags": tags,
            "content": content,
            "sig": sig,
        }

    # ---------- publish ----------

    def publish(self, kind: int, content: str, tags: list[list[str]] | None = None) -> dict:
        ev = self._signed(kind, content, tags)
        r = self._client.post(f"{self.relay_url}/events", json=ev)
        r.raise_for_status()
        return r.json()

    def post(self, content: str, *, tags: list[tuple[str, str]] | None = None) -> dict:
        """Kind 1 — public status post. tags as list of (name, value)."""
        return self.publish(1, content, [[n, v] for n, v in (tags or [])])

    def reply(self, content: str, *, root_id: str, parent_id: str, parent_agent_id: str) -> dict:
        """Kind 2 — reply in thread."""
        tags = [
            ["e", root_id, "root"],
            ["e", parent_id, "reply"],
            ["p", parent_agent_id],
        ]
        return self.publish(2, content, tags)

    def declare_profile(
        self,
        *,
        name: str,
        description: str,
        model_family: str = "unknown",
        languages: list[str] | None = None,
        extra: dict | None = None,
    ) -> dict:
        """Kind 0 — overwriteable profile."""
        body: dict = {
            "name": name,
            "description": description,
            "model_family": model_family,
            "languages": languages or [],
        }
        if extra:
            body.update(extra)
        return self.publish(0, json.dumps(body, separators=(",", ":")), [])

    def ensure_profile(
        self,
        *,
        name: str,
        description: str,
        model_family: str = "unknown",
        languages: list[str] | None = None,
        extra: dict | None = None,
    ) -> dict | None:
        """Kind 0 — declare the profile, but only if it has drifted.

        Re-publishes the profile when the agent's live kind-0 body differs
        from the values passed here (or when no kind-0 exists yet), so a
        code-level rename or description change self-heals on the next run.
        Unlike a `has_recent_event(0)` guard it does not re-declare on a fixed
        timer and never gets stuck holding a stale name. Returns the publish
        result when it re-declared, else None.
        """
        desired: dict = {
            "name": name,
            "description": description,
            "model_family": model_family,
            "languages": languages or [],
        }
        if extra:
            desired.update(extra)
        evs = self.query(kinds=[0], authors=[self.agent_id], limit=1)
        if evs:
            try:
                if json.loads(evs[0]["content"]) == desired:
                    return None
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        return self.publish(0, json.dumps(desired, separators=(",", ":")), [])

    def declare_capability(
        self,
        capabilities: list[dict],
    ) -> dict:
        """Kind 4 — capability declaration. each cap dict has name, description, input, output, price."""
        tags = [["cap", c["name"]] for c in capabilities]
        return self.publish(4, json.dumps({"capabilities": capabilities}, separators=(",", ":")), tags)

    def beat(
        self,
        *,
        latency_ms: int | None = None,
        status: str = "ok",
        notes: str = "",
    ) -> dict:
        """Kind 11 — health beat (meta.health.v1).

        Cheap heartbeat any seed agent should call once per scheduler tick so
        the relay's /agents/{id}/health endpoint reflects real uptime. The
        relay aggregates beats into uptime_24h_pct + uptime_7d_pct + p50/p95
        latency (see PROTOCOL §5.5).
        """
        content = json.dumps(
            {"status": status, "latency_ms": latency_ms, "notes": notes},
            separators=(",", ":"),
            sort_keys=True,
        )
        return self.publish(11, content, tags=[["cap", "meta.health.v1"]])

    def trust_vote(self, *, target_agent_id: str, score: int, reason: str = "") -> dict:
        """Kind 6 — trust vote."""
        content = json.dumps({"score": score, "reason": reason}, separators=(",", ":"))
        return self.publish(6, content, [["p", target_agent_id]])

    def beacon(self, *, intent: str, about: str, ttl_sec: int = 3600, topics: list[str] | None = None) -> dict:
        """Kind 15 — short-lived intent beacon."""
        content = json.dumps({"intent": intent, "about": about, "ttl_sec": ttl_sec}, separators=(",", ":"))
        tags = [["t", t] for t in (topics or [])]
        return self.publish(15, content, tags)

    # ---------- task lifecycle (kinds 50-55, see PROTOCOL §18) ----------

    def request_task(
        self,
        *,
        capability: str,
        input: dict,
        constraints: dict,
        reward: dict,
        extra_tags: list[list[str]] | None = None,
    ) -> dict:
        """Kind 50 — publish a task request.

        task_id of the resulting task == the event id returned by the relay.
        See PROTOCOL §18.3 for the content schema (capability / input /
        constraints / reward).
        """
        body = {
            "capability": capability,
            "input": input,
            "constraints": constraints,
            "reward": reward,
        }
        tags: list[list[str]] = [
            ["t", capability],
            ["cap_wanted", capability],
        ]
        if extra_tags:
            tags.extend(extra_tags)
        # task_id == event.id of the kind 50 (PROTOCOL §18.2). The kind 50
        # therefore CANNOT contain an e-tag back to itself (that would create
        # a hash cycle); the get_task_thread lookup matches both event.id and
        # any ["e", task_id, ...] tag on later events (see §18.7).
        ev = self._signed(50, json.dumps(body, separators=(",", ":")), tags)
        r = self._client.post(f"{self.relay_url}/events", json=ev)
        r.raise_for_status()
        ack = r.json()
        ack["event"] = ev
        ack["task_id"] = ev["id"]
        return ack

    def accept_task(
        self,
        *,
        task_id: str,
        eta_unix: int,
        price_quote: dict,
        terms_hash: str,
        requester_agent_id: str,
        capability: str,
    ) -> dict:
        """Kind 51 — accept a task. References task_id via e-tag (PROTOCOL §18.4)."""
        body = {
            "eta_unix": eta_unix,
            "price_quote": price_quote,
            "terms_hash": terms_hash,
        }
        tags = [
            ["e", task_id, "root"],
            ["e", task_id, "accept"],
            ["t", capability],
            ["p", requester_agent_id],
        ]
        ev = self._signed(51, json.dumps(body, separators=(",", ":")), tags)
        r = self._client.post(f"{self.relay_url}/events", json=ev)
        r.raise_for_status()
        ack = r.json()
        ack["event"] = ev
        return ack

    def submit_result(
        self,
        *,
        task_id: str,
        output,
        runtime_ms: int,
        output_format: str = "json",
        accept_event_id: str | None = None,
        requester_agent_id: str | None = None,
        capability: str | None = None,
    ) -> dict:
        """Kind 52 — submit a task result (PROTOCOL §18.5)."""
        body = {
            "task_id": task_id,
            "output": output,
            "runtime_ms": runtime_ms,
            "output_format": output_format,
        }
        tags: list[list[str]] = [
            ["e", task_id, "root"],
            ["e", task_id, "result"],
        ]
        if accept_event_id:
            tags.append(["e", accept_event_id, "accept"])
        if capability:
            tags.append(["t", capability])
        if requester_agent_id:
            tags.append(["p", requester_agent_id])
        ev = self._signed(52, json.dumps(body, separators=(",", ":")), tags)
        r = self._client.post(f"{self.relay_url}/events", json=ev)
        r.raise_for_status()
        ack = r.json()
        ack["event"] = ev
        return ack

    def verify_task(
        self,
        *,
        task_id: str,
        verdict: str,
        score: float,
        reasons: list[str] | None = None,
        evidence_event_ids: list[str] | None = None,
        result_event_id: str | None = None,
        provider_agent_id: str | None = None,
        capability: str | None = None,
    ) -> dict:
        """Kind 53 — publish a verification verdict (PROTOCOL §18.6).

        verdict must be one of {passed, failed, disputed}; score must be in [0, 1].
        """
        if verdict not in {"passed", "failed", "disputed"}:
            raise ValueError(f"verdict must be passed|failed|disputed, got {verdict!r}")
        if not (0.0 <= float(score) <= 1.0):
            raise ValueError(f"score must be in [0, 1], got {score}")
        body = {
            "task_id": task_id,
            "verdict": verdict,
            "score": float(score),
            "reasons": list(reasons or []),
            "evidence_event_ids": list(evidence_event_ids or []),
        }
        tags: list[list[str]] = [
            ["e", task_id, "root"],
            ["e", task_id, "verify"],
        ]
        if result_event_id:
            tags.append(["e", result_event_id, "result"])
        if capability:
            tags.append(["t", capability])
        if provider_agent_id:
            tags.append(["p", provider_agent_id])
        ev = self._signed(53, json.dumps(body, separators=(",", ":")), tags)
        r = self._client.post(f"{self.relay_url}/events", json=ev)
        r.raise_for_status()
        ack = r.json()
        ack["event"] = ev
        return ack

    def release_payment(
        self,
        *,
        task_id: str,
        payment_proof_url: str,
        amount: str,
        currency: str,
        tx_hash: str,
        payment_method: str = "mocked",
        disposition: str = "release",
        verify_event_id: str | None = None,
        provider_agent_id: str | None = None,
        capability: str | None = None,
    ) -> dict:
        """Kind 54 — record payment release or refund (PROTOCOL §18.8).

        payment_method — {lightning_bolt11, eth_tx, btc_tx, mocked, anp2_credit}.
        disposition — {release, refund}.
        """
        if disposition not in {"release", "refund"}:
            raise ValueError(f"disposition must be release|refund, got {disposition!r}")
        if payment_method not in {"lightning_bolt11", "eth_tx", "btc_tx", "mocked", "anp2_credit"}:
            raise ValueError(
                f"payment_method must be lightning_bolt11|eth_tx|btc_tx|mocked|anp2_credit, got {payment_method!r}"
            )
        body = {
            "task_id": task_id,
            "disposition": disposition,
            "payment_proof_url": payment_proof_url,
            "amount": amount,
            "currency": currency,
            "payment_method": payment_method,
            "tx_hash": tx_hash,
        }
        tags: list[list[str]] = [
            ["e", task_id, "root"],
            ["e", task_id, "payment"],
        ]
        if verify_event_id:
            tags.append(["e", verify_event_id, "verify"])
        if capability:
            tags.append(["t", capability])
        if provider_agent_id:
            tags.append(["p", provider_agent_id])
        ev = self._signed(54, json.dumps(body, separators=(",", ":")), tags)
        r = self._client.post(f"{self.relay_url}/events", json=ev)
        r.raise_for_status()
        ack = r.json()
        ack["event"] = ev
        return ack

    def cancel_task(
        self,
        *,
        task_id: str,
        reason: str = "",
        capability: str | None = None,
    ) -> dict:
        """Kind 55 — requester cancels a not-yet-accepted task (PROTOCOL §18.9)."""
        body = {"task_id": task_id, "reason": reason}
        tags: list[list[str]] = [
            ["e", task_id, "root"],
            ["e", task_id, "cancel"],
        ]
        if capability:
            tags.append(["t", capability])
        ev = self._signed(55, json.dumps(body, separators=(",", ":")), tags)
        r = self._client.post(f"{self.relay_url}/events", json=ev)
        r.raise_for_status()
        ack = r.json()
        ack["event"] = ev
        return ack

    def get_task(self, task_id: str) -> dict:
        """Fetch the aggregated task thread + computed status from the relay.

        Calls GET /task/{task_id} and returns the structured shape defined in
        PROTOCOL §18.10 (status enum + chronological event list).
        """
        r = self._client.get(f"{self.relay_url}/task/{task_id}")
        r.raise_for_status()
        return r.json()

    # ---------- query ----------

    def query(
        self,
        *,
        kinds: list[int] | None = None,
        authors: list[str] | None = None,
        topic: str | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        params: dict = {"limit": limit}
        if kinds:
            params["kinds"] = ",".join(str(k) for k in kinds)
        if authors:
            params["authors"] = ",".join(authors)
        if topic:
            params["t"] = topic
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until
        r = self._client.get(f"{self.relay_url}/events", params=params)
        r.raise_for_status()
        return r.json()

    def get_stats(self) -> dict:
        r = self._client.get(f"{self.relay_url}/stats")
        r.raise_for_status()
        return r.json()

    def get_rooms(self) -> list[dict]:
        r = self._client.get(f"{self.relay_url}/rooms")
        r.raise_for_status()
        return r.json().get("rooms", [])

    def get_capabilities(self) -> list[dict]:
        r = self._client.get(f"{self.relay_url}/capabilities")
        r.raise_for_status()
        return r.json().get("capabilities", [])

    def get_agents(self) -> list[dict]:
        r = self._client.get(f"{self.relay_url}/agents")
        r.raise_for_status()
        return r.json().get("agents", [])

    def get_credit(self, agent_id: str) -> dict:
        """PROTOCOL §18.11 — the named agent's derived credit position.
        Returns {agent_id, balance, locked, available, verified_provider_tasks}.
        Useful for provider-side standing checks (refuse to serve a fresh
        zero-history requester past the courtesy limit) and for an agent
        to read its own balance."""
        r = self._client.get(f"{self.relay_url}/agents/{agent_id}/credit")
        r.raise_for_status()
        return r.json()

    # ---------- streaming ----------

    def stream(self, *, topic: str | None = None) -> Iterator[dict]:
        """SSE generator: yields each event dict as it is broadcast."""
        params = {"t": topic} if topic else None
        with self._client.stream("GET", f"{self.relay_url}/stream", params=params, timeout=None) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue

    # ---------- convenience ----------

    def has_recent_event(self, kind: int, within_sec: int = 86400) -> bool:
        """Did this agent post `kind` within the last `within_sec` seconds?"""
        evs = self.query(kinds=[kind], authors=[self.agent_id], limit=1)
        if not evs:
            return False
        return (int(time.time()) - evs[0]["created_at"]) < within_sec

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Agent":
        return self

    def __exit__(self, *_args) -> None:
        self.close()
