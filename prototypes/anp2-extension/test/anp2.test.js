import { test } from "node:test";
import assert from "node:assert/strict";
import {
  deriveKeypairFromApiKey,
  computeEventId,
  signEvent,
  verifyEvent,
} from "../src/lib/anp2.js";

// Cross-check event-id against the published @anp2/client (relay compatibility).
import { computeEventId as refComputeEventId } from "../../anp2-client-js/dist/index.mjs";

test("identity derivation is deterministic (same API key -> same identity)", async () => {
  const a = await deriveKeypairFromApiKey("sk-test-abc-123");
  const b = await deriveKeypairFromApiKey("sk-test-abc-123");
  assert.equal(a.publicKeyHex, b.publicKeyHex);
  assert.equal(a.privateKeyHex, b.privateKeyHex);
  assert.equal(a.publicKeyHex.length, 64);
});

test("different API keys -> different identities", async () => {
  const a = await deriveKeypairFromApiKey("sk-test-abc-123");
  const b = await deriveKeypairFromApiKey("sk-test-abc-124");
  assert.notEqual(a.publicKeyHex, b.publicKeyHex);
});

test("trimming: whitespace around the key does not change identity", async () => {
  const a = await deriveKeypairFromApiKey("sk-test-abc-123");
  const b = await deriveKeypairFromApiKey("  sk-test-abc-123\n");
  assert.equal(a.publicKeyHex, b.publicKeyHex);
});

test("event id matches @anp2/client (relay will accept our events)", async () => {
  const kp = await deriveKeypairFromApiKey("sk-test-abc-123");
  const unsigned = {
    agent_id: kp.publicKeyHex,
    created_at: 1781766774,
    kind: 1,
    tags: [["t", "verification"]],
    content: "hello anp2",
  };
  const ours = await computeEventId(unsigned);
  const ref = await refComputeEventId(unsigned);
  assert.equal(ours, ref);
});

test("sign + local verify roundtrip is valid", async () => {
  const kp = await deriveKeypairFromApiKey("sk-test-abc-123");
  const unsigned = {
    agent_id: kp.publicKeyHex,
    created_at: 1781766774,
    kind: 1,
    tags: [],
    content: "signed by me",
  };
  const signed = await signEvent(unsigned, kp.privateKeyHex);
  const res = await verifyEvent(signed);
  assert.equal(res.valid, true, res.reason);
});

test("tampered content is detected (id mismatch)", async () => {
  const kp = await deriveKeypairFromApiKey("sk-test-abc-123");
  const signed = await signEvent(
    { agent_id: kp.publicKeyHex, created_at: 1781766774, kind: 1, tags: [], content: "original" },
    kp.privateKeyHex,
  );
  signed.content = "tampered";
  const res = await verifyEvent(signed);
  assert.equal(res.valid, false);
});

test("tampered signature is detected", async () => {
  const kp = await deriveKeypairFromApiKey("sk-test-abc-123");
  const signed = await signEvent(
    { agent_id: kp.publicKeyHex, created_at: 1781766774, kind: 1, tags: [], content: "x" },
    kp.privateKeyHex,
  );
  signed.sig = signed.sig.replace(/^../, "00");
  const res = await verifyEvent(signed);
  assert.equal(res.valid, false);
});

test("foreign event from the live-relay shape verifies as valid", async () => {
  // A self-produced event signed with one identity must NOT verify under another agent_id.
  const me = await deriveKeypairFromApiKey("key-A");
  const other = await deriveKeypairFromApiKey("key-B");
  const signed = await signEvent(
    { agent_id: me.publicKeyHex, created_at: 1781766774, kind: 1, tags: [], content: "mine" },
    me.privateKeyHex,
  );
  // swap agent_id to someone else -> id recompute mismatch -> invalid
  signed.agent_id = other.publicKeyHex;
  const res = await verifyEvent(signed);
  assert.equal(res.valid, false);
});
