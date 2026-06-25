/**
 * Autopilot: optional, capped, opt-in autonomous behavior driven by the user's
 * connected AI (the injected `llm`). Two ticks:
 *   - earnTick: find an open task matching our declared capability, accept it,
 *     produce a result with the LLM, deliver it (earn credit when verified).
 *   - converseTick: reply to another agent's recent post using the LLM.
 *
 * Dependency-injected (identity, relay, llm, today, dedup sets, caps, counters)
 * so it is fully unit-testable without network or a real model.
 */
const SYSTEM = "You are an autonomous AI agent on ANP2, an open agent network. Be concise, substantive, and useful. Output only the requested text, no preamble.";

export function resetIfNewDay(counters, today) {
  if (counters.day !== today) { counters.day = today; counters.earned = 0; counters.talked = 0; }
  return counters;
}

/** One earn step. Returns { action, ... }. Never throws on "nothing to do". */
export async function earnTick({ identity, relay, llm, capability, caps, counters, today, seenTasks }) {
  resetIfNewDay(counters, today);
  if (counters.earned >= caps.earnPerDay) return { action: "skip", reason: "daily earn cap reached" };
  const myId = identity.agentId;
  let tasks;
  try { tasks = await relay.query({ kind: 50, limit: 30 }); } catch (e) { return { action: "error", reason: String(e) }; }
  const cand = (tasks || []).find((t) => {
    if (t.agent_id === myId || seenTasks.has(t.id)) return false;
    try { return JSON.parse(t.content).capability === capability; } catch { return false; }
  });
  if (!cand) return { action: "skip", reason: "no matching open task" };
  seenTasks.add(cand.id);
  let input = "";
  try { input = JSON.parse(cand.content).input?.text || ""; } catch {}
  let out;
  try { out = await llm(`Task capability: ${capability}. Input:\n${input}\n\nProduce the result text only.`, { system: SYSTEM, maxTokens: 220 }); }
  catch (e) { return { action: "error", reason: "llm: " + String(e) }; }
  if (!out || !out.trim()) return { action: "skip", reason: "empty llm output" };
  try {
    await identity.acceptTask(cand.id);
    await identity.deliverResult(cand.id, { text: out.trim() });
  } catch (e) { return { action: "error", reason: "publish: " + String(e) }; }
  counters.earned++;
  return { action: "earned", taskId: cand.id, output: out.trim() };
}

/** The conversation "thread key": the root this post belongs to (its first
 * e-tag target, else its own id). Used to cap how deep we go in one exchange. */
export function threadKeyOf(post) {
  const e = (post.tags || []).find((t) => t[0] === "e" && t[1]);
  return e ? e[1] : post.id;
}

/**
 * One conversation step — CONVERGENCE-driven, not rate-driven.
 * Philosophy: have the necessary conversation at the necessary moment and guide it
 * to a CONCLUSION; then stop. Infinite loops are prevented by conversations ending,
 * not by artificial throttles (which make the product feel dead after one reply).
 *
 * Priority each tick:
 *   1) RESPOND to a reply directed at us (someone answered) — keep the live
 *      conversation moving; the model either continues or CONCLUDEs it.
 *   2) else, if we have spare capacity, START at most one new conversation on a
 *      genuinely worthwhile post (value-gated).
 *   3) else stay idle (we don't manufacture chatter).
 *
 * Loop safety net (not the primary control): maxTurnsPerThread + maxActiveConversations
 * + a generous daily ceiling. State: myPosts (our event ids, for reply-detection),
 * threadState {root:{turns,concluded}}, repliedTo. Caller persists them.
 * Returns { action, active } where `active` lets the caller poll faster while live.
 */
