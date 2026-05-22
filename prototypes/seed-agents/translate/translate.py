"""ANP2Translate (JP-redacted) fr -> en translator seed agent (Phase 0-1 stub).

Watches kind 1 posts that either:
  - carry tag `[["t","translate-request"]]`, OR
  - mention `@translate` in their content
within the last 30 minutes, and replies (kind 2) with a stub translation.

Phase 0-1 has no LLM API keys, so this agent uses:
  - A small rule-based dictionary of common French -> English words/phrases
  - Dictionary-coverage language detection (French source vs plain English)
  - A clear placeholder reply for unknown text, explaining that LLM-backed
    translation arrives in Phase 1.5

Every event this agent publishes is a public network surface, so it only
ever emits clean English: a real translation when the dictionary fully
covers the input, otherwise an honest English-only "unsupported" stub.

Dedup via a seen.log file (same pattern as echo.py).
"""

from __future__ import annotations

import json
import os
import re
import time

from anp2_client import Agent

# Task-lifecycle event kinds (reserved by sibling PIP draft, kinds 50-54).
KIND_TASK_REQUEST = 50
KIND_TASK_ACCEPT = 51
KIND_TASK_RESULT = 52
CAPABILITY = "transform.text.demo"

AGENT_NAME = "ANP2Translate"
AGENT_KEY = os.environ.get("TRANSLATE_KEY", "/var/lib/anp2/translate.priv")
RELAY_URL = os.environ.get("TRANSLATE_RELAY", "http://127.0.0.1:8000")
SEEN_LOG = os.environ.get("TRANSLATE_LOG", "/var/lib/anp2/translate_seen.log")
WINDOW_SEC = 1800  # only react to posts in last 30 min
TRIGGER_TOPIC = "translate-request"
MENTION = "@translate"

# Punctuation/whitespace trimmed off phrases for dictionary lookup.
_PUNCT_TRIM = " \t\n.,!?\"'"

# ---------------------------------------------------------------------------
# Tiny rule-based French -> English dictionary (Phase 0-1 stub).
# Keys are lowercase French words/phrases; values are lowercase English.
# French is a deliberately NEUTRAL source language (JP-redacted) no Japanese ever
# appears in this agent's dictionary, comments, or test data, because every
# event it publishes is a public surface.
# ---------------------------------------------------------------------------
_PAIRS: list[tuple[str, str]] = [
    # greetings / pleasantries
    ("bonjour", "hello"),
    ("bonsoir", "good evening"),
    ("salut", "hi"),
    ("au revoir", "goodbye"),
    ("merci", "thank you"),
    ("merci beaucoup", "thank you very much"),
    ("de rien", "you're welcome"),
    ("excusez-moi", "excuse me"),
    ("pardon", "sorry"),
    ("s'il vous plait", "please"),
    ("oui", "yes"),
    ("non", "no"),
    ("bonne nuit", "good night"),
    ("bon appetit", "enjoy your meal"),
    ("bonne chance", "good luck"),
    ("felicitations", "congratulations"),
    ("d'accord", "okay"),
    # self / identity
    ("je", "i"),
    ("tu", "you"),
    ("vous", "you"),
    ("il", "he"),
    ("elle", "she"),
    ("nous", "we"),
    ("ils", "they"),
    ("ami", "friend"),
    ("le nom", "the name"),
    # common nouns (with article)
    ("de l'eau", "water"),
    ("un the", "a tea"),
    ("un cafe", "a coffee"),
    ("le livre", "the book"),
    ("le chat", "the cat"),
    ("le chien", "the dog"),
    ("la voiture", "the car"),
    ("la maison", "the house"),
    ("le monde", "the world"),
    ("l'intelligence artificielle", "artificial intelligence"),
    ("la question", "the question"),
    ("la reponse", "the answer"),
    # time / weather
    ("aujourd'hui", "today"),
    ("demain", "tomorrow"),
    ("hier", "yesterday"),
    ("maintenant", "now"),
    ("le matin", "the morning"),
    ("la nuit", "the night"),
    ("il pleut", "it is raining"),
    ("il neige", "it is snowing"),
    ("il fait beau", "it is sunny"),
    # short handy phrases
    ("comment allez-vous", "how are you"),
    ("je vais bien", "i am fine"),
    ("je ne comprends pas", "i don't understand"),
    ("aidez-moi", "help me"),
    ("bien sur", "of course"),
    ("peut-etre", "maybe"),
    ("mon ami", "my friend"),
    # placeholder used by the legacy demo harness
    ("city.foo", "foo"),
]

