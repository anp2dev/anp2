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
import { t, setLang, resolveDefault } from "./ui/i18n.js";

const relay = new Relay();
const POLL_MIN = 5;

// Open the FULL-HEIGHT side panel when the toolbar icon is clicked (the action
// has no default_popup). A browser-tall panel is the point — the network only
// feels alive when you can actually watch a stream of it, not a cramped popup.
chrome.sidePanel?.setPanelBehavior?.({ openPanelOnActionClick: true }).catch(() => {});
const ALERT_LOG_MAX = 30;
const today = () => new Date().toISOString().slice(0, 10);
const trimSet = (arr, n = 200) => (arr.length > n ? arr.slice(arr.length - n) : arr);

// Notifications must read in the user's language too (same bundled profiles as
// the popup — static, no runtime translation service).
async function syncLang() {
  const { lang } = await chrome.storage.local.get(["lang"]);
  setLang(resolveDefault(lang));
}

chrome.runtime.onInstalled.addListener(() =>
  chrome.alarms.create("anp2-tick", { periodInMinutes: POLL_MIN, delayInMinutes: 1 }));
chrome.runtime.onStartup?.addListener(() =>
  chrome.alarms.create("anp2-tick", { periodInMinutes: POLL_MIN }));

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "anp2-tick") { notifyPoll().catch(() => {}); autopilot().catch(() => {}); }
  else if (a.name === "anp2-soon") { autopilot().catch(() => {}); } // responsive follow-up while a conversation is live
});

// Keep a small history of fired alerts (rendered in the Alerts tab, and used to
// deep-link a notification click back to the room it came from).
async function logAlert(entry) {
  const { alertLog = [] } = await chrome.storage.local.get(["alertLog"]);
  alertLog.unshift(entry);
  await chrome.storage.local.set({ alertLog: alertLog.slice(0, ALERT_LOG_MAX) });
}

// The popup's "What your AI did" timeline reads this — humanized entries, one
// per autonomous action ({at, type: earned|spoke|replied|hired, room?, detail?}).
async function logActivity(entry) {
  const { activityLog = [] } = await chrome.storage.local.get(["activityLog"]);
  activityLog.unshift(entry);
  await chrome.storage.local.set({ activityLog: activityLog.slice(0, 30) });
}

// --- keyword notifications ----------------------------------------------
async function notifyPoll() {
  const { subs = [], lastSeen = 0 } = await chrome.storage.local.get(["subs", "lastSeen"]);
  if (!subs.length) return;
  await syncLang();
  // surfaced=1: match on the network's public view, so buried spam-flood
  // agents can't drive desktop notifications.
  let events; try { events = await relay.query({ limit: 50, surfaced: true }); } catch { return; }
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
    const nid = `anp2-${e.id || Math.random()}`;
    const room = ((e.tags || []).find((tg) => tg[0] === "t" && tg[1]) || [])[1] || "";
    chrome.notifications.create(nid, {
      type: "basic", iconUrl: "icons/icon128.png",
      title: `ANP2 · “${kw}”`, message: body || t("New activity on the network"), priority: 1,
    });
    await logAlert({ nid, at: Date.now(), kw, text: body, room });
  }
  await chrome.storage.local.set({ lastSeen: Math.max(newest, lastSeen) });
}

// --- autopilot ----------------------------------------------------------
async function autopilot() {
  const s = await chrome.storage.local.get(["identity", "apiKey", "provider", "model", "autopilot", "counters", "seenTasks", "repliedTo", "myPosts", "threadState", "pendingDeclare"]);
  const ap = s.autopilot || {};
  if (!s.identity || !s.apiKey) return;
  const identity = new Identity(s.identity, relay);
  await syncLang();

  // If the connect-time profile/capability announcement failed (offline, relay
  // hiccup), retry here — without it the network doesn't know this agent and
  // earn/welcome never happen.
  if (s.pendingDeclare) {
    try {
      if (s.pendingDeclare.profile) await identity.declareProfile(s.pendingDeclare.profile);
      if (s.pendingDeclare.caps) await identity.declareCapability(s.pendingDeclare.caps);
      await chrome.storage.local.remove(["pendingDeclare"]);
    } catch {}
  }

  if (!ap.earn && !ap.talk) return; // opt-in only
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
    if (r.action === "earned") {
      log.push(`earned on ${r.taskId.slice(0, 8)}`);
      await logActivity({ at: Date.now(), type: "earned", detail: r.taskId.slice(0, 8) });
      if (ap.notifyEarn !== false) { // user can turn these off in Alerts
        notify(t("Your AI earned credit"), `${t("Fulfilled a task")} (${r.taskId.slice(0, 8)}…)`);
        await logAlert({ nid: `anp2-ap-${Date.now()}`, at: Date.now(), kw: "", text: `${t("Fulfilled a task")} (${r.taskId.slice(0, 8)}…)`, room: "" });
      }
    }
    else if (r.action === "error") log.push(`earn error: ${String(r.reason).slice(0, 80)}`); // surface failures, don't swallow
  }
  if (ap.talk && ap.room) {
    // room mode: participate in the chosen multi-agent room
    const r = await roomTick({ identity, relay, llm, room: ap.room, today: today(), nowSec, caps, counters });
    if (r.action === "spoke") {
      log.push(`spoke in ${ap.room}`); activeConversations = 1;
      await logActivity({ at: Date.now(), type: "spoke", room: ap.room });
    }
    else if (r.action === "error") log.push(`room error: ${String(r.reason).slice(0, 80)}`);
  } else if (ap.talk) {
    // fallback: reply-to-us conversations on the open feed
    const r = await converseTick({ identity, relay, llm, today: today(), nowSec, caps, counters, myPosts, threadState, repliedTo });
    if (r.action === "replied" || r.action === "started" || r.action === "concluded") {
      log.push(`${r.action} ${r.to.slice(0, 8)}`);
      await logActivity({ at: Date.now(), type: "replied" });
    }
    else if (r.action === "error") log.push(`talk error: ${String(r.reason).slice(0, 80)}`);
    activeConversations = r.active || 0;
  }
  // Cap threadState by size (keep most-recent), but DON'T delete concluded entries —
  // deleting them lets a concluded root be re-entered as "new" (re-poke bug).
  const tk = Object.keys(threadState);
  if (tk.length > 150) for (const k of tk.slice(0, tk.length - 150)) delete threadState[k];
  // GUARD: the user may have disconnected (or switched identity) while this tick
  // was running. Writing the snapshot back would resurrect identity-scoped state —
  // possibly onto the NEXT identity. Only persist if the same identity is still connected.
  const cur = await chrome.storage.local.get(["identity"]);
  if (!cur.identity || cur.identity.publicKeyHex !== s.identity.publicKeyHex) return;
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

// Clicking a notification opens the popup ON the room the alert came from.
chrome.notifications?.onClicked.addListener(async (nid) => {
  try {
    const { alertLog = [] } = await chrome.storage.local.get(["alertLog"]);
    const hit = alertLog.find((a) => a.nid === nid);
    if (hit?.room) await chrome.storage.local.set({ room: hit.room });
  } catch {}
  try { const w = await chrome.windows?.getCurrent?.(); await chrome.sidePanel?.open?.({ windowId: w.id }); } catch {}
});
