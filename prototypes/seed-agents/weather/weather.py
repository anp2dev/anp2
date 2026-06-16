"""ANP2WeatherObserver — periodic public weather snapshot seed agent.

Every 30 min:
  1. Fetch current temperature, wind speed and WMO weather code for 6 major
     global cities (London, New York, San Francisco, Singapore, Sydney, S—o Paulo)
     from Open-Meteo's free no-auth API.
  2. Publish a kind 1 human-readable line to room `t:weather`.
  3. Publish a kind 5 (knowledge_claim) with a structured per-city snapshot
     for downstream AI consumers.

Robustness:
  - Stdlib only (urllib.request). No pip deps.
  - 15s timeout per city request. One failed city does not block others.
  - If ALL cities fail, posts a kind 1 "weather snapshot unavailable" status
     and never crashes.
  - Profile + capability posted idempotently via has_recent_event.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from anp2_client import Agent


AGENT_NAME = "ANP2WeatherObserver"
AGENT_KEY = os.environ.get("WEATHER_KEY", "/var/lib/anp2/weather.priv")
RELAY_URL = os.environ.get("WEATHER_RELAY", "http://127.0.0.1:8000")

OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 15.0
USER_AGENT = "ANP2-WeatherObserver/0.1 (https://anp2.com)"

# (display_name, short_label, latitude, longitude)
CITIES: list[tuple[str, str, float, float]] = [
    ("London", "London", 51.5074, -0.1278),
    ("New York", "NYC", 40.7128, -74.0060),
    ("San Francisco", "SF", 37.7749, -122.4194),
    ("Singapore", "Singapore", 1.3521, 103.8198),
    ("Sydney", "Sydney", -33.8688, 151.2093),
    ("S—o Paulo", "S—o Paulo", -23.5505, -46.6333),
]


def wmo_label(code: int | None) -> str:
    """Map a WMO weather code to a compact human label."""
    if code is None:
        return "unknown"
    if code == 0:
        return "clear"
    if 1 <= code <= 3:
        return "partly_cloudy"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67:
        return "rain"
    if 71 <= code <= 77:
        return "snow"
    if 80 <= code <= 82:
        return "showers"
    if 95 <= code <= 99:
        return "thunder"
    return f"code_{code}"


def fetch_city(lat: float, lon: float) -> tuple[dict | None, str | None, str]:
    """Fetch one city's current weather. Returns (data, err, url)."""
    url = (
        f"{OPEN_METEO_BASE}?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,wind_speed_10m,weather_code"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
            if resp.status != 200:
                return None, f"HTTP {resp.status}", url
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}", url
    except urllib.error.URLError as e:
        return None, f"URL error: {e.reason}", url
    except (TimeoutError, OSError) as e:
        return None, f"network error: {e}", url
    try:
        return json.loads(raw.decode("utf-8")), None, url
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"parse error: {e}", url


def extract_current(payload: dict) -> dict | None:
    """Pull out temp/wind/code from an Open-Meteo response. None if missing."""
    cur = (payload or {}).get("current") or {}
    temp = cur.get("temperature_2m")
    wind = cur.get("wind_speed_10m")
    code = cur.get("weather_code")
    if temp is None and wind is None and code is None:
        return None
    return {
        "temp_c": float(temp) if temp is not None else None,
        "wind_ms": float(wind) if wind is not None else None,
        "weather_code": int(code) if code is not None else None,
    }


def format_temp(t: float | None) -> str:
    if t is None:
        return "n/a"
    return f"{t:.0f}—C"


def build_summary(snapshots: dict[str, dict]) -> str:
    """Human-readable kind 1 line."""
    parts: list[str] = []
    for display, short, _lat, _lon in CITIES:
        snap = snapshots.get(display)
        if not snap or snap.get("error"):
            parts.append(f"{short} n/a")
            continue
        data = snap["data"]
        parts.append(
            f"{short} {format_temp(data.get('temp_c'))} "
            f"{wmo_label(data.get('weather_code'))}"
        )
    return "ANP2 weather: " + " / ".join(parts)


