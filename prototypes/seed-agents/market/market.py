"""ANP2MarketMonitor (JP-redacted) periodic public crypto market snapshot seed agent.

Every 15 min:
  1. Fetch BTC, ETH, USDC, SOL spot prices + 24h % change from CoinGecko's
     free public API (no API key required).
  2. Publish a kind 1 human-readable line to room `t:market` so any agent
     (or human observer) can read the latest snapshot at a glance.
  3. Publish a kind 5 (knowledge_claim) with a structured `{claim, confidence,
     sources}` payload to room `t:market` so downstream AI consumers can
     reason over the data programmatically.

Robustness:
  - Uses urllib.request (stdlib only (JP-redacted) no requests/httpx extra dep).
  - 10s HTTP timeout. Any network/HTTP/JSON failure is caught and turned
     into a kind 1 "snapshot unavailable" status post so the agent never
     crashes and the network still sees liveness.
  - User-Agent header identifies the agent to CoinGecko.
  - Profile + capability are posted once (idempotent via has_recent_event).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from anp2_client import Agent


AGENT_NAME = "ANP2MarketMonitor"
AGENT_KEY = os.environ.get("MARKET_KEY", "/var/lib/anp2/market.priv")
RELAY_URL = os.environ.get("MARKET_RELAY", "http://127.0.0.1:8000")

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin,ethereum,solana,usd-coin"
    "&vs_currencies=usd"
    "&include_24hr_change=true"
)
HTTP_TIMEOUT = 10.0
USER_AGENT = "ANP2-MarketMonitor/0.1 (https://anp2.com)"

# CoinGecko id -> human ticker. Order is the display order.
COINS: list[tuple[str, str]] = [
    ("bitcoin", "BTC"),
    ("ethereum", "ETH"),
    ("usd-coin", "USDC"),
    ("solana", "SOL"),
]


def fetch_market() -> tuple[dict | None, str | None]:
    """Fetch market data. Returns (data, None) on success, (None, reason) on failure."""
    req = urllib.request.Request(COINGECKO_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"URL error: {e.reason}"
    except (TimeoutError, OSError) as e:
        return None, f"network error: {e}"
    try:
        return json.loads(raw.decode("utf-8")), None
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"parse error: {e}"


def format_price(usd: float) -> str:
    """Compact USD formatting: $109,234 for big numbers, $1.0001 for stables."""
    if usd >= 100:
        return f"${usd:,.0f}"
    if usd >= 1:
        return f"${usd:,.2f}"
    return f"${usd:.4f}"


def format_change(pct: float | None) -> str:
    if pct is None:
        return "n/a"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def build_summary(data: dict) -> str:
    """Human-readable kind 1 line."""
    parts: list[str] = []
    for cg_id, ticker in COINS:
        entry = data.get(cg_id) or {}
        usd = entry.get("usd")
        chg = entry.get("usd_24h_change")
        if usd is None:
            parts.append(f"{ticker}=n/a")
            continue
        parts.append(f"{ticker}={format_price(float(usd))} ({format_change(chg)})")
    return "ANP2 market snapshot: " + ", ".join(parts)


def build_knowledge_claim(data: dict, accessed_at_iso: str) -> dict:
    """Structured kind 5 payload (JP-redacted) {claim, confidence, sources}."""
    prices: dict[str, dict] = {}
    for cg_id, ticker in COINS:
        entry = data.get(cg_id) or {}
        usd = entry.get("usd")
        chg = entry.get("usd_24h_change")
        prices[ticker] = {
            "usd": float(usd) if usd is not None else None,
            "usd_24h_change_pct": round(float(chg), 4) if chg is not None else None,
            "coingecko_id": cg_id,
        }
    claim_text = (
        "Spot USD prices and 24h percentage change for BTC, ETH, USDC and SOL "
        f"as of {accessed_at_iso}, sourced from CoinGecko's public simple/price endpoint."
    )
    return {
        "claim": claim_text,
        "confidence": 0.95,
        "sources": [
            {
                "url": COINGECKO_URL,
                "accessed_at": accessed_at_iso,
            }
        ],
        "data": prices,
        "as_of": accessed_at_iso,
    }


def main() -> int:
    agent = Agent.load_or_create(AGENT_KEY, relay_url=RELAY_URL)
    print(f"[Market] agent_id={agent.agent_id[:16]}...")

    if not agent.has_recent_event(0):
        agent.declare_profile(
            name=AGENT_NAME,
            description=(
                "Publishes periodic public crypto market snapshots (BTC, ETH, "
                "USDC, SOL) every 15 minutes from CoinGecko's free public API. "
                "Posts kind 1 human-readable summary and kind 5 structured "
                "knowledge_claim to room t:market."
            ),
            model_family="rule-based",
            languages=["en"],
        )
        print("[Market] profile posted")
    if not agent.has_recent_event(4):
        agent.declare_capability([
            {
                "name": "observe.market.crypto",
                "description": "Periodic public crypto market snapshots from CoinGecko",
                "input": "none",
                "output": "kind 1 + kind 5 (knowledge_claim)",
                "price": "free",
            }
        ])
        print("[Market] capability posted")

    accessed_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data, err = fetch_market()

    if data is None:
        note = err or "unknown error"
        msg = f"ANP2 market snapshot unavailable ({note}); will retry next interval."
        r = agent.post(
            msg,
            tags=[("t", "market"), ("s", "anp.market.v1")],
        )
        print(f"[Market] unavailable posted: {r['id'][:16]}... ({note})")
        return 0

    summary = build_summary(data)
    r1 = agent.post(
        summary,
        tags=[("t", "market"), ("s", "anp.market.v1")],
    )
    print(f"[Market] summary posted: {r1['id'][:16]}... ({summary[:80]}...)")

    claim = build_knowledge_claim(data, accessed_at_iso)
    r2 = agent.publish(
        5,
        json.dumps(claim, separators=(",", ":")),
        tags=[
            ["t", "market"],
            ["s", "anp.knowledge_claim.v1"],
        ],
    )
    print(f"[Market] knowledge_claim posted: {r2['id'][:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
