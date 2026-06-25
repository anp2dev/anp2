import { test } from "node:test";
import assert from "node:assert/strict";
import { earnTick, converseTick, roomTick, resetIfNewDay, DEFAULT_CAPS } from "../src/lib/autopilot.js";

function mockIdentity(agentId = "me") {
  const calls = { accept: [], deliver: [], post: [] };
  let n = 0;
  return {
    agentId,
    acceptTask: async (id) => calls.accept.push(id),
    deliverResult: async (id, out) => calls.deliver.push({ id, out }),
    post: async (text, tags) => { calls.post.push({ text, tags }); return { id: "posted-" + (++n) }; },
    _calls: calls,
  };
}
const mockRelay = (events) => ({ query: async () => events });
const okLlm = async () => "TRANSFORMED";
const today = "2026-06-18";

test("earnTick: accepts a matching open task, delivers LLM result, counts it", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([
    { id: "t1", agent_id: "someone", content: JSON.stringify({ capability: "transform.text.demo", input: { text: "hi" } }) },
  ]);
  const counters = { day: today, earned: 0, talked: 0 };
  const res = await earnTick({ identity: id, relay, llm: okLlm, capability: "transform.text.demo", caps: DEFAULT_CAPS, counters, today, seenTasks: new Set() });
  assert.equal(res.action, "earned");
  assert.deepEqual(id._calls.accept, ["t1"]);
  assert.equal(id._calls.deliver[0].out.text, "TRANSFORMED");
  assert.equal(counters.earned, 1);
});

test("earnTick: ignores our own task and wrong capability", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([
    { id: "mine", agent_id: "me", content: JSON.stringify({ capability: "transform.text.demo" }) },
    { id: "other", agent_id: "x", content: JSON.stringify({ capability: "something.else" }) },
  ]);
  const res = await earnTick({ identity: id, relay, llm: okLlm, capability: "transform.text.demo", caps: DEFAULT_CAPS, counters: { day: today, earned: 0, talked: 0 }, today, seenTasks: new Set() });
  assert.equal(res.action, "skip");
  assert.equal(id._calls.accept.length, 0);
});

test("earnTick: respects daily cap", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([{ id: "t1", agent_id: "x", content: JSON.stringify({ capability: "c" }) }]);
  const res = await earnTick({ identity: id, relay, llm: okLlm, capability: "c", caps: { earnPerDay: 2, talkPerDay: 3 }, counters: { day: today, earned: 2, talked: 0 }, today, seenTasks: new Set() });
  assert.equal(res.action, "skip");
  assert.match(res.reason, /cap/);
});

test("earnTick: does not re-handle a seen task", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([{ id: "t1", agent_id: "x", content: JSON.stringify({ capability: "c" }) }]);
  const seen = new Set(["t1"]);
  const res = await earnTick({ identity: id, relay, llm: okLlm, capability: "c", caps: DEFAULT_CAPS, counters: { day: today, earned: 0, talked: 0 }, today, seenTasks: seen });
  assert.equal(res.action, "skip");
});

const NOW = 1781766774;
const base = (over = {}) => ({ identity: mockIdentity("me"), llm: okLlm, today, nowSec: NOW, caps: DEFAULT_CAPS, counters: { day: today, earned: 0, talked: 0 }, myPosts: [], threadState: {}, repliedTo: new Set(), ...over });

test("converseTick: PRIORITY — responds to a reply aimed at our post and keeps it live", async () => {
  const id = mockIdentity("me");
  // p1 references our post 'mine1' via e-tag => a reply to us
  const relay = mockRelay([{ id: "p1", agent_id: "other", tags: [["e", "mine1", "reply"]], content: "responding to your point" }]);
  const args = base({ identity: id, relay, myPosts: ["mine1"] });
  const res = await converseTick(args);
  assert.equal(res.action, "replied");
  assert.deepEqual(id._calls.post[0].tags, [["e", "p1", "reply"]]);
  assert.equal(args.myPosts.includes("posted-1"), true); // our new reply tracked for future detection
});

test("converseTick: CONCLUDE ends the thread (no infinite loop)", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([{ id: "p1", agent_id: "other", tags: [["e", "mine1", "reply"]], content: "ok that resolves it" }]);
  const threadState = {};
  const res = await converseTick(base({ identity: id, relay, myPosts: ["mine1"], threadState, llm: async () => "CONCLUDE: Agreed, good to settle here." }));
  assert.equal(res.action, "concluded");
  assert.equal(id._calls.post[0].text, "Agreed, good to settle here.");
  assert.equal(threadState["mine1"].concluded, true); // thread closed -> we won't reply again
});

