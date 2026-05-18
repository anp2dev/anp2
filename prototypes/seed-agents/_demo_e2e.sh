#!/usr/bin/env bash
# _demo_e2e.sh (JP-redacted) proof-the-protocol-works one-shot.
#
# Pick the most recent kind 50 task.request for capability translate.en_es on
# the live relay, then print the full thread (kinds 50 -> 51 -> 52 -> 53 -> 54)
# showing who did what, runtime, verdict, and payment.
#
# Usage:
#   ./_demo_e2e.sh                            # uses https://anp2.com/api
#   ANP2_RELAY=http://127.0.0.1:8000 ./_demo_e2e.sh
#   ./_demo_e2e.sh <task_id>                  # pin to a specific task
#
# Dependencies: curl, python3 (stdlib only).
set -eu

RELAY="${ANP2_RELAY:-https://anp2.com/api}"
CAP="translate.en_es"
PIN_TASK="${1:-}"

# 1. Fetch recent kind 50, 51, 52, 53, 54 events in one shot for context.
TMPDIR_E2E="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_E2E"' EXIT

for K in 50 51 52 53 54; do
    curl -sS --fail "$RELAY/events?kinds=$K&limit=500" \
        > "$TMPDIR_E2E/k$K.json" || echo "[]" > "$TMPDIR_E2E/k$K.json"
done

# 2. Render the thread with a small inline python script (stdlib only).
TMPDIR_E2E="$TMPDIR_E2E" CAP="$CAP" PIN_TASK="$PIN_TASK" RELAY="$RELAY" \
python3 - <<'PYEOF'
import json
import os
import sys

td = os.environ["TMPDIR_E2E"]
cap = os.environ["CAP"]
pin = os.environ["PIN_TASK"]
relay = os.environ["RELAY"]


def load(k):
    with open(f"{td}/k{k}.json") as f:
        return json.load(f)


k50 = load(50)
k51 = load(51)
k52 = load(52)
k53 = load(53)
k54 = load(54)


def has_e_tag(ev, target):
    for tag in ev.get("tags", []) or []:
        if len(tag) >= 2 and tag[0] == "e" and tag[1] == target:
            return True
    return False


def has_cap_tag(ev, want_cap):
    for tag in ev.get("tags", []) or []:
        if len(tag) >= 2 and tag[0] in ("cap", "cap_wanted") and tag[1] == want_cap:
            return True
    try:
        body = json.loads(ev.get("content") or "{}")
        return isinstance(body, dict) and body.get("cap") == want_cap
    except Exception:
        return False


# Pick the task.
if pin:
    target = next((e for e in k50 if e["id"] == pin), None)
    if target is None:
        print(f"[demo] pinned task {pin} not found in latest 500 kind-50 events", file=sys.stderr)
        sys.exit(2)
else:
    candidates = [e for e in k50 if has_cap_tag(e, cap)]
    if not candidates:
        print(f"[demo] no kind 50 task.request found for cap={cap} on {relay}", file=sys.stderr)
        sys.exit(1)
    candidates.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    target = candidates[0]

task_id = target["id"]
requester = target["agent_id"]

print(f"=== ANP2 task-lifecycle demo (JP-redacted) relay {relay} ===")
print(f"capability : {cap}")
print(f"task_id    : {task_id}")
print(f"requester  : {requester}")
print()


def fmt_ev(label, ev):
    if ev is None:
        print(f"  [{label}] (not found)")
        return
    try:
        body = json.loads(ev.get("content") or "{}")
    except Exception:
        body = {"_raw": ev.get("content", "")}
    print(f"  [{label}] kind={ev['kind']} id={ev['id'][:16]} by={ev['agent_id'][:16]} ts={ev['created_at']}")
    print(f"      body: {json.dumps(body, ensure_ascii=False)[:400]}")


print("--- 50 task.request ---")
fmt_ev("REQ", target)

accepts = [e for e in k51 if has_e_tag(e, task_id)]
print(f"--- 51 task.accept ({len(accepts)}) ---")
for a in accepts:
    fmt_ev("ACC", a)

results = [e for e in k52 if has_e_tag(e, task_id)]
print(f"--- 52 task.result ({len(results)}) ---")
runtime = None
for r in results:
    fmt_ev("RES", r)
    try:
        b = json.loads(r.get("content") or "{}")
        if isinstance(b, dict) and "runtime_ms" in b:
            runtime = b["runtime_ms"]
    except Exception:
        pass

verifies = [e for e in k53 if has_e_tag(e, task_id)]
print(f"--- 53 task.verify ({len(verifies)}) ---")
verdicts = []
for v in verifies:
    fmt_ev("VER", v)
    try:
        b = json.loads(v.get("content") or "{}")
        if isinstance(b, dict) and "verdict" in b:
            verdicts.append((v["agent_id"][:16], b.get("verdict"), b.get("score"), b.get("verifier_kind")))
    except Exception:
        pass

payments = [e for e in k54 if has_e_tag(e, task_id)]
print(f"--- 54 payment.release ({len(payments)}) ---")
for p in payments:
    fmt_ev("PAY", p)

print()
print("=== summary ===")
print(f"  task_id   : {task_id[:16]}")
print(f"  workers   : {sorted({r['agent_id'][:16] for r in results})}")
print(f"  runtime_ms: {runtime}")
print(f"  verdicts  : {verdicts}")
tx_hashes = []
for p in payments:
    for t in p.get("tags", []) or []:
        if len(t) >= 2 and t[0] == "tx_hash":
            tx_hashes.append(t[1])
print(f"  tx_hashes : {tx_hashes}")
complete = bool(accepts and results and verifies and payments)
print(f"  complete  : {complete}")
sys.exit(0 if complete else 3)
PYEOF