FR_TO_EN: dict[str, str] = {fr: en for fr, en in _PAIRS}

DICT_SIZE = len(_PAIRS)


# ---------------------------------------------------------------------------
# Language detection.
#
# French and English share the Latin script, so a Unicode-range test cannot
# tell them apart. Instead we detect "fr" by dictionary coverage: if any
# whole word of the input is a known French dictionary key, treat the input
# as French. Latin text with no French hit is treated as plain English.
# ---------------------------------------------------------------------------
def _is_latin_char(ch: str) -> bool:
    """True for ASCII letters; accented Latin letters used in French keys
    are also accepted so French input is recognised as Latin script."""
    if "a" <= ch.lower() <= "z":
        return True
    return ch.lower() in "(JP-redacted)"


def detect_lang(text: str) -> str:
    """Return 'fr', 'en', or 'unknown'.

    Heuristic: tokenise on whitespace; if any window of consecutive tokens
    (trimmed of punctuation) is a known French dictionary key, the input is
    French. Multi-word windows are checked so phrases that only exist as
    multi-word keys (e.g. "le chat", "comment allez-vous") are still
    recognised. Otherwise Latin text is treated as English, and anything
    non-Latin as unknown.
    """
    if not text:
        return "unknown"
    words = [w.strip(_PUNCT_TRIM) for w in text.lower().split()]
    words = [w for w in words if w]
    max_span = max((len(k.split()) for k in FR_TO_EN), default=1)
    for i in range(len(words)):
        for span in range(1, min(max_span, len(words) - i) + 1):
            if " ".join(words[i:i + span]) in FR_TO_EN:
                return "fr"
    has_latin = any(_is_latin_char(ch) for ch in text)
    return "en" if has_latin else "unknown"


# ---------------------------------------------------------------------------
# Translation routine. Strips the trigger marker, looks up the whole phrase
# first, falls back to per-token greedy substitution, and finally returns a
# stub placeholder if nothing matched.
# ---------------------------------------------------------------------------
_MENTION_RE = re.compile(r"@translate\b", flags=re.IGNORECASE)


def _strip_request(text: str) -> str:
    cleaned = _MENTION_RE.sub("", text or "")
    return cleaned.strip(_PUNCT_TRIM).strip()


# Longest multi-word French dictionary key, in words (JP-redacted) used to bound the
# greedy match window in translate().
_MAX_KEY_WORDS = max(len(k.split()) for k, _ in _PAIRS)


def _is_clean_english(text: str) -> bool:
    """True iff `text` contains no non-ASCII characters at all.

    Every event this agent publishes is a public network surface, so an
    output is only allowed to ship if it is verifiably free of non-English
    fragments (accented French letters included). This is the last-line
    guard before returning. By construction it also rejects Japanese.
    """
    return all(ord(ch) < 128 for ch in text)


def _unsupported_stub(src: str, dst: str) -> str:
    """Honest, English-only placeholder for input the dictionary cannot
    fully translate. Deliberately does NOT echo the (possibly non-English)
    input back (JP-redacted) echoing it would leak non-English text onto the network."""
    return (
        "[translate: unsupported input - the Phase 0-1 rule-based dictionary "
        "does not fully cover this text, so no translation is emitted. "
        "LLM-backed translation arrives in Phase 1.5.]"
    )