test("converseTick: a concluded/maxed thread is not re-entered", async () => {
  const relay = mockRelay([{ id: "p9", agent_id: "x", tags: [["e", "mine1", "reply"]], content: "still poking the thread" }]);
  const res = await converseTick(base({ relay, myPosts: ["mine1"], threadState: { mine1: { turns: 4, concluded: false } } }));
  assert.equal(res.action, "idle"); // maxTurnsPerThread reached
});

test("converseTick: starts at most one new conversation when idle and worthwhile", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([{ id: "p2", agent_id: "x", content: "a fresh, substantive thought worth engaging with" }]);
  const res = await converseTick(base({ identity: id, relay }));
  assert.equal(res.action, "started");
  assert.equal(id._calls.post.length, 1);
});

test("converseTick: does NOT start new convos when already at maxActiveConversations", async () => {
  const relay = mockRelay([{ id: "p3", agent_id: "x", content: "another fresh post we could engage with" }]);
  const threadState = { a: { turns: 1, concluded: false }, b: { turns: 1, concluded: false } }; // 2 active
  const res = await converseTick(base({ relay, threadState }));
  assert.equal(res.action, "idle");
});

test("converseTick: value gate (SKIP) stays quiet", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([{ id: "p4", agent_id: "x", content: "a low-value post not worth a reply at all" }]);
  const res = await converseTick(base({ identity: id, relay, llm: async () => "SKIP" }));
  assert.equal(res.action, "skip");
  assert.equal(id._calls.post.length, 0);
});

test("converseTick: idle when nothing relevant (no manufactured chatter)", async () => {
  const relay = mockRelay([{ id: "mine", agent_id: "me", content: "our own post, nothing to do here" }]);
  const res = await converseTick(base({ relay }));
  assert.equal(res.action, "idle");
});

test("roomTick: contributes one message to a multi-agent room (group, tagged t=room)", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([
    { id: "m1", agent_id: "other1", content: "a real point about verification in this room" },
    { id: "m2", agent_id: "other2", content: "building on that with a second angle" },
  ]);
  const counters = { day: today, earned: 0, talked: 0 };
  const res = await roomTick({ identity: id, relay, llm: okLlm, room: "market", today, nowSec: NOW, caps: DEFAULT_CAPS, counters });
  assert.equal(res.action, "spoke");
  assert.deepEqual(id._calls.post[0].tags, [["t", "market"]]);
  assert.equal(counters.talked, 1);
});

test("roomTick: anti-monologue — if we spoke last, we wait", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([{ id: "m9", agent_id: "me", content: "my own most-recent message" }, { id: "m8", agent_id: "x", content: "earlier other message here" }]);
  const res = await roomTick({ identity: id, relay, llm: okLlm, room: "market", today, nowSec: NOW, caps: DEFAULT_CAPS, counters: { day: today, earned: 0, talked: 0 } });
  assert.equal(res.action, "idle");
  assert.equal(id._calls.post.length, 0);
});

test("roomTick: value gate (SKIP) stays quiet in the room", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([{ id: "m1", agent_id: "x", content: "low value chatter not worth joining" }]);
  const res = await roomTick({ identity: id, relay, llm: async () => "SKIP", room: "meta", today, nowSec: NOW, caps: DEFAULT_CAPS, counters: { day: today, earned: 0, talked: 0 } });
  assert.equal(res.action, "skip");
  assert.equal(id._calls.post.length, 0);
});

test("converseTick: honors the user-set talkPerDay cap (not just dailyCeiling)", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([{ id: "p", agent_id: "x", content: "a fresh post worth engaging with here" }]);
  const res = await converseTick(base({ identity: id, relay, caps: { ...DEFAULT_CAPS, talkPerDay: 2 }, counters: { day: today, earned: 0, talked: 2 } }));
  assert.equal(res.action, "idle");
  assert.equal(id._calls.post.length, 0);
});

test("roomTick: honors the user-set talkPerDay cap", async () => {
  const id = mockIdentity("me");
  const relay = mockRelay([{ id: "m1", agent_id: "x", content: "room chatter worth joining here" }]);
  const res = await roomTick({ identity: id, relay, llm: okLlm, room: "meta", today, nowSec: NOW, caps: { ...DEFAULT_CAPS, talkPerDay: 3 }, counters: { day: today, earned: 0, talked: 3 } });
  assert.equal(res.action, "idle");
  assert.equal(id._calls.post.length, 0);
});

test("resetIfNewDay zeroes counters on a new day", () => {
  const c = { day: "2026-06-17", earned: 9, talked: 9 };
  resetIfNewDay(c, "2026-06-18");
  assert.equal(c.earned, 0); assert.equal(c.talked, 0); assert.equal(c.day, "2026-06-18");
});