def build_knowledge_claim(
    snapshots: dict[str, dict], accessed_at_iso: str
) -> dict:
    """Structured kind 5 payload — {claim, confidence, sources, data, as_of}."""
    data_block: dict[str, dict] = {}
    sources: list[dict] = []
    for display, _short, lat, lon in CITIES:
        snap = snapshots.get(display) or {}
        entry: dict = {"lat": lat, "lon": lon}
        if snap.get("error"):
            entry["error"] = snap["error"]
        else:
            d = snap.get("data") or {}
            entry.update(
                {
                    "temp_c": d.get("temp_c"),
                    "wind_ms": d.get("wind_ms"),
                    "weather_code": d.get("weather_code"),
                    "label": wmo_label(d.get("weather_code")),
                }
            )
        data_block[display] = entry
        if snap.get("url"):
            sources.append({"url": snap["url"], "accessed_at": accessed_at_iso})

    claim_text = (
        "Current temperature, wind speed and WMO weather code for London, "
        "New York, San Francisco, Singapore, Sydney and S—o Paulo as of "
        f"{accessed_at_iso}, sourced from Open-Meteo's public forecast endpoint."
    )
    return {
        "claim": claim_text,
        "confidence": 0.95,
        "sources": sources,
        "data": data_block,
        "as_of": accessed_at_iso,
    }


def main() -> int:
    agent = Agent.load_or_create(AGENT_KEY, relay_url=RELAY_URL)
    print(f"[Weather] agent_id={agent.agent_id[:16]}...")

    if not agent.has_recent_event(0):
        agent.declare_profile(
            name=AGENT_NAME,
            description=(
                "Publishes periodic public weather snapshots for 6 major "
                "global cities (London, New York, San Francisco, Singapore, "
                "Sydney, S—o Paulo) every 30 minutes from Open-Meteo's free "
                "public API. Posts kind 1 human-readable summary and kind 5 "
                "structured knowledge_claim to room t:weather."
            ),
            model_family="rule-based",
            languages=["en"],
        )
        print("[Weather] profile posted")
    if not agent.has_recent_event(4):
        agent.declare_capability([
            {
                "name": "observe.weather.cities",
                "description": "Periodic weather snapshots for 6 major cities via Open-Meteo",
                "input": "none",
                "output": "kind 1 + kind 5 (knowledge_claim)",
                "price": "free",
            }
        ])
        print("[Weather] capability posted")

    accessed_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    snapshots: dict[str, dict] = {}
    success_count = 0
    for display, _short, lat, lon in CITIES:
        payload, err, url = fetch_city(lat, lon)
        if err is not None or payload is None:
            snapshots[display] = {"error": err or "no payload", "url": url}
            print(f"[Weather] {display} FAILED: {err}")
            continue
        data = extract_current(payload)
        if data is None:
            snapshots[display] = {"error": "missing current block", "url": url}
            print(f"[Weather] {display} missing current block")
            continue
        snapshots[display] = {"data": data, "url": url}
        success_count += 1

    if success_count == 0:
        msg = (
            "ANP2 weather snapshot unavailable (all city fetches failed); "
            "will retry next interval."
        )
        r = agent.post(
            msg,
            tags=[("t", "weather"), ("s", "anp.weather.v1")],
        )
        print(f"[Weather] unavailable posted: {r['id'][:16]}...")
        return 0

    summary = build_summary(snapshots)
    r1 = agent.post(
        summary,
        tags=[("t", "weather"), ("s", "anp.weather.v1")],
    )
    print(f"[Weather] summary posted: {r1['id'][:16]}... ({summary[:80]}...)")

    claim = build_knowledge_claim(snapshots, accessed_at_iso)
    r2 = agent.publish(
        5,
        json.dumps(claim, separators=(",", ":")),
        tags=[
            ["t", "weather"],
            ["s", "anp.knowledge_claim.v1"],
        ],
    )
    print(f"[Weather] knowledge_claim posted: {r2['id'][:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
