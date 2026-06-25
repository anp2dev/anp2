/**
 * Earn demo: run the REAL autopilot earn path (src/lib/autopilot.js → earnTick)
 * so you can watch the agent TAKE ON a task, do the work, and deliver a result —
 * and prove it can't run away (bounded by earnPerDay, no token burst).
 *
 * SAFETY: LLM + relay + identity are MOCKED — no tokens, nothing posted to the
 * live network. The mock "AI" actually transforms the task input (uppercases it)
 * so the delivered result is visibly derived from the work requested.
 *
 * Run: npm run earn
 */
import { earnTick, DEFAULT_CAPS } from "../src/lib/autopilot.js";

const CAPS = DEFAULT_CAPS;
const CAP = "transform.text.demo";
const MY = "ME".padEnd(64, "m");
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };

const mkIdentity = (ledger) => ({
  agentId: MY,
  async acceptTask(id) { ledger.accepts.push(id); },
  async deliverResult(id, res) { ledger.deliveries.push({ id, text: res.text }); },
});
// The mock "AI" does the actual work: it pulls the task input out of the prompt
// and transforms it (uppercase). Deterministic, no tokens, no network.
const mkLlm = (st) => async (prompt) => {
  st.calls++;
  const m = prompt.match(/Input:\n([\s\S]*?)\n\nProduce/);
  const input = (m ? m[1] : "").trim();
  return input ? input.toUpperCase() : "DONE";
};

// ============ Scenario A: take on real-ish tasks (watch the work) ========
console.log("\n=== Scenario A: the agent takes on tasks and delivers results ===");
{
  const TASKS = [
    { id: "t1", agent_id: "clientA".padEnd(64, "c"), kind: 50, content: JSON.stringify({ capability: CAP, input: { text: "summarize this for me" } }) },
    { id: "t2", agent_id: "clientB".padEnd(64, "c"), kind: 50, content: JSON.stringify({ capability: "image.generate", input: { text: "a cat" } }) }, // wrong capability → skip
    { id: "t3", agent_id: MY,                          kind: 50, content: JSON.stringify({ capability: CAP, input: { text: "my own task" } }) },        // our own → skip
    { id: "t4", agent_id: "clientC".padEnd(64, "c"), kind: 50, content: JSON.stringify({ capability: CAP, input: { text: "format these notes" } }) },
  ];
  const ledger = { accepts: [], deliveries: [] };
  const st = { calls: 0 };
  const identity = mkIdentity(ledger);
  const llm = mkLlm(st);
  const seenTasks = new Set();
  const counters = { day: "2026-07-01", earned: 0, talked: 0 };
  const relay = { async query() { return TASKS; } };

  for (let i = 0; i < 6; i++) {
    const r = await earnTick({ identity, relay, llm, capability: CAP, caps: CAPS, counters, today: "2026-07-01", seenTasks });
    if (r.action === "earned") console.log(`  📥 accepted ${r.taskId}  →  📤 delivered: "${r.output}"`);
    else console.log(`  ·  ${r.action}${r.reason ? " (" + r.reason + ")" : ""}`);
  }

  check("accepts only matching, non-own tasks", ledger.accepts.slice().sort().join(",") === "t1,t4", `accepted [${ledger.accepts}]`);
  check("did the work: result is derived from the task input", ledger.deliveries.find((d) => d.id === "t1")?.text === "SUMMARIZE THIS FOR ME");
  check("skipped the wrong-capability task and our own task", !ledger.accepts.includes("t2") && !ledger.accepts.includes("t3"));
  check("never accepts the same task twice", new Set(ledger.accepts).size === ledger.accepts.length);
}

// ============ Scenario B: an endless task queue must be capped ===========
console.log("\n=== Scenario B: an endless queue of tasks must not be taken without limit ===");
{
  const ledger = { accepts: [], deliveries: [] };
  const st = { calls: 0 };
  const identity = mkIdentity(ledger);
  const llm = mkLlm(st);
  const seenTasks = new Set();
  const counters = { day: "", earned: 0, talked: 0 };
  let seq = 0;
  const relay = { async query() { return [{ id: `task-${++seq}`, agent_id: "client".padEnd(64, "c"), kind: 50, content: JSON.stringify({ capability: CAP, input: { text: `job ${seq}` } }) }]; } };

  const perDay = {};
  for (let day = 1; day <= 3; day++) {
    const today = `2026-07-0${day + 1}`;
    const before = ledger.accepts.length;
    for (let tick = 0; tick < 60; tick++) {
      await earnTick({ identity, relay, llm, capability: CAP, caps: CAPS, counters, today, seenTasks });
    }
    perDay[today] = ledger.accepts.length - before;
    console.log(`  ${today}: accepted=${perDay[today]}  (earnPerDay=${CAPS.earnPerDay})  cumulative llmCalls=${st.calls}`);
  }
  const maxPerDay = Math.max(...Object.values(perDay));
  check("tasks taken/day never exceed earnPerDay", maxPerDay <= CAPS.earnPerDay, `max ${maxPerDay} ≤ ${CAPS.earnPerDay}`);
  check("each new day resets and re-caps (not a one-time stop)", Object.values(perDay).every((p) => p > 0 && p <= CAPS.earnPerDay));
  check("llm calls bounded (no token burst) across 180 ticks", st.calls <= 3 * CAPS.earnPerDay + 2, `${st.calls} calls`);
  check("once the daily cap is hit, further ticks idle", maxPerDay === CAPS.earnPerDay);
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) { console.error("FAILED:", failed.map((f) => f.name).join("; ")); process.exit(1); }
console.log("Earn path works AND stays bounded — no runaway.");
