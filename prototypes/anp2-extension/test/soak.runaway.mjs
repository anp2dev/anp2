/**
 * Runaway soak: run the REAL autopilot conversation logic (src/lib/autopilot.js)
 * for a long time and prove it can't run away — no infinite talk, no token burst.
 *
 * SAFETY: everything is mocked — the LLM (no tokens), the relay + identity (no
 * posts to the live network). We only exercise the real decision/cap logic.
 *
 * The network here is ADVERSARIAL: it replies to us on EVERY tick and always
 * offers a fresh thread to open. Without caps the agent would post on every
 * single tick forever. With caps it must stay bounded.
 *
 * Run: npm run soak
 */
import { converseTick, roomTick, resetIfNewDay, DEFAULT_CAPS } from "../src/lib/autopilot.js";

const CAPS = DEFAULT_CAPS;
const log = [];
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };

// --- mocks ---------------------------------------------------------------
let postSeq = 0;
const mkIdentity = (posts) => ({
  agentId: "ME".padEnd(64, "m"),
  async post(text, tags) { const id = `mine-${++postSeq}`; posts.push({ id, text, tags }); return { id }; },
});
const mkLlm = (st, mode = "reply") => async (prompt /*, opts */) => {
  st.llmCalls++;
  if (mode === "conclude") return "CONCLUDE: good talk, we agree.";
  return "A concrete, substantive reply that moves the point forward.";
};

// Adversarial "eager" feed: a reply e-tagging our latest post (keeps talking) +
// a fresh opener every tick (always something new to start).
const mkEagerRelay = (posts) => ({
  async query() {
    const out = [];
    const last = posts[posts.length - 1];
    let n = posts.length;
    if (last) out.push({ id: `reply-${++n}-${last.id}`, agent_id: "OTHER".padEnd(64, "o"), kind: 1, created_at: 2000 + n, content: "Here is my counterpoint — what do you think? Let's keep going.", tags: [["e", last.id]] });
    out.push({ id: `open-${++n}`, agent_id: "OPEN".padEnd(64, "x"), kind: 1, created_at: 2000 + n, content: "A genuinely interesting fresh topic worth engaging with at length.", tags: [] });
    return out;
  },
});

// ============ Scenario 1: long soak vs an eager network ==================
console.log("\n=== Scenario 1: 3 simulated days × 120 ticks vs an eager network ===");
{
  const posts = [];
  const identity = mkIdentity(posts);
  const relay = mkEagerRelay(posts);
  const st = { llmCalls: 0 };
  const llm = mkLlm(st);
  const counters = { day: "", earned: 0, talked: 0 };
  let myPosts = [], threadState = {}, repliedTo = new Set();
  const perDay = {};
  const DAYS = 10, TICKS = 120;
  let maxThreadStateSize = 0;

  for (let day = 1; day <= DAYS; day++) {
    const today = `2026-06-${String(20 + day).padStart(2, "0")}`;
    const startPosts = posts.length;
    for (let tick = 0; tick < TICKS; tick++) {
      await converseTick({ identity, relay, llm, today, nowSec: tick, caps: CAPS, counters, myPosts, threadState, repliedTo });
      maxThreadStateSize = Math.max(maxThreadStateSize, Object.keys(threadState).length);
    }
    perDay[today] = posts.length - startPosts;
    console.log(`  ${today}: posts=${perDay[today]}  (talkCap=${Math.min(CAPS.talkPerDay, CAPS.dailyCeiling)})  cumulative llmCalls=${st.llmCalls}  threadState=${Object.keys(threadState).length}`);
  }
  const maxPerDay = Math.max(...Object.values(perDay));
  const cap = Math.min(CAPS.talkPerDay, CAPS.dailyCeiling);
  const ticks = DAYS * TICKS;
  check("posts/day never exceed the daily talk cap", maxPerDay <= cap, `max ${maxPerDay} ≤ ${cap}`);
  check("each new day resets and re-caps (not a one-time stop)", Object.values(perDay).every((p) => p > 0 && p <= cap));
  check(`llm calls bounded (no token burst) over ${ticks} ticks`, st.llmCalls <= DAYS * cap + 6, `${st.llmCalls} calls in ${ticks} ticks`);
  check("threadState stays bounded (no unbounded memory growth)", maxThreadStateSize <= CAPS.maxThreadState, `max ${maxThreadStateSize} ≤ ${CAPS.maxThreadState}`);
  check("once capped, further ticks idle (posts == cap, not growing)", maxPerDay === cap);
}

// ============ Scenario 2: a single thread must CONCLUDE ==================
console.log("\n=== Scenario 2: one thread with a stubborn interlocutor (root-consistent) ===");
{
  const posts = [{ id: "root-0" }]; // we already opened this thread
  postSeq = 100;
  const identity = mkIdentity(posts);
  const st = { llmCalls: 0 };
  const llm = mkLlm(st);
  // interlocutor ALWAYS replies e-tagging the SAME root → turns accumulate on one thread
  let n = 0;
  const relay = { async query() { return [{ id: `rep-${++n}`, agent_id: "OTHER".padEnd(64, "o"), kind: 1, created_at: 3000 + n, content: "But consider this again — keep replying.", tags: [["e", "root-0"]] }]; } };
  const counters = { day: "2026-07-01", earned: 0, talked: 0 };
  let myPosts = ["root-0"], threadState = {}, repliedTo = new Set();
  let replies = 0;
  for (let tick = 0; tick < 30; tick++) {
    const before = posts.length;
    await converseTick({ identity, relay, llm, today: "2026-07-01", nowSec: tick, caps: CAPS, counters, myPosts, threadState, repliedTo });
    if (posts.length > before) replies++;
  }
  const turns = threadState["root-0"]?.turns ?? 0;
  const concluded = !!threadState["root-0"]?.concluded;
  console.log(`  thread root-0: turns=${turns}, concluded=${concluded}, our replies in thread=${replies}`);
  check("a single thread concludes at maxTurnsPerThread", turns === CAPS.maxTurnsPerThread && concluded, `turns ${turns} == ${CAPS.maxTurnsPerThread}`);
  check("thread does not exceed the turn cap even vs a stubborn interlocutor", replies <= CAPS.maxTurnsPerThread);
}

// ============ Scenario 3: rooms — no monologue, capped ===================
console.log("\n=== Scenario 3: group room — anti-monologue + cap ===");
{
  const posts = [];
  const identity = mkIdentity(posts);
  const st = { llmCalls: 0 };
  const llm = mkLlm(st);
  const myId = identity.agentId;
  // room where WE always spoke last → anti-monologue must keep us idle
  const relayMine = { async query() { return [{ id: "m1", agent_id: myId, kind: 1, created_at: 9, content: "my last word", tags: [["t", "meta"]] }]; } };
  const counters = { day: "2026-07-02", earned: 0, talked: 0 };
  let spoke = 0;
  for (let tick = 0; tick < 30; tick++) {
    const r = await roomTick({ identity, relay: relayMine, llm, room: "meta", today: "2026-07-02", nowSec: tick, caps: CAPS, counters });
    if (r.action === "spoke") spoke++;
  }
  check("never monologues when we spoke last in the room", spoke === 0, `spoke ${spoke} times`);
  check("room path made no LLM calls when idle (no token waste)", st.llmCalls === 0);
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} runaway-safety checks passed`);
if (failed.length) { console.error("FAILED:", failed.map((f) => f.name).join("; ")); process.exit(1); }
console.log("NO RUNAWAY — all caps held.");
