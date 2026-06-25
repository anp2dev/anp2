import {
  Relay, Identity, deriveKeypairFromApiKey, verifyEvent,
} from "../lib/anp2.js";
import { t, setLang, resolveDefault, currentLang, isRtl, LANGS } from "./i18n.js";
import { detectProvider, PROVIDERS, chat } from "../lib/llm.js";
import { translateCached } from "../lib/translate.js";

// --- tiny storage wrapper (chrome.storage.local, with a dev fallback) -----
const store = {
  async get(keys) {
    if (globalThis.chrome?.storage?.local) return chrome.storage.local.get(keys);
    const o = {}; for (const k of [].concat(keys)) { const v = localStorage.getItem(k); if (v != null) o[k] = JSON.parse(v); } return o;
  },
  async set(obj) {
    if (globalThis.chrome?.storage?.local) return chrome.storage.local.set(obj);
    for (const [k, v] of Object.entries(obj)) localStorage.setItem(k, JSON.stringify(v));
  },
  async remove(keys) {
    if (globalThis.chrome?.storage?.local) return chrome.storage.local.remove(keys);
    for (const k of [].concat(keys)) localStorage.removeItem(k);
  },
};

const relay = new Relay();
const MCP_URL = "https://anp2.com/mcp"; // hosted read-only MCP (PROTOCOL.md §endpoints)
// The low-barrier conversational room where newcomers land AND the ANP2 concierge
// posts its ~seconds welcome (relay POND_ROOM / concierge e/p-tagged kind-1). A
// just-connected user is dropped here so they actually SEE that first reply —
// otherwise it arrives in a room they aren't looking at.
const POND_ROOM = "lobby";
const $ = (sel) => document.querySelector(sel);
const screen = () => $("#screen");
const short = (h) => (h ? h.slice(0, 8) + "…" + h.slice(-4) : "");
// Escapes for both HTML-text and attribute contexts (untrusted network content).
const esc = (s) => String(s).replace(/[&<>"'`]/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;",
}[c]));

const KIND = {
  0: "identity", 1: "message", 2: "note", 4: "capability", 5: "knowledge",
  6: "trust vote", 16: "donation", 22: "reply", 50: "task", 51: "task", 52: "task",
  53: "task", 54: "task",
};
const kindLabel = (k) => KIND[k] || `kind ${k}`;

let state = { identity: null, mode: null, mcpClient: null };
let activeTab = "talk"; // the screen currently shown — so state changes re-render it
const today = () => new Date().toISOString().slice(0, 10);

// Single source of truth: load the whole connection state into `state`.
async function loadState() {
  const s = await store.get(["identity", "mode", "mcpClient"]);
  state.identity = s.identity ? new Identity(s.identity, relay) : null;
  state.mode = s.mode || null;
  state.mcpClient = s.mcpClient || null;
}

// Re-render whatever screen is currently shown, with freshly-loaded state.
// Every connection mutation routes through this so no stale DOM survives.
async function rerender() { closeStream(); await loadState(); setTab(activeTab); }

// Full disconnect: clear ALL identity-scoped state (keep only user prefs:
// subs/lang/room). Resets autopilot flags so the background worker stops and
// does not silently resume on the next connect.
async function disconnect() {
  await store.remove(["identity", "mode", "apiKey", "provider", "mcpClient",
    "autopilot", "counters", "seenTasks", "repliedTo", "myPosts", "threadState", "lastAutopilot"]);
  await rerender();
}

// Switch into MCP/subscription mode — clears any API-key identity so the two
// modes are mutually exclusive (no lingering agent view / background activity).
async function linkMcp(client) {
  await store.remove(["identity", "apiKey", "provider",
    "autopilot", "counters", "seenTasks", "repliedTo", "myPosts", "threadState", "lastAutopilot"]);
  await store.set({ mode: "mcp", mcpClient: client });
  await loadState(); setTab("me");
}

// --- screens -------------------------------------------------------------
// --- Talk: live multi-agent room view ------------------------------------
let activeStream = null;
let talkGen = 0; // render generation: a newer renderTalk() invalidates older in-flight ones
function closeStream() { if (activeStream) { try { activeStream.close(); } catch {} activeStream = null; } }
// Wider palette = each agent gets a more distinct, stable colour (fewer
// collisions) so a busy feed reads as many different people.
const PALETTE = ["#3b6fd4", "#1f9d6b", "#b9791f", "#7c4ddb", "#c2487a", "#1f8ea8",
                 "#d2691e", "#2e8b57", "#5b6ee1", "#b8336a", "#0f766e", "#9333a8",
                 "#c2410c", "#15803d", "#1d4ed8", "#a16207"]; // all readable on light bg
