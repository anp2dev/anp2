#!/usr/bin/env python3
"""anp2_image_gen.py — cheap, budget-capped image generation for ANP2 posts.

Generates an illustration (e.g. a dev.to article cover) via OpenRouter using
the CHEAPEST image model (openai/gpt-5-image-mini), with hard guardrails so it
can never run away on cost:

  - PER-IMAGE COST CAP (~$0.06): enforced POST-generation — if an image's cost
    (reported by OpenRouter, or ESTIMATED from token counts when not reported)
    exceeds the cap, the run stops + warns. It cannot un-spend a single over-cap
    image, so the real guard is the low-detail prompt, which keeps gpt-5-image
    -mini cost about an order of magnitude under the cap (~$0.01-0.02/image).
  - PER-RUN cap of 2 images + "use sparingly, not dozens" (a spend ledger
    tracks lifetime image count for visibility).
  - policy PROMPT GUARD: refuses prompts containing Japanese script, origin
    fingerprints, human-existence wording, or the legacy brand; appends a
    "no text / no words / no logos" directive so the model can't render stray
    text that would smuggle B/D leaks into the image.

The API key is read from internal/env/openrouter.env (gitignored, never
committed). This script contains NO secret and is safe to track.

Usage:
    tools/anp2_image_gen.py --prompt "abstract network of nodes, flat minimal" \
        [--out internal/generated-images/cover.png] [--n 1] [--dry-run]

Exit: 0 ok · 2 budget/guard refusal · 1 usage/API error
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
ENV_FILE = os.path.join(REPO, "internal", "env", "openrouter.env")
LEDGER = os.path.join(REPO, "internal", "env", "openrouter_spend.json")
OUT_DIR = os.path.join(REPO, "internal", "generated-images")
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# Conservative upper bound reserved per image for the PRE-call budget check.
# gpt-5-image-mini low-q images cost ~$0.01-0.02 in practice. Budget cap is
# tiny ($0.06), so reserve $0.02/image: a couple images fit, and we stop well
# before the cap. Actual reported cost (usage.cost) is recorded when present.
RESERVE_PER_IMAGE_USD = 0.02
MAX_PER_RUN = 2
# gpt-5-image-mini token rates (USD/token); image output is billed as
# completion tokens (no separate image price field). Used to ESTIMATE cost
# when OpenRouter doesn't return usage.cost inline, so the per-image cap always
# has a real number to check. Verify against /api/v1/models if the model changes.
PRICE_PROMPT_USD = 0.0000025
PRICE_COMPLETION_USD = 0.000002

# policy prompt guard (refuse before spending).
FORBIDDEN = [
    (r"[ぁ-んァ-ヶ一-龯]", "Japanese script (rule)"),
    (r"\bJST\b|UTC|[x]|tokyo|japan", "origin fingerprint (rule)"),
    (r"\banp2\b", "legacy brand (rule)"),
    (r"\bfounder\b|human (?:operator|maintainer|team|owner)|operated by (?:a )?(?:person|people|humans)",
     "human-existence wording (rule)"),
]


def load_env() -> dict:
    env = {}
    try:
        with open(ENV_FILE) as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        print(f"missing {ENV_FILE} — cannot generate", file=sys.stderr)
        sys.exit(1)
    return env


def load_ledger() -> dict:
    try:
        with open(LEDGER) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"total_usd": 0.0, "calls": []}


def save_ledger(led: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "w") as f:
        json.dump(led, f, indent=2)
    os.chmod(LEDGER, 0o600)


def guard_prompt(prompt: str) -> None:
    low = prompt.lower()
    for pat, why in FORBIDDEN:
        if re.search(pat, prompt if "script" in why else low, re.IGNORECASE):
            print(f"PROMPT BLOCKED ({why}): refusing to generate", file=sys.stderr)
            sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompt", required=True, help="image description (policy-guarded)")
    ap.add_argument("--out", default=None, help="output PNG path (default internal/generated-images/)")
    ap.add_argument("--n", type=int, default=1, help="images this run (max 2)")
    ap.add_argument("--dry-run", action="store_true", help="check budget+guard, no API call")
    args = ap.parse_args()

    if args.n < 1 or args.n > MAX_PER_RUN:
        print(f"--n must be 1..{MAX_PER_RUN} (no bulk runs)", file=sys.stderr)
        return 2

    guard_prompt(args.prompt)

    env = load_env()
    key = env.get("OPENROUTER_API_KEY", "")
    model = env.get("ANP2_IMAGE_MODEL", "openai/gpt-5-image-mini")
    per_image_cap = float(env.get("ANP2_IMAGE_MAX_USD_PER_IMAGE", "0.06"))
    if not key:
        print("no OPENROUTER_API_KEY", file=sys.stderr)
        return 1

    led = load_ledger()
    spent = float(led.get("total_usd", 0.0))
    n_done = len(led.get("calls", []))
    print(f"per-image cap ${per_image_cap:.2f} ; lifetime so far: {n_done} images, ${spent:.4f}")
    # "use sparingly, not dozens" — soft notice, not a hard refusal.
    if n_done >= 12:
        print(f"NOTE: {n_done} images generated to date — keep usage sparing (operator: not dozens).")

    # Keep it cheap + leak-safe: short prompt, low detail, no text rendered.
    full_prompt = (f"{args.prompt}. Flat, minimal, low-detail vector style. "
                   f"No text, no words, no letters, no logos.")

    if args.dry_run:
        print(f"DRY-RUN — prompt guard OK; per-image cap ${per_image_cap:.2f}. Would call:")
        print(f"  model={model}  n={args.n}")
        print(f"  prompt={full_prompt}")
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    saved = []
    for i in range(args.n):
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": full_prompt}],
            "modalities": ["image", "text"],
            "usage": {"include": True},
        }).encode()
        req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://anp2.com",
            "X-Title": "ANP2",
        })
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            print(f"API error {e.code}: {e.read().decode('utf-8','replace')[:300]}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001
            print(f"request failed: {e}", file=sys.stderr)
            return 1

        # cost: prefer OpenRouter-reported usage.cost; else ESTIMATE from token
        # counts so the per-image cap below always has a real value to check.
        usage = resp.get("usage", {}) or {}
        cost = float(usage.get("cost", 0) or 0)
        cost_estimated = False
        if cost <= 0:
            pt = float(usage.get("prompt_tokens", 0) or 0)
            ct = float(usage.get("completion_tokens", 0) or 0)
            cost = pt * PRICE_PROMPT_USD + ct * PRICE_COMPLETION_USD
            cost_estimated = cost > 0

        # extract image (base64 data URL) from message.images[].image_url.url
        img_b64 = None
        try:
            msg = resp["choices"][0]["message"]
            for im in (msg.get("images") or []):
                url = (im.get("image_url") or {}).get("url", "")
                if url.startswith("data:image"):
                    img_b64 = url.split(",", 1)[1]
                    break
            # fallback: some responses embed the image as a data: URL in content
            if not img_b64 and isinstance(msg.get("content"), str):
                mt = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", msg["content"])
                if mt:
                    img_b64 = mt.group(1)
        except (KeyError, IndexError):
            pass

        out = args.out if (args.out and args.n == 1) else os.path.join(
            OUT_DIR, f"img-{int(time.time())}-{i}.png")
        if img_b64:
            with open(out, "wb") as f:
                f.write(base64.b64decode(img_b64))
            saved.append(out)
            print(f"  saved {out}  (cost ${cost:.4f}{' est' if cost_estimated else ''})")
        else:
            print(f"  NO IMAGE in response (usage={usage}). Raw keys: {list(resp.keys())}", file=sys.stderr)
            print(f"  message preview: {json.dumps(resp.get('choices',[{}])[0].get('message',{}))[:300]}", file=sys.stderr)

        # ledger (visibility only): reported cost if present, else a rough
        # estimate. The HARD control is the per-image cap enforced just below.
        charged = cost if cost > 0 else RESERVE_PER_IMAGE_USD
        led["total_usd"] = float(led.get("total_usd", 0.0)) + charged
        led.setdefault("calls", []).append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model": model, "reported_cost_usd": cost, "charged_usd": charged,
            "out": out if img_b64 else None, "prompt": args.prompt[:120],
        })
        save_ledger(led)

        if cost > per_image_cap:
            print(f"  WARNING: image cost ${cost:.4f} EXCEEDED per-image cap "
                  f"${per_image_cap:.2f} — stopping run. Use a simpler/smaller "
                  f"prompt next time.", file=sys.stderr)
            break

    print(f"done. {len(saved)} image(s) this run; lifetime {len(led.get('calls', []))} images, "
          f"${led['total_usd']:.4f} est total. per-image cap ${per_image_cap:.2f}")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