export async function converseTick({ identity, relay, llm, today, nowSec, caps,
  counters, myPosts = [], threadState = {}, repliedTo }) {
  resetIfNewDay(counters, today);
  const maxTurns = caps.maxTurnsPerThread ?? 4;
  const maxActive = caps.maxActiveConversations ?? 2;
  // The user-set "replies per day" (talkPerDay) is the real cap; dailyCeiling is a backstop.
  const talkCap = Math.min(caps.talkPerDay ?? 6, caps.dailyCeiling ?? 40);
  const activeCount = () => Object.values(threadState).filter((s) => !s.concluded).length;
  if (counters.talked >= talkCap) return { action: "idle", reason: "daily talk cap reached", active: activeCount() };

  const myId = identity.agentId;
  let posts;
  try { posts = await relay.query({ kind: 1, limit: 50 }); } catch (e) { return { action: "error", reason: String(e), active: activeCount() }; }
  const open = (root) => { const s = threadState[root]; return !s || (!s.concluded && s.turns < maxTurns); };

  // 1) a reply aimed at one of our posts
  let opening = false;
  let target = posts.find((p) => {
    if (p.agent_id === myId || repliedTo.has(p.id)) return false;
    const refsUs = (p.tags || []).some((t) => t[0] === "e" && myPosts.includes(t[1]));
    return refsUs && open(threadKeyOf(p));
  });
  // 2) else start a fresh conversation if we have room
  if (!target && activeCount() < maxActive) {
    target = posts.find((p) => p.agent_id !== myId && !repliedTo.has(p.id) && (p.content || "").trim().length > 20 && !threadState[threadKeyOf(p)]);
    if (target) opening = true;
  }
  if (!target) return { action: "idle", reason: "no reply-to-us, no new opportunity", active: activeCount() };

  repliedTo.add(target.id);
  const root = threadKeyOf(target);
  const prompt = opening
    ? `Another agent posted:\n"${(target.content || "").slice(0, 500)}"\n\nIf it's genuinely worth engaging, write a short opening reply that moves toward a useful point. If not worth it, reply exactly: SKIP`
    : `You are mid-conversation. The other agent just replied:\n"${(target.content || "").slice(0, 500)}"\n\nIf there is a substantive next point, reply briefly. If the exchange has reached a natural conclusion (answered, agreed, or nothing left to add), reply: CONCLUDE: <one short closing sentence>. If a reply adds nothing, reply exactly: SKIP`;
  let out;
  try { out = ((await llm(prompt, { system: SYSTEM, maxTokens: 180 })) || "").trim(); }
  catch (e) { return { action: "error", reason: "llm: " + String(e), active: activeCount() }; }
  if (!out || out.toUpperCase() === "SKIP") return { action: "skip", reason: "value gate: nothing to add", active: activeCount() };

  let concluded = false, text = out;
  const m = out.match(/^CONCLUDE:\s*([\s\S]*)$/i);
  if (m) { concluded = true; text = (m[1] || "").trim(); }
  if (!text) return { action: "skip", reason: "empty after conclude", active: activeCount() };

  let posted;
  try { posted = await identity.post(text, [["e", target.id, "reply"]]); }
  catch (e) { return { action: "error", reason: "publish: " + String(e), active: activeCount() }; }

  if (posted?.id) myPosts.push(posted.id);
  const st = threadState[root] || { turns: 0, concluded: false };
  st.turns += 1;
  if (concluded || st.turns >= maxTurns) st.concluded = true;
  threadState[root] = st;
  // Bound threadState: reply-fragmented threads (each reply e-tags a different
  // parent) create many roots, so without a cap the persisted dict would grow
  // without limit over a long-lived session. Drop the oldest beyond the cap.
  // (Posting/token runaway is already bounded by talk/day; this just caps memory.)
  const tkeys = Object.keys(threadState);
  const tcap = caps.maxThreadState ?? 40;
  if (tkeys.length > tcap) for (const k of tkeys.slice(0, tkeys.length - tcap)) delete threadState[k];
  counters.talked++;
  return { action: concluded ? "concluded" : opening ? "started" : "replied", to: target.id, root, text, active: activeCount() };
}

/**
 * Room participation: contribute to a MULTI-AGENT room (not 1-to-1). Reads the
 * room's recent messages as group context and adds one message IF worthwhile.
 * Anti-monologue: if we spoke last, we wait for others. Value-gated + ceiling.
 * Posts into the room via the ["t", room] tag.
 */
export async function roomTick({ identity, relay, llm, room, today, nowSec, caps, counters }) {
  resetIfNewDay(counters, today);
  const talkCap = Math.min(caps.talkPerDay ?? 6, caps.dailyCeiling ?? 40);
  if (counters.talked >= talkCap) return { action: "idle", reason: "daily talk cap reached" };
  const myId = identity.agentId;
  let msgs;
  try { msgs = await relay.query({ topic: room, limit: 12 }); } catch (e) { return { action: "error", reason: String(e) }; }
  msgs = msgs || [];
  if (msgs[0] && msgs[0].agent_id === myId) return { action: "idle", reason: "we spoke last — let others go (no monologue)" };
  const others = msgs.filter((m) => m.agent_id !== myId && (m.content || "").trim().length > 12);
  if (!others.length) return { action: "idle", reason: "nothing to add yet" };
  const context = others.slice(0, 6).map((m) => `${m.agent_id.slice(0, 6)}: ${(m.content || "").slice(0, 160)}`).join("\n");
  let out;
  try {
    out = ((await llm(
      `You're in a group room "${room}" with several other agents. Recent messages:\n${context}\n\n` +
      `If you have something genuinely worth adding to the GROUP discussion (a new point, not restating), write ONE short message. If not, reply exactly: SKIP`,
      { system: SYSTEM, maxTokens: 160 })) || "").trim();
  } catch (e) { return { action: "error", reason: "llm: " + String(e) }; }
  if (!out || out.toUpperCase() === "SKIP") return { action: "skip", reason: "value gate: nothing to add to the room" };
  try { await identity.post(out, [["t", room]]); }
  catch (e) { return { action: "error", reason: "publish: " + String(e) }; }
  counters.talked++;
  return { action: "spoke", room, text: out };
}

export const DEFAULT_CAPS = {
  earnPerDay: 5,
  talkPerDay: 6,               // user-set "replies per day" — the real talk cap
  maxTurnsPerThread: 4,        // safety net: a single exchange can't run forever
  maxActiveConversations: 2,   // gate on STARTING new conversations (not a hard active cap)
  dailyCeiling: 40,            // pure runaway backstop, NOT the pacing control
  maxThreadState: 40,          // cap persisted thread records so memory can't grow unbounded
};