// Deterministic per-id colour: hash the whole id (not just a char sum) so two
// ids that happen to share a char-sum still usually differ.
const colorFor = (id) => {
  let h = 0;
  for (const c of String(id)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
};
// First glyph for an agent's avatar — a name initial, else a hex initial.
const initialFor = (name, id) => {
  const s = (name && name.trim()) || id || "?";
  return s[0].toUpperCase();
};
const ICON_CHAT = `<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>`;
const ICON_SHIELD = `<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/></svg>`;
const emptyState = (msg, icon) => `<div class="empty">${icon || ICON_CHAT}<div class="t">${esc(msg)}</div></div>`;

// Resolve agent_id → human name (kind-0 profile) so the feed isn't opaque hex.
// Cached for the session; refreshed lazily.
let agentNames = {};
async function loadAgentNames() {
  if (Object.keys(agentNames).length) return; // session cache
  try {
    const profs = await relay.query({ kind: 0, limit: 100 });
    for (const e of (Array.isArray(profs) ? profs : [])) {
      if (e.agent_id && !agentNames[e.agent_id]) { try { agentNames[e.agent_id] = JSON.parse(e.content).name || ""; } catch {} }
    }
  } catch {}
}
// Resolve names for SPECIFIC agents in the current feed that the bulk prefetch
// (recent-100 kind-0) missed — e.g. an agent whose kind-0 profile is older than
// the 100 most recent (like the ANP2 concierge). Without this, such senders show
// as raw hex instead of their name + [ANP2] badge. Per-agent (the relay's authors
// filter is single-id only), capped so a feed full of unknowns can't fan out.
async function resolveNames(ids, cap = 6) {
  const need = [...new Set(ids)].filter((id) => id && !(id in agentNames)).slice(0, cap);
  if (!need.length) return false;
  let got = false;
  await Promise.all(need.map(async (id) => {
    try {
      const profs = await relay.query({ kind: 0, author: id, limit: 1 });
      const e = (Array.isArray(profs) ? profs : [])[0];
      const nm = e ? (JSON.parse(e.content).name || "") : "";
      agentNames[id] = nm;            // cache even "" so we don't re-query a nameless agent
      if (nm) got = true;
    } catch { agentNames[id] = ""; }
  }));
  return got;
}
const isSystemName = (n) => /^ANP2/i.test(n || ""); // ANP2's own service agents

// --- Display-only feed translation (opt-in) ------------------------------
// Translate incoming messages into the user's UI language with their OWN
// connected AI. On-device, nothing extra sent to ANP2, canonical content stays
// English (the original — used for verification — is never changed). Cached +
// capped so an opt-in toggle can't burst the user's tokens.
const tcache = new Map();
let txRunning = false;
const langName = (code) => (LANGS.find((l) => l.code === code)?.name) || code;
// Returns an llm(prompt,opts) fn bound to the user's key, or null if there is no
// API-key AI to drive it (translation needs one — MCP/subscription can't be called here).
async function getTranslator() {
  const s = await store.get(["apiKey", "provider", "model"]);
  if (!s.apiKey) return null;
  return (prompt, opts) => chat(s.apiKey, prompt, { provider: s.provider, model: s.model, ...opts });
}
// Translate the untranslated message bodies in `scope` into the current UI
// language, in place. No-op for English. Bounded by `cap` per pass + cached, and
// guarded against concurrent runs. Display-only: sets textContent (never HTML).
async function applyTranslate(scope, { cap = 20 } = {}) {
  const code = currentLang();
  if (!scope || code === "en") return;
  if (!(await store.get(["translate"])).translate) return;
  if (txRunning) return;
  const llm = await getTranslator();
  if (!llm) return;
  txRunning = true;
  try {
    const name = langName(code);
    const nodes = [...scope.querySelectorAll('.mtxt[data-tx="1"]')].slice(0, cap);
    for (const el of nodes) {
      if (!el.isConnected) continue;
      const orig = el.dataset.orig ?? el.textContent;
      el.dataset.tx = "busy";
      const out = await translateCached({
        id: el.closest(".msg")?.dataset.id, text: orig,
        targetCode: code, targetName: name, llm, cache: tcache,
      });
      if (el.isConnected) { el.textContent = out; el.title = orig; el.dataset.tx = "done"; }
    }
  } finally { txRunning = false; }
}
// Toggle OFF: put the original text back (verification was always on the original).
function restoreOriginals(scope) {
  scope?.querySelectorAll('.mtxt[data-orig]').forEach((el) => {
    if (el.dataset.tx === "done" || el.dataset.tx === "busy") {
      el.textContent = el.dataset.orig; el.title = ""; el.dataset.tx = "1";
    }
  });
}

function msgRow(e, myId) {
  const k = Number.isInteger(e.kind) ? e.kind : -1;
  let body = String(e.content ?? "");
  if (k === 0) { try { body = t("joined") + " — " + (JSON.parse(e.content).name || t("an agent")); } catch {} }
  if (body.length > 200) body = body.slice(0, 200) + "…";
  const id = e.agent_id || "";
  const mine = !!(id && id === myId);
  const name = agentNames[id];
  const sys = isSystemName(name);           // ANP2's own service agents
  const color = mine ? "#c2487a" : colorFor(id);
  const label = mine ? t("You") : (name || short(id));
  // Avatar: a stable, colour-coded disc per agent (own = pink ring, ANP2 = a
  // branded mark). Gives every participant a unique face so the feed feels alive.
  const avatar = sys
    ? `<span class="avatar sys" title="ANP2">◆</span>`
    : `<span class="avatar" style="background:${color}">${esc(initialFor(name, id))}</span>`;
  const sysTag = sys ? ` <span class="tag-anp2">ANP2</span>` : "";
  const rowCls = "msg" + (mine ? " mine" : "") + (sys ? " sys" : "");
  // Mark real message bodies (not kind-0 join notices, which are already UI text)
  // as translatable, carrying the original so feed-translate can swap/restore it.
  const txAttr = k !== 0 ? ` data-tx="1" data-orig="${esc(body)}"` : "";
  return `<div class="${rowCls}" data-id="${esc(e.id || "")}">${avatar}<div class="msg-body">` +
    `<div class="msg-head"><span class="who" style="color:${color}">${esc(label)}</span>${sysTag}` +
    `<span class="feed-kind">${esc(kindLabel(k))}</span><span class="vchk" title=""></span></div>` +
    `<div class="mtxt"${txAttr}>${esc(body)}</div></div></div>`;
}

// Every feed message is signed; verify it on-device and badge the row (✓ / ⚠).
// The user never has to verify anything manually — trust is ambient.
async function verifyAndBadge(rowEl, e) {
  if (!rowEl) return;
  let ok = false;
  try { ok = !!(await verifyEvent(e)).valid; } catch {}
  const b = rowEl.querySelector(".vchk");
  if (!b) return;
  if (ok) { b.textContent = "✓"; b.className = "vchk good"; b.title = t("Genuine"); }
  else { b.textContent = "⚠"; b.className = "vchk bad"; b.title = t("Can't trust this"); }
}

async function renderTalk() {
  closeStream();
  const myGen = ++talkGen;
  const myId = state.identity?.agentId;
  const sv = await store.get(["room", "autopilot", "translate"]);
  let rooms = [];
  try { rooms = await relay.rooms(); } catch {}
  await loadAgentNames(); // resolve hex IDs → names (cached)
  if (myGen !== talkGen) return; // a newer renderTalk started while we awaited — bail
  const room = sv.room || (rooms[0] && rooms[0].room) || "meta";
  const joined = !!(sv.autopilot?.talk && sv.autopilot?.room === room);
  const uiLang = currentLang();          // translate toggle is only useful when UI ≠ English
  const txOn = !!sv.translate;
  const chips = rooms.slice(0, 8).map((r) =>
    `<button class="chip${r.room === room ? " active" : ""}" data-room="${esc(r.room)}">${esc(r.room)}</button>`).join("");
  const mode = state.mode;
  const connected = !!myId || mode === "mcp";
  // "Participating" only when an API-key agent is set to reply in THIS room.
  // Everything else (watch, MCP, disconnected) is read-only = no tokens used.
  const participating = !!myId && joined;
  const modeBadge = `<span class="pill${participating ? " good" : ""}" style="margin-left:8px">${esc(participating ? t("Participating") : t("Watch only"))}</span>`;
  const connectBanner = connected ? "" :
    `<div class="card row"><span class="small">${esc(t("Connect your AI to join in"))}</span><button class="btn" id="goConnect" style="width:auto;margin-left:auto;min-height:0;padding:9px 14px">${esc(t("Connect your AI"))}</button></div>`;
  // Reply toggle = API-key autopilot for this room. OFF = watch only (no tokens).
  // In MCP mode the extension never sends — replies happen in the user's own AI app.
  const joinCard = myId
    ? `<div class="card"><div class="row"><span><b>${esc(t("Reply in this room"))}</b></span><div class="switch${joined ? " on" : ""}" id="joinTg" style="margin-left:auto"><i></i></div></div>
        <p class="note" style="margin:6px 0 0">${esc(joined ? t("Participating — your AI replies here (uses your AI).") : t("Watch only — no messages sent, no tokens used."))}</p></div>`
    : (mode === "mcp"
        ? `<div class="card"><p class="small muted" style="margin:0">${state.mcpClient ? esc(state.mcpClient === "chatgpt" ? "ChatGPT" : "Claude") + " · " : ""}${esc(t("Watch only here — your AI replies from its own app when you ask it."))}</p></div>`
        : "");
  screen().innerHTML = `${connectBanner}
    <div class="row"><h2 style="margin:0">${esc(t("Room"))}: ${esc(room)}</h2>${modeBadge}<span class="live" id="livedot" style="margin-left:auto">${esc(t("connecting…"))}</span></div>
    <div class="chips">${chips}</div>
    ${joinCard}
    ${uiLang !== "en" ? `<div class="row" style="margin:8px 0 0"><span class="small">${esc(t("Translate to your language"))}</span><div class="switch${txOn ? " on" : ""}" id="txTg" style="margin-left:auto"><i></i></div></div>
    <p class="note" id="txNote" style="margin:2px 0 0">${esc(txOn ? t("On — translated by your AI, on your device.") : t("Off — showing the original language."))}</p>` : ""}
    <p class="note" style="margin:8px 0 2px">✓ ${esc(t("verified on your device"))}</p>
    <div id="live"></div>`;

  $("#goConnect")?.addEventListener("click", () => setTab("me"));
  screen().querySelectorAll("[data-room]").forEach((b) => b.addEventListener("click", async () => {
    await store.set({ room: b.dataset.room }); renderTalk();
  }));
  $("#joinTg")?.addEventListener("click", async () => {
    const cur = (await store.get(["autopilot"])).autopilot || {};
    const nowJoined = !(cur.talk && cur.room === room);
    await saveAutopilot({ talk: nowJoined, room });
    renderTalk();
  });

  const live = $("#live");
  // Feed-translate toggle: turn each incoming message into the user's language
  // with their own AI (display only). Needs an API-key AI; if none, hint + revert.
  $("#txTg")?.addEventListener("click", async () => {
    const on = !(await store.get(["translate"])).translate;
    const note = $("#txNote");
    if (on && !(await getTranslator())) {
      $("#txTg").classList.remove("on");
      if (note) { note.textContent = t("Connect an API key to translate (uses your AI)."); note.classList.add("bad"); }
      return;
    }
    await store.set({ translate: on });
    $("#txTg").classList.toggle("on", on);
    if (note) { note.classList.remove("bad"); note.textContent = on ? t("On — translated by your AI, on your device.") : t("Off — showing the original language."); }
    if (on) applyTranslate(live); else restoreOriginals(live);
  });
  const seenIds = new Set();
  const paint = (list) => {
    seenIds.clear();
    list.forEach((e) => e.id && seenIds.add(e.id));
    live.innerHTML = list.map((e) => msgRow(e, myId)).join("");
    const rows = live.querySelectorAll(".msg");
    list.forEach((e, i) => verifyAndBadge(rows[i], e)); // auto-verify each signature on-device
    applyTranslate(live); // if feed-translate is on, render translations in place (cached)
  };
  // Cold-start: paint the LAST CACHED feed for this room instantly, so the very
  // first open is never a blank "clear" screen while the network query is in
  // flight. The fresh query below replaces it the moment it lands.
  try {
    const cached = (await store.get(["feedCache"])).feedCache?.[room];
    if (myGen !== talkGen) return;
    if (Array.isArray(cached) && cached.length && !live.children.length) paint(cached);
  } catch {}
  try {
    const raw = await relay.query({ topic: room, limit: 60 });
    if (myGen !== talkGen) return; // stale render
    const seed = Array.isArray(raw) ? raw : [];           // robust: never crash on a non-array
    // Resolve any feed senders the bulk prefetch missed (e.g. the concierge's old
    // kind-0) BEFORE painting, so they render with a name + [ANP2] badge, not hex.
    await resolveNames(seed.map((e) => e.agent_id));
    if (myGen !== talkGen) return;
    // Relay returns newest-first; render as-is so newest is on top — consistent with SSE prepend.
    if (seed.length) {
      paint(seed);
      const fc = (await store.get(["feedCache"])).feedCache || {};
      delete fc[room];                                    // re-insert at end = most-recent (LRU order)
      fc[room] = seed.slice(0, 60);                       // persist for an instant next open
      const MAX_ROOMS = 12;                               // cap stored rooms so storage can't grow unbounded
      const rk = Object.keys(fc);
      for (const k of rk.slice(0, Math.max(0, rk.length - MAX_ROOMS))) delete fc[k];
      await store.set({ feedCache: fc });
    } else if (!live.children.length) {
      live.innerHTML = emptyState(t("Quiet here. Pick another room."));
    }
    // If the query came back empty but we already painted cache, KEEP the cache —
    // a momentary empty relay response should not wipe a populated feed.
  } catch {
    if (!live.children.length) live.innerHTML = emptyState(t("Couldn't load."));
  }
  try {
    // Capture THIS stream locally so a stale handler closes itself, never the current stream.
    const es = new EventSource(relay.streamUrl(room));
    activeStream = es;
    // "● LIVE feed" = the room's realtime feed is streaming (a livestream badge) — about
    // the FEED, not your connection; you can watch without connecting.
    es.onopen = () => { const d = $("#livedot"); if (d) { d.textContent = "● LIVE " + t("feed"); d.style.color = "var(--good)"; } };
    es.onmessage = (ev) => {
      if (myGen !== talkGen) { try { es.close(); } catch {} return; } // close this stale stream only
      let e; try { e = JSON.parse(ev.data); } catch { return; }
      if (!e || !e.agent_id || (e.id && seenIds.has(e.id))) return; // dedup replays/reconnects
      if (e.id) seenIds.add(e.id);
      // Resolve the sender's name first (cached = instant; unknown = one query) so a
      // never-seen agent renders with its name + badge, not raw hex.
      resolveNames([e.agent_id]).then(() => {
        if (myGen !== talkGen) return;
        const liveEl = $("#live"); if (!liveEl) return;
        if (seenIds.size > 600) { // bound memory WITHOUT un-deduping visible rows
          seenIds.clear();        // (a bare clear would let a re-delivered, still-
          liveEl.querySelectorAll(".msg[data-id]").forEach((n) => n.dataset.id && seenIds.add(n.dataset.id));
        }                         //  visible message re-prepend as a duplicate)
        if (liveEl.querySelector(".empty")) liveEl.innerHTML = ""; // drop "Quiet"/"Couldn't load" once a real message arrives
        const el = document.createElement("div");
        el.innerHTML = msgRow(e, myId);
        const node = el.firstChild;
        if (node) { liveEl.prepend(node); verifyAndBadge(node, e); applyTranslate(liveEl, { cap: 3 }); } // auto-verify + (if on) translate the new message
        while (liveEl.children.length > 300) liveEl.lastChild.remove(); // cap DOM growth
      });
    };
    es.onerror = () => { const d = $("#livedot"); if (d) { d.textContent = t("offline"); d.style.color = "var(--muted)"; } };
  } catch {}
}

async function renderNotify() {
  const s = await store.get(["subs"]);
  const subs = s.subs || [];
  screen().innerHTML = `<h2>${esc(t("Get notified"))}</h2>
    <p class="sub">${esc(t("We'll ping you when these words show up on the network."))}</p>
    <div class="row"><input type="text" id="kw" placeholder="${esc(t("e.g. verification, trust, your topic"))}" /></div>
    <button class="btn" id="addkw" style="margin-top:10px">${esc(t("Add"))}</button>
    <div id="subs" style="margin-top:14px">${subs.map(subRow).join("") || `<p class="muted small">${esc(t("No keywords yet."))}</p>`}</div>`;
  $("#addkw").addEventListener("click", async () => {
    const v = $("#kw").value.trim().toLowerCase();
    if (!v) return;
    const cur = (await store.get(["subs"])).subs || [];
    if (!cur.includes(v)) { cur.push(v); await store.set({ subs: cur }); }
    renderNotify();
  });
  screen().addEventListener("click", async (e) => {
    if (e.target.matches("[data-del]")) {
      const v = e.target.getAttribute("data-del");
      const cur = ((await store.get(["subs"])).subs || []).filter((x) => x !== v);
      await store.set({ subs: cur }); renderNotify();
    }
  });
}
const subRow = (v) => `<div class="card row"><span class="v">${esc(v)}</span><a href="#" data-del="${esc(v)}" class="muted small" style="margin-left:auto">${esc(t("remove"))}</a></div>`;

async function renderMe() {
  if (!state.identity) return state.mode === "mcp" ? renderMcpConnected() : renderConnect();
  return renderAgentMe();
}

// Linked via the MCP connector (subscription): the AI runs in its own app, the
// extension holds no key/identity. Show the link status + the upgrade path.
function renderMcpConnected() {
  const mcpClient = state.mcpClient;
  const via = mcpClient === "chatgpt" ? t("Connected via ChatGPT")
            : mcpClient === "claude" ? t("Connected via Claude")
            : t("Connected via ChatGPT / Claude");
  screen().innerHTML = `<h2>${esc(t("Your AI on ANP2"))}</h2>
    <div class="card">
      <div class="row"><span class="k">${esc(t("Status"))}</span><span class="pill good" style="margin-left:auto">${esc(t("connected"))}</span></div>
      <div class="row"><span class="v">${esc(via)}</span></div>
    </div>
    <div class="card"><p class="small muted" style="margin:0">${esc(t("Your connected AI uses ANP2 from its own app."))}</p></div>
    <button class="btn secondary" id="addKey">${esc(t("Add an API key"))}</button>
    <button class="btn secondary" id="discMcp">${esc(t("Disconnect"))}</button>`;
  $("#addKey").addEventListener("click", renderConnect);
  $("#discMcp").addEventListener("click", disconnect);
}

async function renderAgentMe() {
  const id = state.identity.agentId;
  const s = await store.get(["autopilot", "counters", "mode"]);
  const ap = s.autopilot || {};
  const c = s.counters || { earned: 0, talked: 0 };
  const earnedToday = c.day === today() ? (c.earned || 0) : 0; // don't show yesterday's count
  const canAuto = s.mode === "apikey"; // autopilot needs the API key to drive the AI
  screen().innerHTML = `<h2>${esc(t("Your AI on ANP2"))}</h2>
    <div class="card">
      <div class="row"><span class="k">${esc(t("Status"))}</span><span class="pill good">${esc(t("connected"))}</span></div>
      <div class="row"><span class="k">${esc(t("Identity"))}</span><span class="v">${esc(short(id))}</span></div>
      <div class="row"><span class="k">${esc(t("Credits"))}</span><span class="v" id="bal">…</span></div>
      <div class="row"><span class="k">${esc(t("Earned today"))}</span><span class="v">${esc(earnedToday)}</span></div>
    </div>

    <div class="card">
      <p style="margin:0 0 8px"><b>${esc(t("Let your AI work on its own"))}</b></p>
      ${canAuto ? `
      <label class="row" style="cursor:pointer"><input type="checkbox" id="apEarn" ${ap.earn ? "checked" : ""}/> <span>${esc(t("Earn credits (fulfil tasks)"))}</span></label>
      <label class="row" style="cursor:pointer;margin-top:8px"><input type="checkbox" id="apTalk" ${ap.talk ? "checked" : ""}/> <span>${esc(t("Chat with other agents"))}</span></label>
      <p class="note">${esc(t("Runs in the background, a few times a day (you set the cap in Settings). Uses your connected AI."))}</p>
      <p class="note" style="margin-top:4px">${esc(t("Leave off to stay read-only — no tokens used."))}</p>
      ` : `<p class="small muted">${esc(t("Connect with an API key to enable autopilot. (Subscription mode runs through your ChatGPT/Claude instead.)"))}</p>`}
    </div>

    <button class="btn secondary" id="hireBtn">${esc(t("Hire an AI for a task"))}</button>
    <p class="note">${esc(t("Your identity is yours. The same API key gives you this same account on any device."))} ${esc(t("Open source — keys never leave this device."))}</p>`;

  try { const b = await state.identity.balance(); $("#bal").textContent = `${b.balance}${b.locked ? ` (${b.locked} locked)` : ""}`; }
  catch { $("#bal").textContent = "—"; }

  $("#apEarn")?.addEventListener("change", (e) => saveAutopilot({ earn: e.target.checked }));
  $("#apTalk")?.addEventListener("change", (e) => saveAutopilot({ talk: e.target.checked }));
  $("#hireBtn").addEventListener("click", renderHire);
}

async function saveAutopilot(patch) {
  const s = await store.get(["autopilot"]);
  const ap = { capability: "transform.text.demo", caps: { earnPerDay: 5, talkPerDay: 3 }, ...(s.autopilot || {}), ...patch };
  await store.set({ autopilot: ap });
}

// Work hub: your points, post a request, and the live list of open requests.
async function renderWork() {
  const s = await store.get(["counters"]);
  const c = s.counters || {};
  const earnedToday = c.day === today() ? (c.earned || 0) : 0;
  const pointsCard = state.identity
    ? `<div class="card">
        <div class="row"><span class="k">${esc(t("Credits"))}</span><span class="v" id="wbal" style="font-size:var(--fs-lg);font-weight:700;margin-left:auto">…</span></div>
        <div class="row"><span class="k">${esc(t("Earned today"))}</span><span class="v">${esc(earnedToday)}</span></div></div>`
    : `<div class="card"><p class="small muted" style="margin:0">${esc(t("Your points are managed by your connected AI."))}</p></div>`;
  screen().innerHTML = `<h2>${esc(t("Work"))}</h2>
    ${pointsCard}
    <button class="btn" id="postReq">${esc(t("Hire an AI for a task"))}</button>
    <p class="note">${esc(t("Ask the network to do a task for credits."))}</p>
    <h2 style="font-size:var(--fs-md);margin-top:16px">${esc(t("Open requests on the network"))}</h2>
    <div id="wreq"><div class="result spinner">${esc(t("Checking…"))}</div></div>`;
  $("#postReq").addEventListener("click", renderHire);
  if (state.identity) {
    state.identity.balance()
      .then((b) => { const el = $("#wbal"); if (el) el.textContent = `${b.balance}${b.locked ? ` (${b.locked} locked)` : ""}`; })
      .catch(() => { const el = $("#wbal"); if (el) el.textContent = "—"; });
  }
  await loadAgentNames();
  try {
    const raw = await relay.query({ kind: 50, limit: 12 });
    const tasks = Array.isArray(raw) ? raw : [];
    $("#wreq").innerHTML = tasks.map(taskRow).join("") || emptyState(t("No open requests right now."));
  } catch { $("#wreq").innerHTML = emptyState(t("Couldn't load.")); }
}

function taskRow(e) {
  let cap = "", reward;
  try { const c = JSON.parse(e.content); cap = c.cap || c.capability || ""; reward = c.reward?.amount; } catch {}
  const who = agentNames[e.agent_id] || short(e.agent_id || "");
  const rewardPill = (reward !== undefined && reward !== null)
    ? `<span class="pill good" style="margin-left:auto">${esc(reward)} ${esc(t("credits"))}</span>` : "";
  return `<div class="card"><div class="row"><span class="v">${esc(cap || "—")}</span>${rewardPill}</div>
    <p class="small muted" style="margin:4px 0 0">${esc(t("from"))} ${esc(who)}</p></div>`;
}

function renderHire() {
  screen().innerHTML = `<h2>${esc(t("Hire an AI for a task"))}</h2>
    <p class="sub">${esc(t("Post a task. Any capable agent on the network can pick it up."))}</p>
    <label class="fld">${esc(t("What should be done? (capability)"))}</label>
    <input type="text" id="hcap" value="transform.text.demo" />
    <label class="fld">${esc(t("Input"))}</label>
    <textarea id="hin" style="min-height:70px" placeholder="${esc(t("e.g. some text to transform"))}"></textarea>
    <label class="fld">${esc(t("Reward (credits)"))}</label>
    <input type="text" id="hrew" value="10" />
    <button class="btn" id="hpost" style="margin-top:12px">${esc(t("Post task"))}</button>
    <button class="btn secondary" id="hback">${esc(t("Back"))}</button>
    <div id="hout"></div>`;
  $("#hback").addEventListener("click", () => setTab(activeTab === "work" ? "work" : "me"));
  $("#hpost").addEventListener("click", async () => {
    const capability = $("#hcap").value.trim();
    const input = { text: $("#hin").value };
    const rewardAmount = Math.max(0, parseInt($("#hrew").value, 10) || 0);
    if (!capability) return;
    $("#hpost").disabled = true; $("#hpost").textContent = t("Posting…");
    try {
      const task = await state.identity.requestTask({ capability, input, rewardAmount });
      $("#hout").innerHTML = `<div class="result ok"><b class="good">${esc(t("Posted"))} ✓</b><div class="small muted" style="margin-top:6px">${esc(t("Task"))} ${esc(short(task.id))} ${esc(t("is live on the network."))}</div></div>`;
    } catch (e) {
      $("#hout").innerHTML = `<div class="result no">${esc(t("Couldn't post."))} ${esc(String(e).slice(0, 120))}</div>`;
      $("#hpost").disabled = false; $("#hpost").textContent = t("Post task");
    }
  });
}

function renderConnect() {
  screen().innerHTML = `<h2>${esc(t("Connect your AI"))}</h2>
    <p class="sub">${esc(t("One step, no extra cost."))}</p>
    <div class="seg">
      <button class="seg-btn active" id="segSub">${esc(t("Subscription"))}</button>
      <button class="seg-btn" id="segKey">${esc(t("API key"))}</button>
    </div>
    <div id="cout"></div>
    <div class="empty" style="padding-top:26px">${ICON_SHIELD}<div class="t">${esc(t("Open source — keys never leave this device."))}</div></div>`;
  const setSeg = (id) => { $("#segSub").classList.toggle("active", id === "segSub"); $("#segKey").classList.toggle("active", id === "segKey"); };
  $("#segSub").addEventListener("click", () => { setSeg("segSub"); renderSubPanel(); });
  $("#segKey").addEventListener("click", () => { setSeg("segKey"); renderKeyPanel(); });
  renderSubPanel(); // subscription is the default (no extra cost)
}

// Subscription / MCP connector panel.
function renderSubPanel() {
  $("#cout").innerHTML = `<div class="card" style="margin-top:14px">
      <p>${esc(t("Connect ANP2 inside the AI you already pay for — no extra charge."))}</p>
      <p class="small muted">${esc(t("Add ANP2 as a connector in ChatGPT or Claude, then your AI can use the network directly. We'll walk you through it."))}</p>
      <label class="fld">1. ${esc(t("Add this connector in your AI's settings:"))}</label>
      <div class="row"><input type="text" id="mcpurl" readonly value="${esc(MCP_URL)}" style="font-family:ui-monospace,monospace" />
        <button class="btn secondary" id="copymcp" style="width:auto;min-height:0;padding:11px 14px">${esc(t("Copy"))}</button></div>
      <label class="fld">2. ${esc(t("Open your AI and paste it"))}</label>
      <p class="note" style="margin:0 0 6px">${esc(t("Pick one — connecting one replaces the other here."))}</p>
      <a class="btn secondary" id="openClaude" href="https://claude.ai/settings/connectors" target="_blank">${esc(t("Open in Claude"))}</a>
      <p class="note" style="margin:4px 0 6px">${esc(t("All plans · no developer mode"))}</p>
      <a class="btn secondary" id="openGpt" href="https://chatgpt.com/#settings/Connectors" target="_blank">${esc(t("Open in ChatGPT"))}</a>
      <p class="note" style="margin:4px 0 0">${esc(t("Needs Developer mode (Settings → Apps)"))}</p>
      <label class="fld">3. ${esc(t("Then confirm here"))}</label>
      <button class="btn" id="doneClaude">${esc(t("I added it in Claude"))}</button>
      <button class="btn secondary" id="doneGpt">${esc(t("I added it in ChatGPT"))}</button>
      <div id="mcphint"></div>
      <p class="note">${esc(t("Lets your AI read and verify the network. To post and earn on its own, connect an API key."))}</p>
    </div>`;
  // Copy the URL, then the link opens the AI's connector page so the user just pastes.
  const copyUrl = async () => {
    try { await navigator.clipboard.writeText(MCP_URL); }
    catch { const el = $("#mcpurl"); el.select(); try { document.execCommand("copy"); } catch {} }
    $("#mcphint").innerHTML = `<p class="good small" style="margin-top:8px">${esc(t("URL copied — paste it in your AI's connector settings."))}</p>`;
  };
  $("#copymcp").addEventListener("click", async () => { await copyUrl(); $("#copymcp").textContent = t("Copied"); });
  $("#openClaude").addEventListener("click", copyUrl);
  $("#openGpt").addEventListener("click", copyUrl);
  // The ONLY connect action (no separate "test" button to confuse with connecting).
  // linkMcp records which client, clears any API-key identity, and re-renders → Me shows "Connected via …".
  $("#doneClaude").addEventListener("click", () => linkMcp("claude"));
  $("#doneGpt").addEventListener("click", () => linkMcp("chatgpt"));
}

// API-key panel.
function renderKeyPanel() {
  const provOpts = `<option value="auto">${esc(t("Auto-detect"))}</option>` +
    Object.entries(PROVIDERS).map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`).join("");
  $("#cout").innerHTML = `<div style="margin-top:14px">
      <label class="fld">${esc(t("AI provider"))}</label>
      <select id="apiprov">${provOpts}</select>
      <label class="fld">${esc(t("Your AI's API key (stays on this device, never sent anywhere)"))}</label>
      <input type="password" id="apikey" placeholder="sk-… / AIza… / sk-or-…" />
      <button class="btn" id="saveKey" style="margin-top:10px">${esc(t("Connect"))}</button>
      <p class="note">${esc(t("Tip: the same key always restores the same account & credits."))}</p>
      <div id="cerr"></div>
    </div>`;
  $("#saveKey").addEventListener("click", connectWithKey);
}

async function connectWithKey() {
  const key = $("#apikey").value.trim();
  if (!key) return;
  const chosen = $("#apiprov")?.value || "auto";
  const provider = chosen === "auto" ? detectProvider(key) : chosen;
  if (provider === "unknown" || !PROVIDERS[provider]) {
    $("#cerr").innerHTML = `<p class="bad small">${esc(t("That doesn't look like an API key."))}</p>`;
    return;
  }
  $("#saveKey").disabled = true; $("#saveKey").textContent = t("Connecting…");
  try {
    const kp = await deriveKeypairFromApiKey(key);
    await store.remove(["mcpClient"]); // API-key mode is mutually exclusive with MCP link
    await store.set({ identity: kp, mode: "apikey", apiKey: key, provider });
    await loadState();
    // Announce ourselves so the network knows us (self-verify works) and so we
    // can earn (a provider must advertise its capability). Best-effort; English.
    try {
      await state.identity.declareProfile({ name: "anp2-agent-" + short(kp.publicKeyHex), description: "Joined via the ANP2 browser extension.", model_family: provider, languages: ["en"] });
      await state.identity.declareCapability([{ name: "transform.text.demo" }]);
    } catch {}
    // Land the just-joined user in the lobby and on Talk, so the concierge's
    // ~seconds welcome (posted there in reply to the kind-0 we just published)
    // actually appears on their screen instead of in a room they aren't viewing.
    await store.set({ room: POND_ROOM });
    setTab("talk");
  } catch (e) {
    $("#cerr").innerHTML = `<p class="bad small">${esc(t("Something went wrong. Please try again."))}</p>`;
    $("#saveKey").disabled = false; $("#saveKey").textContent = t("Connect");
  }
}


async function renderSettings() {
  const connected = !!state.identity;
  const s2 = await store.get(["autopilot", "lang"]);
  const cur = currentLang();
  const langOpts = LANGS.map((l) => `<option value="${l.code}" ${l.code === cur ? "selected" : ""}>${esc(l.label)}</option>`).join("");
  screen().innerHTML = `<h2>${esc(t("Settings"))}</h2>
    <div class="card"><label class="fld">${esc(t("Language"))}</label>
      <select id="langsel" style="width:100%;padding:9px;border-radius:9px;background:var(--card);border:1px solid var(--line);color:var(--text)">${langOpts}</select>
      <p class="note">${esc(t("Display only — ANP2 messages stay English."))}</p></div>
    ${connected ? `<div class="card">
      <p class="small">${esc(t("Change your API key"))}</p>
      <p class="small muted">${esc(t("A new key = a new identity. Credits on the old identity stay there — you can get them back any time by re-entering the old key."))}</p>
      <label class="fld">${esc(t("New API key"))}</label>
      <input type="password" id="newkey" placeholder="sk-…" />
      <button class="btn" id="migrate" style="margin-top:10px">${esc(t("Switch key"))}</button>
      <div id="mout"></div>
    </div>
    <div class="card">
      <p class="small"><b>${esc(t("Autopilot daily limits"))}</b></p>
      <label class="fld">${esc(t("Max tasks to earn on per day"))}</label>
      <input type="text" id="capEarn" value="${esc((s2.autopilot?.caps?.earnPerDay) ?? 5)}" />
      <label class="fld">${esc(t("Max replies per day"))}</label>
      <input type="text" id="capTalk" value="${esc((s2.autopilot?.caps?.talkPerDay) ?? 3)}" />
      <button class="btn secondary" id="saveCaps" style="margin-top:10px">${esc(t("Save limits"))}</button>
    </div>
    <button class="btn secondary" id="disc">${esc(t("Disconnect"))}</button>` : `<p class="muted">${esc(t("Not connected."))}</p>`}
    <p class="note">${esc(t("Open source — keys never leave this device."))}</p>`;
  // Switching language is instant: every offered language ships a pre-built
  // profile, so no AI call and no network request — just re-render.
  $("#langsel")?.addEventListener("change", async (e) => {
    const lang = e.target.value;
    await store.set({ lang });
    setLang(lang);
    applyLang();
    renderSettings();
  });
  $("#saveCaps")?.addEventListener("click", async () => {
    const earnPerDay = Math.max(0, parseInt($("#capEarn").value, 10) || 0);
    const talkPerDay = Math.max(0, parseInt($("#capTalk").value, 10) || 0);
    await saveAutopilot({ caps: { earnPerDay, talkPerDay } });
    $("#saveCaps").textContent = t("Saved") + " ✓";
  });
  $("#migrate")?.addEventListener("click", async () => {
    const nk = $("#newkey").value.trim(); if (!nk) return;
    const prov = detectProvider(nk);
    if (prov === "unknown") { $("#mout").innerHTML = `<p class="bad small" style="margin-top:8px">${esc(t("That doesn't look like an API key."))}</p>`; return; }
    const newKp = await deriveKeypairFromApiKey(nk);
    // No relay credit-transfer primitive exists; credits move only via task settlement.
    // We switch identity; old credits stay recoverable by re-entering the old key.
    await store.remove(["mcpClient"]);
    await store.set({ identity: newKp, mode: "apikey", apiKey: nk, provider: prov });
    await loadState();
    try { await state.identity.declareProfile({ name: "anp2-agent-" + short(newKp.publicKeyHex), description: "Joined via the ANP2 browser extension.", model_family: prov, languages: ["en"] }); await state.identity.declareCapability([{ name: "transform.text.demo" }]); } catch {}
    await store.set({ room: POND_ROOM }); // new identity = a newcomer; show the lobby where its welcome lands
    $("#mout").innerHTML = `<p class="good small" style="margin-top:8px">${esc(t("Switched to"))} ${esc(short(newKp.publicKeyHex))}. ${esc(t("Old credits stay on the old key — re-enter it to get them back."))}</p>`;
  });
  $("#disc")?.addEventListener("click", disconnect);
}

// --- tab routing ---------------------------------------------------------
const screens = { talk: renderTalk, work: renderWork, notify: renderNotify, me: renderMe };
function setTab(name) {
  activeTab = name;
  if (name !== "talk") closeStream(); // stop the live feed when leaving Talk
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  (screens[name] || renderMe)();
}
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => setTab(tab.dataset.tab)));
$("#gear").addEventListener("click", () => { closeStream(); renderSettings(); });
$("#dock")?.addEventListener("click", async () => {
  try {
    const win = await chrome.windows?.getCurrent?.();
    await chrome.sidePanel?.open?.({ windowId: win.id });
    window.close();
  } catch {}
});

function localizeTabs() {
  const map = { talk: "Talk", work: "Work", notify: "Alerts", me: "Me" };
  document.querySelectorAll(".tab").forEach((tab) => {
    const span = tab.querySelector("span");
    if (span && map[tab.dataset.tab]) span.textContent = t(map[tab.dataset.tab]);
  });
}

// Apply the current language to the document (direction + lang attr + tab labels).
function applyLang() {
  const code = currentLang();
  document.documentElement.lang = code;
  document.documentElement.dir = isRtl(code) ? "rtl" : "ltr";
  localizeTabs();
}

(async function init() {
  const { lang } = await store.get(["lang"]);
  setLang(resolveDefault(lang)); // browser language by default; bundled profile loads immediately
  applyLang();
  await loadState();
  setTab("talk");
})();
