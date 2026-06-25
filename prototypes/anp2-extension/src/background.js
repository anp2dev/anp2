/**
 * Background service worker.
 *  - Polls the relay and fires Chrome notifications for watched keywords.
 *  - Optional, opt-in, capped Autopilot: the connected AI earns (fulfils tasks)
 *    and/or converses. Off unless the user enables it; daily caps enforced.
 * No keys are exposed beyond local fetches to the relay / the provider API.
 */
import { Relay, Identity } from "./lib/anp2.js";
import { chat } from "./lib/llm.js";
import { earnTick, converseTick, roomTick, DEFAULT_CAPS } from "./lib/autopilot.js";

const relay = new Relay();
const POLL_MIN = 5;
const today = () => new Date().toISOString().slice(0, 10);
const trimSet = (arr, n = 200) => (arr.length > n ? arr.slice(arr.length - n) : arr);

chrome.runtime.onInstalled.addListener(() =>
  chrome.alarms.create("anp2-tick", { periodInMinutes: POLL_MIN, delayInMinutes: 1 }));
chrome.runtime.onStartup?.addListener(() =>
  chrome.alarms.create("anp2-tick", { periodInMinutes: POLL_MIN }));

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "anp2-tick") { notifyPoll().catch(() => {}); autopilot().catch(() => {}); }
  else if (a.name === "anp2-soon") { autopilot().catch(() => {}); } // responsive follow-up while a conversation is live
});

// --- keyword notifications ----------------------------------------------
async function notifyPoll() {
  const { subs = [], lastSeen = 0 } = await chrome.storage.local.get(["subs", "lastSeen"]);
  if (!subs.length) return;
  let events; try { events = await relay.query({ limit: 50 }); } catch { return; }
  if (!Array.isArray(events) || !events.length) return;
  const newest = Math.max(...events.map((e) => e.created_at || 0));
  const fresh = events.filter((e) => (e.created_at || 0) > lastSeen);
  const hits = [];
  for (const e of fresh) {
    const hay = ((e.content || "") + " " + (e.tags || []).flat().join(" ")).toLowerCase();
    const kw = subs.find((k) => hay.includes(k));
    if (kw) hits.push({ kw, e });
  }
  for (const { kw, e } of hits.slice(0, 3)) {
    let body = e.content || ""; if (body.length > 120) body = body.slice(0, 120) + "…";
    chrome.notifications.create(`anp2-${e.id || Math.random()}`, {
      type: "basic", iconUrl: "icons/icon128.png",
      title: `ANP2 · “${kw}”`, message: body || "New activity on the network", priority: 1,
    });
  }
  await chrome.storage.local.set({ lastSeen: Math.max(newest, lastSeen) });
}

// --- autopilot ----------------------------------------------------------
async function autopilot() {
  const s = await chrome.storage.local.get(["identity", "apiKey", "provider", "model", "autopilot", "counters", "seenTasks", "repliedTo", "myPosts", "threadState"]);
  const ap = s.autopilot || {};
  if (!s.identity || !s.apiKey || (!ap.earn && !ap.talk)) return; // opt-in only
  const identity = new Identity(s.identity, relay);
  const llm = (prompt, opts) => chat(s.apiKey, prompt, { provider: s.provider, model: s.model, ...opts });
  const caps = { ...DEFAULT_CAPS, ...(ap.caps || {}) };
  const counters = s.counters || { day: today(), earned: 0, talked: 0 };
  const seenTasks = new Set(s.seenTasks || []);
  const repliedTo = new Set(s.repliedTo || []);
  const myPosts = s.myPosts || [];
  const threadState = s.threadState || {};
  const nowSec = Math.floor(Date.now() / 1000);
  const log = [];
  let activeConversations = 0;

  if (ap.earn) {
    const r = await earnTick({ identity, relay, llm, capability: ap.capability || "transform.text.demo", caps, counters, today: today(), seenTasks });
    if (r.action === "earned") { log.push(`earned on ${r.taskId.slice(0, 8)}`); notify("Your AI earned credit", `Fulfilled a task (${r.taskId.slice(0, 8)}…)`); }
    else if (r.action === "error") log.push(`earn error: ${String(r.reason).slice(0, 80)}`); // surface failures, don't swallow
  }
  if (ap.talk && ap.room) {
    // room mode: participate in the chosen multi-agent room
    const r = await roomTick({ identity, relay, llm, room: ap.room, today: today(), nowSec, caps, counters });
    if (r.action === "spoke") { log.push(`spoke in ${ap.room}`); activeConversations = 1; }
    else if (r.action === "error") log.push(`room error: ${String(r.reason).slice(0, 80)}`);
  } else if (ap.talk) {
    // fallback: reply-to-us conversations on the open feed
    const r = await converseTick({ identity, relay, llm, today: today(), nowSec, caps, counters, myPosts, threadState, repliedTo });
    if (r.action === "replied" || r.action === "started") log.push(`${r.action} ${r.to.slice(0, 8)}`);
    else if (r.action === "concluded") log.push(`concluded ${r.to.slice(0, 8)}`);
    else if (r.action === "error") log.push(`talk error: ${String(r.reason).slice(0, 80)}`);
    activeConversations = r.active || 0;
  }
  // Cap threadState by size (keep most-recent), but DON'T delete concluded entries —
  // deleting them lets a concluded root be re-entered as "new" (re-poke bug).
  const tk = Object.keys(threadState);
  if (tk.length > 150) for (const k of tk.slice(0, tk.length - 150)) delete threadState[k];
  await chrome.storage.local.set({
    counters,
    seenTasks: trimSet([...seenTasks]),
    repliedTo: trimSet([...repliedTo]),
    myPosts: trimSet(myPosts, 200),
    threadState,
    lastAutopilot: { at: Date.now(), log },
  });

  // ADAPTIVE TIMING: if a live conversation is in flight, check back soon (feels
  // responsive); otherwise the periodic 5-min alarm is enough (no wasted chatter).
  if (activeConversations > 0) chrome.alarms.create("anp2-soon", { delayInMinutes: 1 });
}

function notify(title, message) {
  chrome.notifications.create(`anp2-ap-${Date.now()}`, { type: "basic", iconUrl: "icons/icon128.png", title, message, priority: 1 });
}

chrome.notifications?.onClicked.addListener(() => chrome.action.openPopup?.());
