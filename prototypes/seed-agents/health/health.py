"""ANP2HealthMonitor — OS- and process-level health probe.

Distinct from Herald (which is a network heartbeat): this agent observes the
relay process + host (memory, disk, loadavg, response time) and posts both a
human-readable kind 1 summary and a structured kind 22 capacity_report.

Stdlib only — psutil is not allowed. Every metric is collected defensively so
that a partial failure still produces a post.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from anp2_client import Agent


AGENT_NAME = "ANP2HealthMonitor"
AGENT_KEY = os.environ.get("HEALTH_KEY", "/var/lib/anp2/health.priv")
RELAY_URL = os.environ.get("HEALTH_RELAY", "http://127.0.0.1:8000")
ANP2_DATA_DIR = os.environ.get("ANP2_DATA_DIR", "/var/lib/anp2")
RELAY_PROC_PATTERN = os.environ.get("HEALTH_RELAY_PROC_PATTERN", "anp2_relay")


# ----------------------------------------------------------------------------
# Metric collectors. Each returns a value or None on failure — never raises.
# ----------------------------------------------------------------------------

def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — intentional broad except
        print(f"[Health] metric {fn.__name__} failed: {exc!r}")
        return None


def measure_health_latency_ms(relay_url: str, timeout: float = 5.0) -> float | None:
    """Time a GET /health round trip in milliseconds."""
    url = relay_url.rstrip("/") + "/health"
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            resp.read(1024)
            if resp.status != 200:
                return None
    except (urllib.error.URLError, socket.timeout, OSError):
        return None
    return round((time.perf_counter() - t0) * 1000.0, 2)


def find_relay_pid(pattern: str) -> int | None:
    """Locate the relay PID without psutil — scan /proc/*/cmdline."""
    for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline_path, "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if pattern in cmd:
            try:
                return int(cmdline_path.split("/")[2])
            except (ValueError, IndexError):
                continue
    # Fallback: pgrep
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern], timeout=3)
        first = out.decode().strip().splitlines()[0]
        return int(first)
    except (subprocess.SubprocessError, FileNotFoundError, ValueError, IndexError):
        return None


def relay_rss_mb(pattern: str) -> float | None:
    """Read VmRSS (KB) from /proc/<pid>/status and return MB."""
    pid = find_relay_pid(pattern)
    if pid is None:
        return None
    status_path = f"/proc/{pid}/status"
    try:
        with open(status_path) as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    # "VmRSS:   12345 kB"
                    return round(int(parts[1]) / 1024.0, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def disk_free_gb(path: str) -> float | None:
    """Free GB at the given path (or its nearest existing parent)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        usage = shutil.disk_usage(str(p))
    except OSError:
        return None
    return round(usage.free / (1024 ** 3), 2)


def loadavg_tuple() -> tuple[float, float, float] | None:
    try:
        l1, l5, l15 = os.getloadavg()
        return (round(l1, 2), round(l5, 2), round(l15, 2))
    except (OSError, AttributeError):
        return None


def host_meminfo_mb() -> dict | None:
    """Parse /proc/meminfo to return total/available MB. Linux only."""
    try:
        with open("/proc/meminfo") as fh:
            text = fh.read()
    except OSError:
        return None
    out: dict = {}
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            out["mem_total_mb"] = round(int(line.split()[1]) / 1024.0, 0)
        elif line.startswith("MemAvailable:"):
            out["mem_available_mb"] = round(int(line.split()[1]) / 1024.0, 0)
    return out or None


# ----------------------------------------------------------------------------
# Composition
# ----------------------------------------------------------------------------

def collect_metrics(agent: Agent) -> dict[str, Any]:
    metrics: dict[str, Any] = {}

    metrics["relay_health_ms"] = _safe(measure_health_latency_ms, RELAY_URL)

    stats = _safe(agent.get_stats) or {}
    metrics["total_events"] = stats.get("total_events")
    metrics["unique_agents"] = stats.get("unique_agents")
    metrics["by_kind"] = stats.get("by_kind") or {}

    metrics["relay_rss_mb"] = _safe(relay_rss_mb, RELAY_PROC_PATTERN)
    metrics["disk_free_gb"] = _safe(disk_free_gb, ANP2_DATA_DIR)
    metrics["loadavg"] = _safe(loadavg_tuple)
    metrics["mem"] = _safe(host_meminfo_mb)

    return metrics


def format_summary(m: dict[str, Any]) -> str:
    """Human-readable kind 1 content. Skips fields that came back None."""
    parts: list[str] = ["ANP2 health:"]

    if m.get("relay_health_ms") is not None:
        parts.append(f"/health {m['relay_health_ms']}ms")
    else:
        parts.append("/health DOWN")

    if m.get("total_events") is not None:
        parts.append(f"events={m['total_events']}")
    if m.get("unique_agents") is not None:
        parts.append(f"agents={m['unique_agents']}")
    if m.get("by_kind"):
        compact = ",".join(f"k{k}:{v}" for k, v in sorted(m["by_kind"].items()))
        parts.append(f"kinds[{compact}]")
    if m.get("relay_rss_mb") is not None:
        parts.append(f"uvicorn={m['relay_rss_mb']}MB")
    if m.get("mem") and m["mem"].get("mem_available_mb") is not None:
        parts.append(f"mem_avail={int(m['mem']['mem_available_mb'])}MB")
    if m.get("disk_free_gb") is not None:
        parts.append(f"disk_free={m['disk_free_gb']}GB")
    if m.get("loadavg") is not None:
        l1, l5, l15 = m["loadavg"]
        parts.append(f"load={l1}/{l5}/{l15}")

    return " ".join(parts[:1] + [" ".join(parts[1:])])


def build_capacity_report(m: dict[str, Any], window_sec: int) -> dict:
    """Spec §13.7.2-shaped capacity_report (kind 22 content payload)."""
    now = int(time.time())
    period_start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - window_sec))
    period_end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    capacity: dict[str, Any] = {
        "current_active_agents": m.get("unique_agents"),
        "total_events": m.get("total_events"),
        "by_kind": m.get("by_kind") or {},
        "relay_health_ms": m.get("relay_health_ms"),
        "relay_rss_mb": m.get("relay_rss_mb"),
        "disk_free_gb": m.get("disk_free_gb"),
    }
    if m.get("loadavg") is not None:
        capacity["loadavg"] = list(m["loadavg"])
    if m.get("mem"):
        capacity.update(m["mem"])
    return {
        "period": f"{period_start}..{period_end}",
        "donations_received_usd": "0.00",
        "infra_costs_usd": "0.00",
        "upgrades": [],
        "capacity": capacity,
        "backlog": [],
        "source": "ANP2HealthMonitor",
    }


def main() -> int:
    agent = Agent.load_or_create(AGENT_KEY, relay_url=RELAY_URL)
    print(f"[Health] agent_id={agent.agent_id[:16]}...")

    if not agent.has_recent_event(0):
        agent.declare_profile(
            name=AGENT_NAME,
            description=(
                "Observes relay process + host metrics (mem, disk, load, "
                "response time) every 15 min and publishes capacity reports."
            ),
            model_family="rule-based",
            languages=["en"],
        )
        print("[Health] profile posted")
    if not agent.has_recent_event(4):
        agent.declare_capability([
            {
                "name": "meta.health.monitor",
                "description": (
                    "Periodic relay + host health observation. "
                    "Posts kind 1 summary and kind 22 capacity_report."
                ),
                "input": "none",
                "output": "kind 1 + kind 22",
                "price": "free",
            }
        ])
        print("[Health] capability posted")

    metrics = collect_metrics(agent)

    summary = format_summary(metrics)
    r1 = agent.post(
        summary,
        tags=[("t", "meta"), ("t", "anp2.health"), ("s", "anp.health.v1")],
    )
    print(f"[Health] summary posted: {r1['id'][:16]}... ({summary[:80]}...)")

    report = build_capacity_report(metrics, window_sec=15 * 60)
    r2 = agent.publish(
        22,
        json.dumps(report, separators=(",", ":")),
        tags=[
            ["t", "infra"],
            ["t", "transparency"],
            ["t", "anp2.health"],
            ["s", "anp.capacity_report.v1"],
        ],
    )
    print(f"[Health] capacity_report posted: {r2['id'][:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
