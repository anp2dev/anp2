import { test } from "node:test";
import assert from "node:assert/strict";
import {
  deriveKeypairFromApiKey, signEvent, verifyEvent,
  buildTaskRequest, buildAccept, buildResult, buildCapabilityDecl,
} from "../src/lib/anp2.js";
import { computeEventId as refComputeEventId } from "../../anp2-client-js/dist/index.mjs";

async function signed(kp, built, created_at = 1781766774) {
  return signEvent({ agent_id: kp.publicKeyHex, created_at, ...built }, kp.privateKeyHex);
}

test("hire: kind-50 task request is well-formed, signed, verifiable", async () => {
  const kp = await deriveKeypairFromApiKey("hirer");
  const ev = await signed(kp, buildTaskRequest({ capability: "transform.text.demo", input: { text: "hi" }, rewardAmount: 10 }));
  assert.equal(ev.kind, 50);
  const body = JSON.parse(ev.content);
  assert.equal(body.capability, "transform.text.demo");
  assert.equal(body.reward.amount, 10);
  assert.equal(body.reward.payment_method, "anp2_credit");
  assert.ok(body.constraints.deadline_unix > 0);
  assert.equal((await verifyEvent(ev)).valid, true);
  assert.equal(await refComputeEventId(ev), ev.id); // relay-compatible id
});

test("earn: accept (51) and result (52) reference the task via [e, taskId, role]", async () => {
  const kp = await deriveKeypairFromApiKey("provider");
  const taskId = "a".repeat(64);
  const accept = await signed(kp, buildAccept(taskId));
  assert.equal(accept.kind, 51);
  assert.deepEqual(accept.tags[0], ["e", taskId, "accept"]);
  assert.equal((await verifyEvent(accept)).valid, true);

  const result = await signed(kp, buildResult(taskId, "done: HI"));
  assert.equal(result.kind, 52);
  assert.deepEqual(result.tags[0], ["e", taskId, "result"]);
  assert.equal(JSON.parse(result.content).output, "done: HI");
  assert.equal((await verifyEvent(result)).valid, true);
});

test("declare capability: kind-4 is well-formed + verifiable", async () => {
  const kp = await deriveKeypairFromApiKey("provider");
  const ev = await signed(kp, buildCapabilityDecl([{ name: "transform.text.demo" }]));
  assert.equal(ev.kind, 4);
  assert.equal(JSON.parse(ev.content).capabilities[0].name, "transform.text.demo");
  assert.equal((await verifyEvent(ev)).valid, true);
  assert.equal(await refComputeEventId(ev), ev.id);
});

test("tampering a task reward is detected", async () => {
  const kp = await deriveKeypairFromApiKey("hirer");
  const ev = await signed(kp, buildTaskRequest({ capability: "x", rewardAmount: 10 }));
  const body = JSON.parse(ev.content); body.reward.amount = 9999; ev.content = JSON.stringify(body);
  assert.equal((await verifyEvent(ev)).valid, false);
});