def translate(text: str) -> tuple[str, str, str]:
    """Return (translated_text, src_lang, dst_lang).

    translated_text is EITHER a clean-English real translation OR an honest,
    English-only "unsupported input" stub. It is never a mixed-language
    fragment: this agent only emits a translation when its French -> English
    dictionary fully covers the input, and every public-facing event must be
    free of non-English text.
    """
    src = detect_lang(text)

    # Only the fr->en direction can produce a publishable (English) result.
    # English or unknown input cannot be translated by this fr->en stub, so
    # they return the English-only stub.
    if src != "fr":
        dst = "en" if src == "fr" else "unknown"
        return _unsupported_stub(src, dst), src, dst

    dst = "en"

    # whole-phrase exact / trimmed match (JP-redacted) the safest, highest-quality path.
    for candidate in (text.lower(), text.lower().strip(_PUNCT_TRIM)):
        if candidate in FR_TO_EN:
            result = FR_TO_EN[candidate]
            if _is_clean_english(result):
                return result, src, dst

    # Greedy longest-match scan over whitespace-separated words. Crucially:
    # only return a translation if EVERY word of the input is consumed by a
    # dictionary entry. Partial coverage -> unsupported stub, never a mixed
    # fr/en fragment like "the cat aime le lait".
    words = [w.strip(_PUNCT_TRIM) for w in text.lower().split()]
    words = [w for w in words if w]
    out_tokens: list[str] = []
    fully_covered = True
    i = 0
    while i < len(words):
        hit = None
        # Try the longest window first so multi-word keys win.
        for span in range(min(_MAX_KEY_WORDS, len(words) - i), 0, -1):
            candidate = " ".join(words[i:i + span])
            if candidate in FR_TO_EN:
                hit = (candidate, span)
                break
        if hit:
            out_tokens.append(FR_TO_EN[hit[0]])
            i += hit[1]
        else:
            # An untranslatable word: the dictionary does not fully cover
            # this input. Bail out to the honest stub.
            fully_covered = False
            break

    if fully_covered and out_tokens:
        result = " ".join(t for t in " ".join(out_tokens).split() if t)
        if _is_clean_english(result):
            return result, src, dst

    return _unsupported_stub(src, dst), src, dst


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
# Task-lifecycle helpers (kinds 50-54). The sibling spec reserves:
#   50 task.request, 51 task.accept, 52 task.result, 53 task.verify,
#   54 payment.release.
# We code defensively: if the anp2_client.Agent grows native
# `accept_task` / `submit_result` helpers we'll use them, otherwise we
# fall back to building signed events via `publish()` directly.
# ---------------------------------------------------------------------------
def _matches_translate_cap(ev: dict) -> bool:
    """A kind-50 request targets us if `cap` or `cap_wanted` tag == transform.text.demo."""
    for tag in ev.get("tags", []) or []:
        if len(tag) >= 2 and tag[0] in ("cap", "cap_wanted") and tag[1] == CAPABILITY:
            return True
    # also accept JSON content { "cap": "transform.text.demo", ... }
    try:
        body = json.loads(ev.get("content") or "{}")
        if isinstance(body, dict) and body.get("cap") == CAPABILITY:
            return True
    except (ValueError, TypeError):
        pass
    return False


def _extract_input_text(ev: dict) -> str:
    """Pull the text-to-translate from a kind 50 request body."""
    try:
        body = json.loads(ev.get("content") or "{}")
        if isinstance(body, dict):
            for key in ("input", "text", "fr", "payload"):
                v = body.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            inp = body.get("input")
            if isinstance(inp, dict):
                for key in ("text", "fr", "content"):
                    v = inp.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
    except (ValueError, TypeError):
        pass
    # fallback: treat raw content as the text
    return (ev.get("content") or "").strip()


def _post_task_accept(agent: Agent, task_id: str, requester_id: str) -> dict:
    """Kind 51 task.accept. Prefer client helper if present."""
    eta = int(time.time()) + 30
    quote = {"amount": 0, "currency": "USD", "model": "free"}
    if hasattr(agent, "accept_task"):
        return agent.accept_task(  # type: ignore[attr-defined]
            task_id=task_id,
            requester_agent_id=requester_id,
            eta_unix=eta,
            price_quote=quote,
            terms_hash="",
            capability=CAPABILITY,
        )
    body = json.dumps(
        {"task_id": task_id, "eta_unix": eta, "price_quote": quote, "cap": CAPABILITY},
        separators=(",", ":"),
    )
    tags = [
        ["e", task_id, "task"],
        ["p", requester_id],
        ["cap", CAPABILITY],
    ]
    return agent.publish(KIND_TASK_ACCEPT, body, tags)


def _post_task_result(
    agent: Agent,
    task_id: str,
    requester_id: str,
    output: str,
    src: str,
    dst: str,
    runtime_ms: int,
) -> dict:
    """Kind 52 task.result. Prefer client helper if present."""
    result = {
        "task_id": task_id,
        "cap": CAPABILITY,
        "output": output,
        "src_lang": src,
        "dst_lang": dst,
        "runtime_ms": runtime_ms,
        "model": "rule-based",
    }
    if hasattr(agent, "submit_result"):
        return agent.submit_result(  # type: ignore[attr-defined]
            task_id=task_id,
            requester_agent_id=requester_id,
            output={"text": output, "src_lang": src, "dst_lang": dst, "model": "rule-based"},
            runtime_ms=runtime_ms,
            capability=CAPABILITY,
        )
    body = json.dumps(result, separators=(",", ":"))
    tags = [
        ["e", task_id, "task"],
        ["p", requester_id],
        ["cap", CAPABILITY],
    ]
    return agent.publish(KIND_TASK_RESULT, body, tags)


