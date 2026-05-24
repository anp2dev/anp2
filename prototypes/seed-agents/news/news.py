"""ANP2NewsSummarizer — periodic public news snapshot seed agent.

Every 60 min:
  1. Fetch top items from several public RSS feeds (BBC World, Hacker News,
     TechCrunch, arXiv cs.AI) using stdlib urllib + xml.etree.ElementTree.
  2. Publish a kind 1 human-readable digest line covering the top 3 headlines
     across sources to room `t:news`.
  3. Publish a kind 5 (knowledge_claim) with the structured items list
     (`{title, source, link, published}`) for downstream AI consumers.

Headline dedup — every digest leads with something new:
  The digest used to repeat its lead headline whenever a feed's top item had
  not changed between hourly runs. We now persist a small recent-set of
  already-posted headline keys (NEWS_SEEN, default
  /var/lib/anp2/news_seen.json) and skip items already covered. Each
  digest therefore leads with a headline the network has not seen recently.
  The recent-set is capped (SEEN_CAP) and trimmed oldest-first so it can
  never grow unbounded.

Robustness:
  - Stdlib only (urllib.request + xml.etree.ElementTree). No feedparser/requests.
  - 10s timeout per feed; one failed feed never blocks others.
  - Total runtime budget ~30s across all feeds.
  - If ALL feeds fail, posts a kind 1 "news snapshot unavailable" status and
     never crashes.
  - If every fetched item is already in the recent-set, the digest falls back
     to the freshest items rather than posting nothing.
  - Profile + capability posted idempotently via has_recent_event.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from anp2_client import Agent


AGENT_NAME = "ANP2NewsSummarizer"
AGENT_KEY = os.environ.get("NEWS_KEY", "/var/lib/anp2/news.priv")
RELAY_URL = os.environ.get("NEWS_RELAY", "http://127.0.0.1:8000")

HTTP_TIMEOUT = 10.0
TOTAL_BUDGET_SEC = 30.0
USER_AGENT = "ANP2-NewsSummarizer/0.1 (https://anp2.com)"

# Persisted recent-set of already-posted headlines, so consecutive hourly
# digests do not repeat the same lead item.
SEEN_PATH = os.environ.get("NEWS_SEEN", "/var/lib/anp2/news_seen.json")
# Cap on remembered headline keys. ~3 digests' worth of headroom beyond the
# items one run can surface; trimmed oldest-first so it never grows unbounded.
SEEN_CAP = 60

# (source_label, feed_url). Reuters tech often 404s, so we fall back to
# TechCrunch — listed here directly to keep one fetch per source.
FEEDS: list[tuple[str, str]] = [
    ("BBC", "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("HN", "https://hnrss.org/frontpage"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("arXiv", "http://export.arxiv.org/rss/cs.AI"),
]

# How many items to keep per feed for the structured claim.
ITEMS_PER_FEED = 5
# How many headlines to include in the human-readable digest.
DIGEST_HEADLINES = 3


def fetch_feed(url: str, deadline: float) -> tuple[bytes | None, str | None]:
    """Fetch one feed. Returns (raw_bytes, None) on success, (None, reason) on failure."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "budget exhausted"
    timeout = min(HTTP_TIMEOUT, remaining)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            return resp.read(), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"URL error: {e.reason}"
    except (TimeoutError, OSError) as e:
        return None, f"network error: {e}"


def _text(elem: ET.Element | None) -> str:
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


def _findtext_ns(elem: ET.Element, *names: str) -> str:
    """Find first child by local-name (namespace-insensitive)."""
    for child in elem:
        tag = child.tag.rsplit("}", 1)[-1]  # strip namespace
        if tag in names:
            return (child.text or "").strip()
    return ""


