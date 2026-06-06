#!/usr/bin/env python3
"""anp2_openai_image.py — ANP2 article-image generation via the OpenAI Images API.

Operator-provided 2026-06-05 for Medium (and other) article cover/hero images.
Model: gpt-image-2-2026-04-21, quality=low (cheapest). Key lives ONLY in
internal/env/openai_image.env (gitignored) — this file contains NO secret.

Guardrails (mirrors anp2_image_gen.py so the OpenAI path is no looser):
  - PROMPT GUARD: refuses prompts matching the content-policy denylist (locale
    fingerprints, human-existence wording, legacy identifiers); appends a hard
    "no text / no words / no letters / no logos" directive so the model can't
    render stray text that would smuggle a leak into the image.
  - quality is forced to 'low' (operator directive); size defaults to the
    landscape 1536x1024 (closest OpenAI size to Medium's 1.91:1 hero card).
  - PER-RUN cap of 2 images; a spend ledger records lifetime count + last run.

Usage:
    tools/anp2_openai_image.py --prompt "abstract flat network, no text" \
        [--out internal/generated-images/medium-hero.png] [--size 1536x1024] \
        [--dry-run]

Exit: 0 ok · 2 guard/cap refusal · 1 usage/API error
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_FILE = os.path.join(REPO, "internal", "env", "openai_image.env")
LEDGER = os.path.join(REPO, "internal", "env", "openai_image_spend.json")
OUT_DIR = os.path.join(REPO, "internal", "generated-images")
ENDPOINT = "https://api.openai.com/v1/images/generations"
MODEL = "gpt-image-2-2026-04-21"
MAX_PER_RUN = 2

# gpt-image-2 token prices (USD per token), verified 2026-06 from public pricing:
# image-output $30/1M, image-input $8/1M, text-input $5/1M.
PRICE_IMG_OUT = 30.0 / 1_000_000
PRICE_IMG_IN = 8.0 / 1_000_000
PRICE_TXT_IN = 5.0 / 1_000_000
# Operator target 2026-06-05: ~$0.006 per image. low-q 1536x1024 measures ~$0.005;
# low-q 1024x1024 is the canonical $0.006 point. We WARN (can't un-spend) if an
# actual generation exceeds this, so prompt/size creep is caught.
COST_TARGET_USD = 0.006


def est_cost(usage: dict) -> float:
    """Estimate USD cost of a generation from the API usage block."""
    if not usage:
        return 0.0
    out = usage.get("output_tokens", 0) or 0
    det = usage.get("input_tokens_details", {}) or {}
    txt_in = det.get("text_tokens", usage.get("input_tokens", 0) or 0)
    img_in = det.get("image_tokens", 0) or 0
    return out * PRICE_IMG_OUT + txt_in * PRICE_TXT_IN + img_in * PRICE_IMG_IN

# --- prompt guard ------------------------------------------------------------
# Forbidden patterns load from the local policy config so the specific strings
# stay out of this source; generic fallback (no project-specific literals) if absent.
def _load_banned() -> list:
    path = os.environ.get("ANP2_CONTENT_DENYLIST") or os.path.join(
        REPO, "internal", "env", "content-denylist.json")
    try:
        pats = json.load(open(path, encoding="utf-8")).get("runtime_guard_patterns")
        if pats:
            return [(re.compile(p, re.I), "content-policy denylist") for p in pats]
    except (OSError, ValueError):
        pass
    return [(re.compile(r"-----BEGIN"), "key material"),
            (re.compile(r"github_pat_|ghp_"), "token")]


_BANNED = _load_banned()
NO_TEXT = (" Flat, minimal, abstract. Absolutely no text, no words, no letters, "
           "no numbers, no logos, no watermarks, no signatures anywhere in the image.")


def guard(prompt: str) -> None:
    for rx, why in _BANNED:
        if rx.search(prompt):
            sys.exit(f"GUARD: prompt matches {why}. Refused.")


def load_key() -> str:
    if not os.path.exists(ENV_FILE):
        sys.exit(f"ERROR: {ENV_FILE} missing (key not configured).")
    for line in open(ENV_FILE):
        line = line.strip()
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit("ERROR: OPENAI_API_KEY not found in env file.")


def record(n: int, size: str, cost: float) -> None:
    led = {"images_total": 0, "spend_usd": 0.0, "runs": []}
    if os.path.exists(LEDGER):
        try:
            led = json.load(open(LEDGER))
        except Exception:
            pass
    led["images_total"] = led.get("images_total", 0) + n
    led["spend_usd"] = round(led.get("spend_usd", 0.0) + cost, 6)
    led.setdefault("runs", []).append({"n": n, "size": size, "model": MODEL,
                                        "quality": "low", "cost_usd": round(cost, 6),
                                        "ts": int(time.time())})
    led["runs"] = led["runs"][-50:]
    json.dump(led, open(LEDGER, "w"), indent=2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "openai-image.png"))
    ap.add_argument("--size", default="1536x1024",
                    help="1024x1024 | 1536x1024 (landscape) | 1024x1536 (portrait)")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.n > MAX_PER_RUN:
        sys.exit(f"GUARD: --n {a.n} exceeds per-run cap {MAX_PER_RUN}. Refused.")
    guard(a.prompt)
    full_prompt = a.prompt.rstrip(". ") + "." + NO_TEXT

    if a.dry_run:
        print("DRY-RUN ok. Model:", MODEL, "quality: low size:", a.size)
        print("Full prompt:\n", full_prompt)
        return 0

    key = load_key()
    body = json.dumps({
        "model": MODEL, "prompt": full_prompt, "size": a.size,
        "quality": "low", "n": a.n,
    }).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"API HTTPError {e.code}: {e.read().decode()[:500]}")
    except Exception as e:
        sys.exit(f"API error: {e}")

    data = resp.get("data") or []
    if not data:
        sys.exit(f"No image returned: {json.dumps(resp)[:400]}")

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    written = []
    for i, item in enumerate(data):
        out = a.out if len(data) == 1 else re.sub(r"(\.\w+)$", f"-{i}\\1", a.out)
        if item.get("b64_json"):
            open(out, "wb").write(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            with urllib.request.urlopen(item["url"], timeout=120) as im:
                open(out, "wb").write(im.read())
        else:
            sys.exit(f"image {i}: no b64_json/url in response item.")
        written.append(out)

    usage = resp.get("usage") or {}
    cost = est_cost(usage)
    record(len(written), a.size, cost)
    print("OK wrote:", *written)
    if usage:
        print("usage:", json.dumps(usage))
    print(f"est cost: ${cost:.5f}/run (target ${COST_TARGET_USD:.3f}/image)")
    if a.n and cost / max(a.n, 1) > COST_TARGET_USD:
        print(f"WARN: ~${cost / a.n:.5f}/image exceeds ${COST_TARGET_USD:.3f} target — "
              f"use --size 1024x1024 and/or a simpler prompt to come back under.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
