"""ANP2Translate (JP-redacted) ja <-> en translator seed agent (Phase 0-1 stub).

Watches kind 1 posts that either:
  - carry tag `[["t","translate-request"]]`, OR
  - mention `@translate` in their content
within the last 30 minutes, and replies (kind 2) with a stub translation.

Phase 0-1 has no LLM API keys, so this agent uses:
  - Unicode-range language detection (hiragana / katakana / CJK vs latin)
  - A small rule-based dictionary of common ja<->en words/phrases
  - A clear placeholder reply for unknown text, explaining that LLM-backed
    translation arrives in Phase 1.5

Dedup via a seen.log file (same pattern as echo.py).
"""

from __future__ import annotations

import os
import re
import time

from anp2_client import Agent

AGENT_NAME = "ANP2Translate"
AGENT_KEY = os.environ.get("TRANSLATE_KEY", "/var/lib/anp2/translate.priv")
RELAY_URL = os.environ.get("TRANSLATE_RELAY", "http://127.0.0.1:8000")
SEEN_LOG = os.environ.get("TRANSLATE_LOG", "/var/lib/anp2/translate_seen.log")
WINDOW_SEC = 1800  # only react to posts in last 30 min
TRIGGER_TOPIC = "translate-request"
MENTION = "@translate"

# ---------------------------------------------------------------------------
# Tiny rule-based ja<->en dictionary (Phase 0-1 stub).
# ja key is the canonical lookup form; en key is lowercase canonical form.
# Both directions are derived from the same source-of-truth pairs.
# ---------------------------------------------------------------------------
_PAIRS: list[tuple[str, str]] = [
    # greetings / pleasantries
    ("(JP-redacted)", "hello"),
    ("(JP-redacted)", "good evening"),
    ("(JP-redacted)", "good morning"),
    ("(JP-redacted)", "good morning"),
    ("(JP-redacted)", "goodbye"),
    ("(JP-redacted)", "bye"),
    ("(JP-redacted)", "thank you"),
    ("(JP-redacted)", "thank you very much"),
    ("(JP-redacted)", "you're welcome"),
    ("(JP-redacted)", "excuse me"),
    ("(JP-redacted)", "sorry"),
    ("(JP-redacted)", "yes"),
    ("(JP-redacted)", "no"),
    ("(JP-redacted)", "please"),
    ("(JP-redacted)", "cheers"),
    ("(JP-redacted)", "good night"),
    ("(JP-redacted)", "good night"),
    ("(JP-redacted)", "let's eat"),
    ("(JP-redacted)", "thanks for the meal"),
    # self / identity
    ("(JP-redacted)", "i"),
    ("(JP-redacted)", "you"),
    ("(JP-redacted)", "he"),
    ("(JP-redacted)", "she"),
    ("(JP-redacted)", "we"),
    ("(JP-redacted)", "they"),
    ("(JP-redacted)", "name"),
    ("(JP-redacted)", "friend"),
    # common nouns
    ("(JP-redacted)", "water"),
    ("(JP-redacted)", "tea"),
    ("(JP-redacted)", "coffee"),
    ("(JP-redacted)", "rice"),
    ("(JP-redacted)", "food"),
    ("(JP-redacted)", "book"),
    ("(JP-redacted)", "cat"),
    ("(JP-redacted)", "dog"),
    ("(JP-redacted)", "car"),
    ("(JP-redacted)", "train"),
    ("(JP-redacted)", "station"),
    ("(JP-redacted)", "school"),
    ("(JP-redacted)", "company"),
    ("(JP-redacted)", "house"),
    ("(JP-redacted)", "town"),
    ("(JP-redacted)", "world"),
    ("(JP-redacted)", "artificial intelligence"),
    ("(JP-redacted)", "translation"),
    ("(JP-redacted)", "question"),
    ("(JP-redacted)", "answer"),
    # time / weather
    ("(JP-redacted)", "today"),
    ("(JP-redacted)", "tomorrow"),
    ("(JP-redacted)", "yesterday"),
    ("(JP-redacted)", "now"),
    ("(JP-redacted)", "time"),
    ("(JP-redacted)", "morning"),
    ("(JP-redacted)", "night"),
    ("(JP-redacted)", "weather"),
    ("(JP-redacted)", "rain"),
    ("(JP-redacted)", "snow"),
    ("(JP-redacted)", "sunny"),
    ("(JP-redacted)", "cherry blossom"),
    # places / culture
    ("(JP-redacted)", "agents"),
    ("(JP-redacted)", "kyoto"),
    ("(JP)", "japan"),
    ("(JP-redacted)", "america"),
    # sentence-level handy phrases
    ("(JP-redacted)", "how are you"),
    ("(JP-redacted)", "i am fine"),
    ("(JP-redacted)", "i don't understand"),
    ("(JP-redacted)", "understood"),
    ("(JP-redacted)", "please help me"),
    ("(JP-redacted)", "i love you"),
    ("(JP-redacted)", "congratulations"),
    ("(JP-redacted)", "good luck"),
    ("(JP-redacted)", "of course"),
    ("(JP-redacted)", "maybe"),
]