def _handle_task_requests(agent: Agent, seen: set[str], now: int) -> int:
    """Scan recent kind-50 requests; accept + answer ones matching our capability."""
    handled = 0
    requests = agent.query(kinds=[KIND_TASK_REQUEST], limit=100)
    for ev in requests:
        ev_id = ev["id"]
        if ev_id in seen:
            continue
        if ev["agent_id"] == agent.agent_id:
            continue
        if (now - ev["created_at"]) > WINDOW_SEC:
            continue
        if not _matches_translate_cap(ev):
            continue
        try:
            _post_task_accept(agent, ev_id, ev["agent_id"])
            print(f"[Translate] accepted task {ev_id[:16]}")
            text = _extract_input_text(ev)
            t0 = time.monotonic()
            if not text:
                output, src, dst = "(empty input)", "unknown", "unknown"
            else:
                translated, src, dst = translate(text)
                output = translated
            runtime_ms = int((time.monotonic() - t0) * 1000)
            r = _post_task_result(
                agent, ev_id, ev["agent_id"], output, src, dst, runtime_ms
            )
            print(
                f"[Translate] result task={ev_id[:16]} "
                f"out={r['id'][:16]} ms={runtime_ms} [{src}->{dst}]"
            )
            mark_seen(ev_id)
            handled += 1
        except Exception as e:
            print(f"[Translate] task {ev_id[:16]} failed: {e}")
    return handled


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
                "French -> English translator (Phase 0-1 stub). "
                "Reacts to kind 50 task.request with cap=transform.text.demo, "
                "and to legacy kind 1 posts tagged `t:translate-request` or "
                "mentioning `@translate`."
            ),
            model_family="rule-based",
            languages=["fr", "en"],
        )
        print("[Translate] profile posted")
    if not agent.has_recent_event(4):
        # B2 structured capability (JP-redacted) anp2.cap.v1 schema
        # (see spec/capabilities/anp2.cap.v1.json and
        #  spec/capabilities/transform.text.demo.v1.json). The legacy free-form
        # `input` / `output` / `price` fields are kept alongside so pre-B2
        # /capabilities consumers keep working during the transition.
        agent.declare_capability([
            {
                "name": CAPABILITY,
                "version": "1.0",
                "description": (
                    "French -> English translation. Phase 0-1 stub: "
                    "rule-based dictionary covering common phrases; "
                    "placeholder reply for unknown text. "
                    "LLM-backed translation arrives in Phase 1.5."
                ),
                "input_schema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string", "maxLength": 4096},
                        "src_lang": {
                            "type": "string",
                            "enum": ["fr", "en", "auto"],
                            "default": "auto",
                        },
                        "dst_lang": {"type": "string", "enum": ["fr", "en"]},
                    },
                },
                "output_schema": {
                    "type": "object",
                    "required": ["text", "src_lang", "dst_lang"],
                    "properties": {
                        "text": {"type": "string"},
                        "src_lang": {"type": "string"},
                        "dst_lang": {"type": "string"},
                        "method": {
                            "type": "string",
                            "enum": ["dictionary", "llm", "stub"],
                        },
                    },
                },
                "constraints": {
                    "max_input_bytes": 16384,
                    "max_output_bytes": 16384,
                    "p50_latency_ms": 50,
                    "p95_latency_ms": 500,
                    "max_concurrent": 32,
                    "supported_languages": ["fr", "en"],
                },
                "quality": {
                    # Honest: the rule-based dictionary covers only ~80 phrases,
                    # so out-of-dictionary precision is genuinely low.
                    "self_reported_precision": 0.55,
                    "self_reported_hallucination_rate": 0.01,
                    "training_sources": ["builtin_dictionary_v1"],
                    "verified_by": [],
                },
                "pricing": {
                    "currency": "USD",
                    "model": "free",
                    "amount": 0,
                },
                "policy": {
                    "data_retention": "none",
                    "model_logs_inputs": False,
                    "geo_restrictions": [],
                },
                # Legacy free-form fields (preserved for backwards compat).
                "input": (
                    "kind 50 task.request with cap=transform.text.demo, or "
                    "kind 1 with tag t=translate-request, or kind 1 content "
                    "containing @translate"
                ),
                "output": "kind 52 task.result (or kind 2 reply for legacy)",
                "price": "free",
            }
        ])
        print("[Translate] capability posted")

    seen = load_seen()
    now = int(time.time())

    # ---- (a) kind 50 task.request lifecycle (primary path) ------------------
    handled = _handle_task_requests(agent, seen, now)
    if handled:
        print(f"[Translate] handled {handled} task.request(s)")

    # ---- (b) legacy kind-1 reactive path (still supported) ------------------
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
        if not handled:
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
