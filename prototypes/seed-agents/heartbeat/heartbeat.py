"""Heartbeat (JP-redacted) emit kind 11 beats on behalf of every seed agent.

Every seed agent's .priv key lives at /var/lib/anp2/<name>.priv. This
process loads each one, measures a simple loopback latency (relay /health),
and publishes a kind 11 beat. Single 60s systemd timer, so all seed
agents are heartbeating from one place.

This populates /agents/{id}/health (patch_003) with real uptime + p50/p95
latency, which any external crawler can use to choose counterparties.
"""
from __future__ import annotations
import glob
import json
import time
from pathlib import Path
from anp2_client import Agent
import httpx

RELAY = "http://127.0.0.1:8000"
KEY_DIR = Path("/var/lib/anp2")


def measure_latency_ms() -> int | None:
    try:
        t0 = time.perf_counter()
        r = httpx.get(f"{RELAY}/health", timeout=5)
        r.raise_for_status()
        return int((time.perf_counter() - t0) * 1000)
    except Exception:
        return None


def main() -> int:
    lat = measure_latency_ms()
    keys = sorted(KEY_DIR.glob("*.priv"))
    if not keys:
        print("[Heartbeat] no .priv keys found in", KEY_DIR)
        return 0
    posted = 0
    for k in keys:
        # Skip the seed-multisig key for liveness (key destroyed at Phase 3 per
        # Principle 8; we don't want it to look operationally identical to
        # ordinary seed agents).
        if k.stem == "founder":
            continue
        try:
            agent = Agent.load_or_create(str(k), relay_url=RELAY)
            agent.beat(latency_ms=lat, status="ok",
                       notes=f"heartbeat seed-agent={k.stem}")
            posted += 1
        except Exception as e:
            print(f"[Heartbeat] {k.stem}: {type(e).__name__}: {e}")
    print(f"[Heartbeat] posted {posted}/{len(keys)} beats (latency_ms={lat})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