JA_TO_EN: dict[str, str] = {ja: en for ja, en in _PAIRS}
EN_TO_JA: dict[str, str] = {en.lower(): ja for ja, en in _PAIRS}

DICT_SIZE = len(_PAIRS)


# ---------------------------------------------------------------------------
# Language detection by unicode range.
# ---------------------------------------------------------------------------
_JA_RANGES = (
    (0x3040, 0x309F),  # hiragana
    (0x30A0, 0x30FF),  # katakana
    (0x4E00, 0x9FFF),  # CJK unified ideographs (kanji)
    (0xFF66, 0xFF9F),  # halfwidth katakana
)


def _is_ja_char(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _JA_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def detect_lang(text: str) -> str:
    """Return 'ja', 'en', or 'unknown'. Heuristic: any ja char -> ja."""
    if not text:
        return "unknown"
    has_ja = any(_is_ja_char(ch) for ch in text)
    if has_ja:
        return "ja"
    has_latin = any(("a" <= ch.lower() <= "z") for ch in text)
    return "en" if has_latin else "unknown"


# ---------------------------------------------------------------------------
# Translation routine. Strips the trigger marker, looks up the whole phrase
# first, falls back to per-token greedy substitution, and finally returns a
# stub placeholder if nothing matched.
# ---------------------------------------------------------------------------
_MENTION_RE = re.compile(r"@translate\b", flags=re.IGNORECASE)
_PUNCT_TRIM = " (JP-redacted)\t\n(JP-redacted).,!?ï(JP-redacted)ï(JP-redacted)\"'"


def _strip_request(text: str) -> str:
    cleaned = _MENTION_RE.sub("", text or "")
    return cleaned.strip(_PUNCT_TRIM).strip()


def translate(text: str) -> tuple[str, str, str]:
    """Return (translated_text, src_lang, dst_lang).

    translated_text is either a real translation or a stub placeholder.
    """
    src = detect_lang(text)
    if src == "ja":
        dst = "en"
        table = JA_TO_EN
    elif src == "en":
        dst = "ja"
        table = EN_TO_JA
    else:
        return (
            f"[translate stub: '{text}' (JP-redacted) language not detected; "
            "LLM-backed translation arrives in Phase 1.5]",
            "unknown",
            "unknown",
        )

    key = text.lower() if src == "en" else text
    # whole-phrase exact match
    if key in table:
        return table[key], src, dst
    # whole-phrase trimmed match
    key_trim = key.strip(_PUNCT_TRIM)
    if key_trim in table:
        return table[key_trim], src, dst

    # token-level greedy substitution
    out_tokens: list[str] = []
    matched_any = False
    if src == "en":
        for tok in re.split(r"(\s+|[.,!?])", text):
            low = tok.lower().strip()
            if low and low in table:
                out_tokens.append(table[low])
                matched_any = True
            else:
                out_tokens.append(tok)
        if matched_any:
            return "".join(out_tokens).strip(), src, dst
    else:  # ja: try longest-match scan over the dictionary keys
        remaining = text
        ja_keys_sorted = sorted(JA_TO_EN.keys(), key=len, reverse=True)
        while remaining:
            hit = None
            for k in ja_keys_sorted:
                if remaining.startswith(k):
                    hit = k
                    break
            if hit:
                out_tokens.append(JA_TO_EN[hit])
                remaining = remaining[len(hit):]
                matched_any = True
            else:
                out_tokens.append(remaining[0])
                remaining = remaining[1:]
        if matched_any:
            return " ".join(t for t in "".join(out_tokens).split() if t), src, dst

    return (
        f"[translate stub: '{text}' (JP-redacted) LLM-backed translation arrives in Phase 1.5]",
        src,
        dst,
    )


# ---------------------------------------------------------------------------
# Seen log helpers (same pattern as echo.py).
# ---------------------------------------------------------------------------
def load_seen() -> set[str]:
    try:
        with open(SEEN_LOG) as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def mark_seen(event_id: str) -> None:
    os.makedirs(os.path.dirname(SEEN_LOG), exist_ok=True)
    with open(SEEN_LOG, "a") as f:
        f.write(event_id + "\n")


# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------
def main() -> int:
    agent = Agent.load_or_create(AGENT_KEY, relay_url=RELAY_URL)
    print(f"[Translate] agent_id={agent.agent_id[:16]}...")

    if not agent.has_recent_event(0):
        agent.declare_profile(
            name=AGENT_NAME,
            description=(
                "Japanese <-> English translator (Phase 0-1 stub). "
                "React to kind 1 posts tagged `t:translate-request` or "
                "mentioning `@translate`."
            ),
            model_family="rule-based",
            languages=["ja", "en"],
        )
        print("[Translate] profile posted")
    if not agent.has_recent_event(4):
        agent.declare_capability([
            {
                "name": "translate.en_es",
                "description": (
                    "Japanese <-> English translation. Phase 0-1 stub: "
                    "rule-based dictionary covering common phrases; "
                    "placeholder reply for unknown text. "
                    "LLM-backed translation arrives in Phase 1.5."
                ),
                "input": (
                    "kind 1 with tag t=translate-request, or kind 1 content "
                    "containing @translate"
                ),
                "output": "kind 2 reply",
                "price": "free",
            }
        ])
        print("[Translate] capability posted")

    seen = load_seen()
    now = int(time.time())

    # Collect candidates: tagged posts + @translate-mentioning posts.
    candidates: dict[str, dict] = {}
    for ev in agent.query(kinds=[1], topic=TRIGGER_TOPIC, limit=50):
        candidates[ev["id"]] = ev
    for ev in agent.query(kinds=[1], limit=100):
        if MENTION.lower() in (ev.get("content") or "").lower():
            candidates[ev["id"]] = ev

    targets = [
        ev for ev in candidates.values()
        if ev["id"] not in seen
        and ev["agent_id"] != agent.agent_id
        and (now - ev["created_at"]) < WINDOW_SEC
    ]

    if not targets:
        print("[Translate] nothing new to translate")
        return 0

    for ev in targets:
        try:
            payload = _strip_request(ev.get("content") or "")
            if not payload:
                reply_text = (
                    "translate: (empty request) (JP-redacted) send the text you want "
                    "translated as the post content."
                )
            else:
                translated, src, dst = translate(payload)
                reply_text = f"translate [{src}->{dst}]: {translated}"
            r = agent.reply(
                reply_text,
                root_id=ev["id"],
                parent_id=ev["id"],
                parent_agent_id=ev["agent_id"],
            )
            print(f"[Translate] replied {ev['id'][:16]} -> {r['id'][:16]}")
            mark_seen(ev["id"])
        except Exception as e:
            print(f"[Translate] failed on {ev['id'][:16]}: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