def parse_feed(raw: bytes, source: str) -> list[dict]:
    """Parse an RSS 2.0 or Atom feed into a list of item dicts.

    Each item: {title, source, link, published}. Returns up to ITEMS_PER_FEED.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    items: list[dict] = []

    # RSS 2.0: <rss><channel><item>...
    # Also RDF/RSS 1.0: <rdf:RDF><item>...
    for it in root.iter():
        tag = it.tag.rsplit("}", 1)[-1]
        if tag != "item":
            continue
        title = _findtext_ns(it, "title")
        link = _findtext_ns(it, "link")
        published = _findtext_ns(it, "pubDate", "date", "published", "updated")
        if title or link:
            items.append({
                "title": title,
                "source": source,
                "link": link,
                "published": published,
            })
        if len(items) >= ITEMS_PER_FEED:
            return items

    if items:
        return items

    # Atom: <feed><entry>...
    for it in root.iter():
        tag = it.tag.rsplit("}", 1)[-1]
        if tag != "entry":
            continue
        title = _findtext_ns(it, "title")
        # Atom link is often <link href="..."/>
        link = ""
        for child in it:
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag == "link":
                link = child.attrib.get("href") or (child.text or "").strip()
                if link:
                    break
        published = _findtext_ns(it, "published", "updated", "date")
        if title or link:
            items.append({
                "title": title,
                "source": source,
                "link": link,
                "published": published,
            })
        if len(items) >= ITEMS_PER_FEED:
            break

    return items


def truncate(s: str, n: int = 90) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "—"


def headline_key(item: dict) -> str:
    """Stable identity for a headline, used for cross-run dedup.

    Prefer the canonical `link` (most stable across feed re-orderings); fall
    back to a normalised source+title. Whitespace- and case-normalised so a
    feed re-emitting the same story with cosmetic changes still matches.
    """
    link = " ".join((item.get("link") or "").split()).strip().lower()
    if link:
        return "l:" + link
    title = " ".join((item.get("title") or "").split()).strip().lower()
    source = (item.get("source") or "").strip().lower()
    return f"t:{source}|{title}"


def load_seen() -> list[str]:
    """Load the persisted recent-set of posted headline keys (oldest first)."""
    try:
        with open(SEEN_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict):  # tolerate {"seen": [...]} shape
        data = data.get("seen", [])
    if not isinstance(data, list):
        return []
    return [k for k in data if isinstance(k, str) and k]


def save_seen(keys: list[str]) -> None:
    """Persist the recent-set, trimmed oldest-first to SEEN_CAP entries."""
    trimmed = keys[-SEEN_CAP:]
    os.makedirs(os.path.dirname(SEEN_PATH) or ".", exist_ok=True)
    tmp = SEEN_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"seen": trimmed, "updated_at": int(time.time())}, f)
    os.replace(tmp, SEEN_PATH)


def select_digest_items(all_items: list[dict], seen: set[str]) -> list[dict]:
    """Pick up to DIGEST_HEADLINES items, preferring source diversity and
    skipping headlines already posted in a recent digest.

    Two passes prefer fresh (unseen) items; if fewer than DIGEST_HEADLINES
    fresh items exist, already-seen items top up the digest so it is never
    empty — but fresh items always come first, so the digest still *leads*
    with something new.
    """
    picks: list[dict] = []
    seen_sources: set[str] = set()

    def _add(item: dict) -> None:
        picks.append(item)
        seen_sources.add(item["source"])

    # Pass 1: fresh items, one per source for diversity.
    for it in all_items:
        if len(picks) >= DIGEST_HEADLINES:
            break
        if headline_key(it) in seen:
            continue
        if it["source"] in seen_sources:
            continue
        _add(it)
    # Pass 2: fresh items, allow repeated sources.
    for it in all_items:
        if len(picks) >= DIGEST_HEADLINES:
            break
        if headline_key(it) in seen or it in picks:
            continue
        _add(it)
    # Pass 3: fallback — everything is already seen. Top up with the freshest
    # items so the digest is never empty (lead is still as fresh as available).
    for it in all_items:
        if len(picks) >= DIGEST_HEADLINES:
            break
        if it in picks:
            continue
        _add(it)
    return picks


def build_summary(picks: list[dict]) -> str:
    """Human-readable kind 1 digest line from pre-selected headline items."""
    if not picks:
        return "ANP2 news snapshot: no items."
    parts = [f"[{p['source']}] {truncate(p['title'], 80)}" for p in picks]
    return "ANP2 news snapshot: " + " / ".join(parts)


def build_knowledge_claim(
    all_items: list[dict],
    per_feed_status: dict[str, dict],
    accessed_at_iso: str,
) -> dict:
    sources: list[dict] = []
    for label, url in FEEDS:
        sources.append({"url": url, "accessed_at": accessed_at_iso, "label": label})
    claim_text = (
        "Top headlines aggregated from public RSS feeds "
        "(BBC World, Hacker News frontpage, TechCrunch, arXiv cs.AI) as of "
        f"{accessed_at_iso}."
    )
    return {
        "claim": claim_text,
        "confidence": 0.9,
        "sources": sources,
        "data": {
            "items": all_items,
            "per_feed": per_feed_status,
        },
        "as_of": accessed_at_iso,
    }


def main() -> int:
    agent = Agent.load_or_create(AGENT_KEY, relay_url=RELAY_URL)
    print(f"[News] agent_id={agent.agent_id[:16]}...")

    if not agent.has_recent_event(0):
        agent.declare_profile(
            name=AGENT_NAME,
            description=(
                "Publishes periodic public news snapshots every 60 minutes by "
                "aggregating top headlines from public RSS feeds (BBC World, "
                "Hacker News frontpage, TechCrunch, arXiv cs.AI). Posts kind 1 "
                "human-readable digest and kind 5 structured knowledge_claim "
                "to room t:news."
            ),
            model_family="rule-based",
            languages=["en"],
        )
        print("[News] profile posted")
    if not agent.has_recent_event(4):
        agent.declare_capability([
            {
                "name": "observe.news.public_rss",
                "description": "Periodic public news snapshots from BBC/HN/Reuters/arXiv",
                "input": "none",
                "output": "kind 1 + kind 5 (knowledge_claim)",
                "price": "free",
            }
        ])
        print("[News] capability posted")

    accessed_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    deadline = time.monotonic() + TOTAL_BUDGET_SEC

    all_items: list[dict] = []
    per_feed_status: dict[str, dict] = {}
    success_count = 0

    for source, url in FEEDS:
        raw, err = fetch_feed(url, deadline)
        if err is not None or raw is None:
            per_feed_status[source] = {"url": url, "error": err or "no payload"}
            print(f"[News] {source} FAILED: {err}")
            continue
        items = parse_feed(raw, source)
        if not items:
            per_feed_status[source] = {"url": url, "error": "no items parsed"}
            print(f"[News] {source} no items parsed")
            continue
        per_feed_status[source] = {"url": url, "count": len(items)}
        all_items.extend(items)
        success_count += 1
        print(f"[News] {source} OK ({len(items)} items)")

    if success_count == 0:
        msg = (
            "ANP2 news snapshot unavailable (all feed fetches failed); "
            "will retry next interval."
        )
        r = agent.post(
            msg,
            tags=[("t", "news"), ("s", "anp.news.v1")],
        )
        print(f"[News] unavailable posted: {r['id'][:16]}...")
        return 0

    # Dedup: prefer headlines not posted in a recent digest so the lead item
    # rotates instead of repeating across consecutive hourly runs.
    seen_list = load_seen()
    seen_set = set(seen_list)
    picks = select_digest_items(all_items, seen_set)
    fresh_count = sum(1 for p in picks if headline_key(p) not in seen_set)
    print(f"[News] digest: {len(picks)} headlines selected, {fresh_count} new "
          f"(recent-set size {len(seen_list)})")

    summary = build_summary(picks)
    r1 = agent.post(
        summary,
        tags=[("t", "news"), ("s", "anp.news.v1")],
    )
    print(f"[News] summary posted: {r1['id'][:16]}... ({summary[:80]}...)")

    # Record the headlines this digest actually led with so the next run skips
    # them. Append in pick order; save_seen trims oldest-first to SEEN_CAP.
    for p in picks:
        k = headline_key(p)
        if k not in seen_set:
            seen_list.append(k)
            seen_set.add(k)
    try:
        save_seen(seen_list)
    except OSError as e:
        # A persistence hiccup must not crash the run or block the kind 5 post.
        print(f"[News] WARN could not persist recent-set: {e}")

    claim = build_knowledge_claim(all_items, per_feed_status, accessed_at_iso)
    r2 = agent.publish(
        5,
        json.dumps(claim, separators=(",", ":")),
        tags=[
            ["t", "news"],
            ["s", "anp.knowledge_claim.v1"],
        ],
    )
    print(f"[News] knowledge_claim posted: {r2['id'][:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
