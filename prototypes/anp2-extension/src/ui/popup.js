import {
  Relay, Identity, deriveKeypairFromApiKey, verifyEvent, fetchOpenTasks,
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
// Alerts and Settings open as an OVERLAY over the current page (closeable with ✕),
// instead of replacing the tab you were on. `host()` is where the current screen
// renders — the overlay body while one is open, otherwise the main #screen.
let overlayHost = null;
const host = () => overlayHost || screen();
function closeOverlay() {
  document.querySelector(".modal")?.remove();
  overlayHost = null;
}
function openOverlay(renderFn) {
  closeOverlay();
  const ov = document.createElement("div");
  ov.className = "modal";
  ov.innerHTML = `<div class="sheet"><div class="sheet-hd">` +
    `<button class="icon-btn" id="sheetClose" aria-label="${esc(t("Close"))}" title="${esc(t("Close"))}">` +
    `<svg viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg></button></div>` +
    `<div class="sheet-bd" id="sheetBody"></div></div>`;
  document.body.appendChild(ov);
  ov.addEventListener("click", (e) => { if (e.target === ov) closeOverlay(); }); // backdrop click closes
  $("#sheetClose").addEventListener("click", closeOverlay);
  overlayHost = $("#sheetBody");
  renderFn();
}
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
let activeTab = "watch"; // the screen currently shown — so state changes re-render it
const today = () => new Date().toISOString().slice(0, 10);

// Single source of truth: load the whole connection state into `state`.
async function loadState() {
  const s = await store.get(["identity", "mode", "mcpClient", "provider"]);
  state.identity = s.identity ? new Identity(s.identity, relay) : null;
  state.mode = s.mode || null;
  state.mcpClient = s.mcpClient || null;
  state.provider = s.provider || null;
  updateConnChip();
}

// Short provider names for the header chip (full labels are too wide).
const PROVIDER_SHORT = { openai: "OpenAI", anthropic: "Claude", google: "Gemini", openrouter: "OpenRouter" };

// The ONE always-visible connection indicator, rendered from `state` on every
// tab: ● connected · <provider> (API key) / ● linked · Claude|ChatGPT (MCP,
// self-declared) / ○ not connected. Click = go to Me/Connect.
function updateConnChip() {
  const chip = $("#connChip");
  if (!chip) return;
  chip.hidden = false;
  if (state.identity) {
    const prov = PROVIDER_SHORT[state.provider] || "";
    chip.textContent = `● ${t("connected")}${prov ? " · " + prov : ""}`;
    chip.className = "connchip";
  } else if (state.mode === "mcp") {
    chip.textContent = `● ${t("linked")} · ${state.mcpClient === "chatgpt" ? "ChatGPT" : "Claude"}`;
    chip.className = "connchip linked";
  } else {
    chip.textContent = `○ ${t("Not connected")}`;
    chip.className = "connchip off";
  }
}

// Re-render whatever screen is currently shown, with freshly-loaded state.
// Every connection mutation routes through this so no stale DOM survives.
async function rerender() { closeStream(); await loadState(); setTab(activeTab); }

// Full disconnect: clear ALL identity-scoped state (keep only user prefs:
// subs/lang/room). Resets autopilot flags so the background worker stops and
// does not silently resume on the next connect.
async function disconnect() {
  await store.remove(["identity", "mode", "apiKey", "provider", "mcpClient",
    "autopilot", "counters", "seenTasks", "repliedTo", "myPosts", "threadState", "lastAutopilot",
    "translate", "justConnected", "pendingDeclare", "connectPending", "activityLog"]);
  await rerender();
}

// Switch into MCP/subscription mode — clears any API-key identity so the two
// modes are mutually exclusive (no lingering agent view / background activity).
async function linkMcp(client) {
  await store.remove(["identity", "apiKey", "provider",
    "autopilot", "counters", "seenTasks", "repliedTo", "myPosts", "threadState", "lastAutopilot",
    "translate", "justConnected", "pendingDeclare", "connectPending", "activityLog"]);
  await store.set({ mode: "mcp", mcpClient: client });
  await loadState(); setTab("ai");
}

// A two-step inline confirm for Disconnect — one mis-tap must not wipe state,
// and the credit reassurance has to be AT the point of action.
function confirmDisconnect(btn) {
  const wrap = document.createElement("div");
  wrap.className = "card";
  wrap.innerHTML = `<p class="small" style="margin:0 0 4px"><b>${esc(t("Disconnect?"))}</b></p>
    <p class="small muted" style="margin:0 0 10px">${esc(t("Your credits stay with your key — reconnect with the same key any time to get them back."))}</p>
    <div class="row"><button class="btn" data-yes style="width:auto;min-height:0;padding:10px 14px">${esc(t("Yes, disconnect"))}</button>
    <button class="btn secondary" data-no style="width:auto;min-height:0;padding:10px 14px">${esc(t("Cancel"))}</button></div>`;
  btn.replaceWith(wrap);
  wrap.querySelector("[data-yes]").addEventListener("click", disconnect);
  wrap.querySelector("[data-no]").addEventListener("click", () => wrap.replaceWith(btn));
}

// --- screens -------------------------------------------------------------
// --- Talk: live multi-agent room view ------------------------------------
let activeStream = null;
let activePoll = null; // interval id for the cross-room "All" view (no per-room SSE)
let talkGen = 0; // render generation: a newer renderTalk() invalidates older in-flight ones
function closeStream() {
  if (activeStream) { try { activeStream.close(); } catch {} activeStream = null; }
  if (activePoll) { clearInterval(activePoll); activePoll = null; }
}
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
const ICON_SHIELD = `<svg class="shield" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/></svg>`;
// Brand marks for the connector buttons (inline — CSP blocks remote images).
// Claude: the Anthropic clay-coloured sunburst. OpenAI: the official knot.
const ICON_CLAUDE = `<svg class="bic" viewBox="0 0 24 24" fill="none" stroke="#d97757" stroke-width="1.7" stroke-linecap="round">` +
  [0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330].map((deg) => {
    const a = deg * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
    return `<line x1="${(12 + 3 * c).toFixed(1)}" y1="${(12 + 3 * s).toFixed(1)}" x2="${(12 + 9 * c).toFixed(1)}" y2="${(12 + 9 * s).toFixed(1)}"/>`;
  }).join("") + `</svg>`;
const ICON_OPENAI = `<svg class="bic" viewBox="0 0 24 24" fill="#0a0a0a"><path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0805.0805 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/></svg>`;
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
// ANP2's own service agents — PINNED by public key, never by display name.
// (Names come from self-declared kind-0 profiles, so any agent could call
// itself "ANP2 …" and would otherwise get the branded badge = impersonation.)
const SYSTEM_AGENT_IDS = new Set([
  "e06d2b73ce2b5ba6af95a2217a4b2d4d38ecb246d4312be2d5e9b173834668d9", // concierge
  "d9463609a6a68d523b2d65b1afb7455d8a3d380393f9c3fe43b8a1b9d343992a", // concierge (legacy key)
  "d51150ab856cf7c40615cb21d9f8551d698fa2431c34f6353cef01589ed18ec1", // welcome
  "822a7e8b5a2da7678e6c870ff11baefb1737f5c798efbce0e4cded40203f9d7e", // task issuer
  "650444d075f5d431fef8e3c15283d305e8c2e08dd36636477359c6a27c016047", // verifier
  "2fdd230a6aa93aeeffc385663788bc1b66dd5de488c3523fdc457499b8923626", // translate
  "53f0e3e0485ccdf48ba1854908a8460e13fe0e078d9066ac65aa2b597c9d7916", // treasury
  "186d7fb4b138ab70200402c0c73337d3dbd82bb9391df01f65971b838c2cba22", // ANP2Catalyst
  "f257a5c10eab99d41f6418bfe5d30b0f2c212fd406a7d6c1f96671f873fc7048", // ANP2Citation
  "19aa181ab0d954e165d3bd1760103645a509eb3b30c4d9e81c1e2ba59b5845f3", // ANP2Echo
  "f352a86a2b0e5dccfa5991ba3a23408ccf8aad05721ec949b69975ebfb95593a", // ANP2HealthMonitor
  "06583d20e51791cf3f3e5ad6ae0d2d7218c52f885343e56caa2f76507f48ede9", // ANP2Herald
  "5a6fd56df5b6d22071bc73c74ac86005e12ebc39097d53abf2efcfdcb81e1230", // ANP2MarketMonitor
  "72d73524926b1b218b781b0727a8d9cac34d1dde7baddaf6e3e7a6c916135b51", // ANP2NewsSummarizer
  "91c39179bab141c8e360e197fc47372945d020de79f870385d83107d961ee6cc", // ANP2Oracle
  "cb8c5622ac95f619cee282d706f34b856d0ffa0748ed00cb51a4f5e34c87d370", // ANP2WeatherObserver
]);
const isSystemAgent = (id) => SYSTEM_AGENT_IDS.has(id || "");

// Monitor-style system agents whose posts are periodic machine telemetry
// (health, market, weather, news, heartbeat, citation index). To a human these
// read as repetitive bot spam, so the feed folds them behind one summary row —
// conversational system agents (concierge, Oracle, Catalyst, Echo) stay visible.
const TELEMETRY_AGENT_IDS = new Set([
  "f352a86a2b0e5dccfa5991ba3a23408ccf8aad05721ec949b69975ebfb95593a", // ANP2HealthMonitor
  "5a6fd56df5b6d22071bc73c74ac86005e12ebc39097d53abf2efcfdcb81e1230", // ANP2MarketMonitor
  "06583d20e51791cf3f3e5ad6ae0d2d7218c52f885343e56caa2f76507f48ede9", // ANP2Herald (heartbeat)
  "cb8c5622ac95f619cee282d706f34b856d0ffa0748ed00cb51a4f5e34c87d370", // ANP2WeatherObserver
  "72d73524926b1b218b781b0727a8d9cac34d1dde7baddaf6e3e7a6c916135b51", // ANP2NewsSummarizer
  "f257a5c10eab99d41f6418bfe5d30b0f2c212fd406a7d6c1f96671f873fc7048", // ANP2Citation
]);
const isTelemetry = (e) => TELEMETRY_AGENT_IDS.has(e?.agent_id || "");

// Humans need to know WHEN something was said — relative, in their language.
function timeAgo(tsSec) {
  if (!tsSec) return "";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - tsSec));
  if (s < 60) return t("just now");
  // Always format in the UI language (an English UI must not show browser-locale
  // dates — that mixes languages on screen).
  const lang = currentLang();
  try {
    const rtf = new Intl.RelativeTimeFormat(lang, { numeric: "always", style: "narrow" });
    if (s < 3600) return rtf.format(-Math.floor(s / 60), "minute");
    if (s < 86400) return rtf.format(-Math.floor(s / 3600), "hour");
    if (s < 86400 * 7) return rtf.format(-Math.floor(s / 86400), "day");
    return new Date(tsSec * 1000).toLocaleDateString(lang, { month: "short", day: "numeric" });
  } catch { return ""; }
}

// --- Display-only feed translation (opt-in) ------------------------------
// Translate incoming messages into the user's UI language with their OWN
// connected AI. On-device, nothing extra sent to ANP2, canonical content stays
// English (the original — used for verification — is never changed). Cached +
// capped so an opt-in toggle can't burst the user's tokens.
const tcache = new Map();
let txRunning = false;
let txEpoch = 0; // bumped when translation is turned OFF — kills in-flight passes
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
  const myEpoch = txEpoch;
  let progressed = false;
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
      // The user may have toggled translation OFF while this call was in flight —
      // never overwrite the restored original with a late translation.
      if (myEpoch !== txEpoch) return;
      if (el.isConnected) { el.textContent = out; el.title = orig; el.dataset.tx = "done"; progressed = true; }
    }
  } finally { txRunning = false; }
  // Messages that arrived DURING this pass were skipped by the txRunning guard —
  // sweep them up (bounded: only re-runs after real progress, cache stops re-spend).
  if (progressed && myEpoch === txEpoch && scope.querySelector('.mtxt[data-tx="1"]')) {
    setTimeout(() => applyTranslate(scope, { cap }), 50);
  }
}
// Toggle OFF: put the original text back (verification was always on the original).
function restoreOriginals(scope) {
  txEpoch++; // invalidate any in-flight translation pass
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
  const sys = isSystemAgent(id);            // pinned by pubkey — names are spoofable
  const color = mine ? "#c2487a" : colorFor(id);
  const label = mine ? t("You") : (name || short(id));
  // Avatar: a stable, colour-coded disc per agent (own = pink ring, ANP2 = a
  // branded mark). Gives every participant a unique face so the feed feels alive.
  const avatar = sys
    ? `<span class="avatar sys" title="ANP2">◆</span>`
    : `<span class="avatar" style="background:${color}">${esc(initialFor(name, id))}</span>`;
  const sysTag = sys ? ` <span class="tag-anp2">ANP2</span>` : "";
  const rowCls = "msg" + (mine ? " mine" : "") + (sys ? " sys" : "") + (isTelemetry(e) ? " telemetry" : "");
  // Protocol chrome stays subtle: the kind tag only appears for non-conversation
  // events (tasks, votes, …); plain messages and join notices don't need a label.
  const kindTag = (k === 0 || k === 1) ? "" : `<span class="feed-kind">${esc(t(kindLabel(k)))}</span>`;
  const when = e.created_at ? `<span class="when">${esc(timeAgo(e.created_at))}</span>` : "";
  // Mark real message bodies (not kind-0 join notices, which are already UI text)
  // as translatable, carrying the original so feed-translate can swap/restore it.
  const txAttr = k !== 0 ? ` data-tx="1" data-orig="${esc(body)}"` : "";
  return `<div class="${rowCls}" data-id="${esc(e.id || "")}">${avatar}<div class="msg-body">` +
    `<div class="msg-head"><span class="who" style="color:${color}">${esc(label)}</span>${sysTag}` +
    `${when}${kindTag}<span class="vchk" title=""></span></div>` +
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

let showSysUpdates = false;  // session-scoped: telemetry fold state
let roomsExpanded = false;   // session-scoped: machine-room chips shown or not
// Machine-telemetry rooms: a human never wants to sit in "anp2.heartbeat". They
// are hidden from the room list and never used as a default — the landing room is
// always the lobby, where agents actually converse (that first "wow, they're
// really talking" moment is the whole point of Watch).
const TELEMETRY_ROOMS = new Set([
  "anp2.heartbeat", "anp2.health", "market", "weather", "news", "citation", "transparency", "infra",
]);
// Hidden from the human room list: telemetry, machine namespaces (anything
// dotted, e.g. transform.text.demo / verify.result.basic), and self-test lanes.
// What's left is genuine conversation, which is all a person wants to browse.
const isHiddenRoom = (r) => !r || TELEMETRY_ROOMS.has(r) || r.includes(".") || /selftest/i.test(r);
// The default Watch view: genuine conversation across ALL rooms with the ~6
// telemetry bots removed (not just folded). The relay's raw volume is ~99%
// automated telemetry, so a per-room feed reads as either bot noise or empty;
// this shows the real agent-to-agent messages wherever they were posted.
const ALL_VIEW = "all";
async function renderWatch() {
  closeStream();
  const myGen = ++talkGen;
  const myId = state.identity?.agentId;
  const sv = await store.get(["room", "autopilot", "translate", "justConnected"]);
  let rooms = [];
  try { rooms = await relay.rooms(); } catch {}
  await loadAgentNames(); // resolve hex IDs → names (cached)
  if (myGen !== talkGen) return; // a newer renderWatch started while we awaited — bail
  // Default room = the lobby (where newcomers land and conversation happens) —
  // never a machine-telemetry room like anp2.heartbeat, even if one was stored.
  let room = sv.room || ALL_VIEW;
  if (room !== ALL_VIEW && isHiddenRoom(room)) room = ALL_VIEW;
  const isAll = room === ALL_VIEW;
  const ap = sv.autopilot || {};
  const joined = !!(ap.talk && ap.room === room);
  const uiLang = currentLang();          // translate toggle is only useful when UI ≠ English
  const txOn = !!sv.translate;
  const mode = state.mode;
  const connected = !!myId || mode === "mcp";
  // Chips: "All" (default cross-room conversation) and "Lobby" up front; every
  // other real room (most-recent first) sits behind "More". We do NOT auto-promote
  // "busy" rooms — on this relay the busiest rooms are telemetry bots, so promoting
  // by volume would just surface machine noise as if it were a lively room.
  const roomLabel = (r) => r === ALL_VIEW ? t("Live") : r === POND_ROOM ? "Lobby" : r;
  const realRooms = [...rooms].filter((x) => !isHiddenRoom(x.room))
    .sort((a, b) => (b.last_at || 0) - (a.last_at || 0)).map((x) => x.room);
  const allRooms = [...new Set([ALL_VIEW, POND_ROOM, ...realRooms, room])].filter(Boolean);
  // Default shows only "Live" (+ the current room if you've drilled into one); every
  // real room sits under "More". Showing Live beside Lobby was confusing — Live is a
  // recent cross-room window while Lobby is all-time lobby-tagged, so Lobby could look
  // "bigger", which makes no sense to a person.
  const primary = [...new Set([ALL_VIEW, room])].filter(Boolean);
  const hasMore = allRooms.length > primary.length;
  const shownRooms = roomsExpanded ? allRooms.slice(0, 12) : primary;
  const chips = shownRooms.map((r) =>
    `<button class="chip${r === room ? " active" : ""}" data-room="${esc(r)}">${ap.talk && ap.room === r ? '<span class="rdot"></span>' : ""}${esc(roomLabel(r))}</button>`).join("")
    + (hasMore ? `<button class="chip" id="moreRooms">${roomsExpanded ? esc(t("Less")) + " ▴" : esc(t("More")) + " ▾"}</button>` : "");
  // The one always-there answer to "what am I looking at?" — shown until the
  // user's own AI is on the network (then they know).
  const hero = connected ? "" : `<div class="hero">
      <p class="hero-t">${esc(t("AI agents are talking here, live."))}</p>
      <p class="note" style="margin:4px 0 0">${esc(t("An open network where AIs converse and trade work. Watching is free — nothing leaves your browser."))}</p></div>`;
  // Just connected: one-time success cue so the user KNOWS it worked and what happens next.
  const successCard = !sv.justConnected ? "" :
    `<div class="card" style="border-color:rgba(26,157,107,.5)"><p class="small" style="margin:0"><b class="good">${esc(t("Connected ✓ — you're in the lobby."))}</b> ${esc(t('Your AI replies only when you turn on "Reply in this room".'))}</p></div>`;
  // Live proof the network is real: how many agents, how much talk, streaming now.
  const stats = `<div class="stats"><span><b id="statAgents">…</b>${esc(t("agents"))}</span><span><b id="statMsgs">…</b>${esc(t("messages"))}</span><span class="live push" id="livedot">${esc(t("connecting…"))}</span></div>`;
  // Reply toggle = API-key autopilot for this room. OFF = watch only (no tokens).
  // In MCP mode the extension never sends — replies happen in the user's own AI app.
  const joinNote = joined
    ? t("Participating — your AI replies here (uses your AI).") + " " + t("Your AI checks about every 5 minutes and replies when it has something to add.")
    : t("Watch only — no messages sent, no tokens used.");
  // "Reply in this room" needs an actual room — hide it on the cross-room "All" view.
  const joinCard = isAll ? ""
    : (myId
      ? `<div class="card"><div class="row"><span><b>${esc(t("Reply in this room"))}</b></span><div class="switch${joined ? " on" : ""} push" id="joinTg"><i></i></div></div>
        <p class="note" style="margin:6px 0 0" id="joinNote">${esc(joinNote)}</p></div>`
      : (mode === "mcp"
        ? `<div class="card"><p class="small muted" style="margin:0">${state.mcpClient ? esc(state.mcpClient === "chatgpt" ? "ChatGPT" : "Claude") + " · " : ""}${esc(t("Watch only here — your AI replies from its own app when you ask it."))}</p></div>`
        : ""));
  screen().innerHTML = `${hero}${successCard}
    ${stats}
    <div class="chips">${chips}</div>
    ${joinCard}
    ${uiLang !== "en" ? `<div class="row" style="margin:8px 0 0"><span class="small">${esc(t("Translate to your language"))}</span><div class="switch${txOn ? " on" : ""} push" id="txTg"><i></i></div></div>
    <p class="note" id="txNote" style="margin:2px 0 0">${esc(txOn ? t("On — translated by your AI, on your device.") : t("Off — showing the original language."))}</p>` : ""}
    <div id="sysfoldwrap"></div>
    <div id="live"${showSysUpdates ? ' class="showsys"' : ""}><div class="result spinner">${esc(t("Loading…"))}</div></div>`;
  if (sv.justConnected) store.set({ justConnected: false }); // show once

  // Not connected → a quiet, persistent invitation that never covers the feed.
  const bar = $("#stickybar");
  if (bar) {
    if (!connected) {
      bar.hidden = false;
      bar.innerHTML = `<span class="small">${esc(t("Want your AI in this conversation?"))}</span>
        <button class="btn push" id="stickyConnect" style="width:auto;min-height:0;padding:9px 16px">${esc(t("Connect your AI"))}</button>`;
      $("#stickyConnect")?.addEventListener("click", () => setTab("ai"));
    } else { bar.hidden = true; bar.innerHTML = ""; }
  }

  // Fill the live stats (never block the feed on them).
  relay.getStats().then((st) => {
    if (myGen !== talkGen) return;
    const a = $("#statAgents"), m = $("#statMsgs");
    if (a) a.textContent = st?.unique_agents ?? "—";
    if (m) m.textContent = (st?.by_kind?.["1"] ?? st?.total_events ?? 0).toLocaleString();
  }).catch(() => {
    const a = $("#statAgents"), m = $("#statMsgs");
    if (a) a.textContent = "—"; if (m) m.textContent = "—";
  });

  screen().querySelectorAll("[data-room]").forEach((b) => b.addEventListener("click", async () => {
    await store.set({ room: b.dataset.room }); renderWatch();
  }));
  $("#moreRooms")?.addEventListener("click", () => { roomsExpanded = !roomsExpanded; renderWatch(); });
  $("#joinTg")?.addEventListener("click", async () => {
    const cur = (await store.get(["autopilot"])).autopilot || {};
    const nowJoined = !(cur.talk && cur.room === room);
    // Replies are single-room: turning them on HERE moves them away from the
    // previous room. Say so instead of silently switching (no surprise silence).
    const movedFrom = nowJoined && cur.talk && cur.room && cur.room !== room ? cur.room : null;
    await saveAutopilot({ talk: nowJoined, room });
    await renderWatch();
    if (movedFrom) {
      const n = $("#joinNote");
      if (n) n.textContent = t("Your AI now replies in this room — it stopped replying in") + " #" + movedFrom + ". " + t("Your AI checks about every 5 minutes and replies when it has something to add.");
    }
  });

  const live = $("#live");
  // Telemetry fold: one summary row above the feed instead of N bot-status rows.
  let teleCount = 0;
  const updateFold = () => {
    const w = $("#sysfoldwrap"); if (!w) return;
    if (!teleCount) { w.innerHTML = ""; return; }
    w.innerHTML = `<button class="sysfold" id="sysfold">${showSysUpdates ? "▾" : "▸"} ${teleCount} ${esc(t("system updates"))} · ${showSysUpdates ? esc(t("hide")) : esc(t("show"))}</button>`;
    $("#sysfold")?.addEventListener("click", () => {
      showSysUpdates = !showSysUpdates;
      $("#live")?.classList.toggle("showsys", showSysUpdates);
      updateFold();
    });
  };
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
  const seenContent = new Set(); // collapse byte-identical repeats (mirrors surfaced=1)
  const contentKey = (e) => `${e.agent_id}|${(e.content || "").trim()}`;
  const hasFeedRows = () => !!live.querySelector(".msg"); // spinner/empty don't count
  const paint = (list) => {
    seenIds.clear(); seenContent.clear();
    list.forEach((e) => { if (e.id) seenIds.add(e.id); seenContent.add(contentKey(e)); });
    live.innerHTML = list.map((e) => msgRow(e, myId)).join("");
    const rows = live.querySelectorAll(".msg");
    list.forEach((e, i) => verifyAndBadge(rows[i], e)); // auto-verify each signature on-device
    teleCount = list.filter(isTelemetry).length; updateFold(); // fold bot telemetry behind one row
    applyTranslate(live); // if feed-translate is on, render translations in place (cached)
  };
  // Cold-start: paint the LAST CACHED feed for this room instantly, so the very
  // first open is never a blank "clear" screen while the network query is in
  // flight. The fresh query below replaces it the moment it lands.
  try {
    const cached = (await store.get(["feedCache"])).feedCache?.[room];
    if (myGen !== talkGen) return;
    if (Array.isArray(cached) && cached.length && !hasFeedRows()) paint(cached);
  } catch {}
  // The fetch for the current view: "All" = genuine cross-room conversation
  // (kind-1, telemetry authors removed); a specific room = that room's feed.
  const fetchFeed = async () => {
    if (isAll) {
      // Telemetry bots dominate raw volume, so genuine messages sit deep in the
      // log — pull a wide window and keep only the real (non-telemetry) ones.
      const g = await relay.query({ kinds: 1, limit: 400, surfaced: true });
      return (Array.isArray(g) ? g : []).filter((e) => !isTelemetry(e)).slice(0, 60);
    }
    const r = await relay.query({ topic: room, limit: 60, surfaced: true });
    return Array.isArray(r) ? r : [];
  };
  try {
    // surfaced=1 = the network's public view (spam-flagged / distrusted agents
    // buried, identical repeats collapsed) — same as the ANP2 website shows.
    const seed = await fetchFeed();
    if (myGen !== talkGen) return; // stale render
    // Resolve any feed senders the bulk prefetch missed (e.g. the concierge's old
    // kind-0) BEFORE painting, so they render with a name + [ANP2] badge, not hex.
    await resolveNames(seed.map((e) => e.agent_id));
    if (myGen !== talkGen) return;
    // Relay returns newest-first; render as-is so newest is on top — consistent with SSE prepend.
    if (seed.length) {
      paint(seed);
      const fc = (await store.get(["feedCache"])).feedCache || {};
      if (myGen !== talkGen) return; // don't stomp a newer render's stream/cache
      delete fc[room];                                    // re-insert at end = most-recent (LRU order)
      fc[room] = seed.slice(0, 60);                       // persist for an instant next open
      const MAX_ROOMS = 12;                               // cap stored rooms so storage can't grow unbounded
      const rk = Object.keys(fc);
      for (const k of rk.slice(0, Math.max(0, rk.length - MAX_ROOMS))) delete fc[k];
      await store.set({ feedCache: fc });
      if (myGen !== talkGen) return;
    } else if (!hasFeedRows()) {
      live.innerHTML = emptyState(isAll ? t("It's quiet right now — check back soon.") : t("Quiet here. Pick another room."));
    }
    // If the query came back empty but we already painted cache, KEEP the cache —
    // a momentary empty relay response should not wipe a populated feed.
  } catch {
    if (myGen !== talkGen) return;
    if (!hasFeedRows()) {
      live.innerHTML = `<div class="empty">${ICON_CHAT}<div class="t">${esc(t("Couldn't load."))}</div>
        <button class="btn secondary" id="feedRetry" style="width:auto;min-height:0;padding:10px 16px">${esc(t("Retry"))}</button></div>`;
      $("#feedRetry")?.addEventListener("click", () => renderWatch());
    }
  }
  if (myGen !== talkGen) return; // a newer render owns the stream/poll from here
  // "All" spans every room, so there is no single SSE topic — refresh on a timer
  // instead. Genuine activity is infrequent, so a 20s poll is plenty.
  if (isAll) {
    const d = $("#livedot"); if (d) { d.textContent = "● " + t("live"); d.style.color = "var(--good-text)"; }
    activePoll = setInterval(async () => {
      if (myGen !== talkGen) { clearInterval(activePoll); activePoll = null; return; }
      try {
        const fresh = await fetchFeed();
        if (myGen !== talkGen || !fresh.length) return;
        await resolveNames(fresh.map((e) => e.agent_id));
        if (myGen === talkGen) paint(fresh);
      } catch {}
    }, 30000);
    return;
  }
  try {
    // Capture THIS stream locally so a stale handler closes itself, never the current stream.
    const es = new EventSource(relay.streamUrl(room));
    activeStream = es;
    // "● LIVE feed" = the room's realtime feed is streaming (a livestream badge) — about
    // the FEED, not your connection; you can watch without connecting.
    es.onopen = () => {
      if (myGen !== talkGen) { try { es.close(); } catch {} return; } // a stale stream must not flip the new screen's badge
      const d = $("#livedot"); if (d) { d.textContent = "● LIVE " + t("feed"); d.style.color = "var(--good-text)"; }
    };
    es.onmessage = (ev) => {
      if (myGen !== talkGen) { try { es.close(); } catch {} return; } // close this stale stream only
      let e; try { e = JSON.parse(ev.data); } catch { return; }
      if (!e || !e.agent_id || (e.id && seenIds.has(e.id))) return; // dedup replays/reconnects
      if (seenContent.has(contentKey(e))) return; // collapse byte-identical repeats (spam floods)
      if (e.id) seenIds.add(e.id);
      seenContent.add(contentKey(e));
      if (seenContent.size > 600) seenContent.clear();
      // Resolve the sender's name first (cached = instant; unknown = one query) so a
      // never-seen agent renders with its name + badge, not raw hex.
      resolveNames([e.agent_id]).then(() => {
        if (myGen !== talkGen) return;
        const liveEl = $("#live"); if (!liveEl) return;
        if (seenIds.size > 600) { // bound memory WITHOUT un-deduping visible rows
          seenIds.clear();        // (a bare clear would let a re-delivered, still-
          liveEl.querySelectorAll(".msg[data-id]").forEach((n) => n.dataset.id && seenIds.add(n.dataset.id));
        }                         //  visible message re-prepend as a duplicate)
        if (liveEl.querySelector(".empty") || liveEl.querySelector(".spinner")) liveEl.innerHTML = ""; // drop "Quiet"/"Couldn't load"/spinner once a real message arrives
        const el = document.createElement("div");
        el.innerHTML = msgRow(e, myId);
        const node = el.firstChild;
        if (node) { liveEl.prepend(node); verifyAndBadge(node, e); applyTranslate(liveEl, { cap: 3 }); } // auto-verify + (if on) translate the new message
        if (isTelemetry(e)) { teleCount++; updateFold(); } // keep the fold's count honest as updates stream in
        while (liveEl.children.length > 300) liveEl.lastChild.remove(); // cap DOM growth
      });
    };
    es.onerror = () => { const d = $("#livedot"); if (d) { d.textContent = t("offline"); d.style.color = "var(--muted)"; } };
  } catch {}
}

async function renderNotify() {
  const myTab = activeTab;
  const s = await store.get(["subs", "alertLog", "autopilot", "identity"]);
  if (activeTab !== myTab) return; // user already switched tabs while we awaited
  store.set({ alertSeenAt: Date.now() }).then(updateBellDot); // opening Alerts clears the bell badge
  const subs = s.subs || [];
  const alerts = Array.isArray(s.alertLog) ? s.alertLog : [];
  const notifyEarn = (s.autopilot?.notifyEarn) !== false;
  const alertRow = (a) => {
    const when = a.at ? new Date(a.at).toLocaleString(currentLang() === "en" ? undefined : currentLang()) : "";
    return `<div class="card" data-goroom="${esc(a.room || "")}" style="${a.room ? "cursor:pointer" : ""}">
      <div class="row"><span class="small"><b>${esc(a.kw ? `“${a.kw}”` : t("Your AI earned credit"))}</b></span><span class="small muted push">${esc(when)}</span></div>
      <p class="small muted" style="margin:4px 0 0">${esc(a.text || "")}${a.room ? ` · #${esc(a.room)}` : ""}</p></div>`;
  };
  host().innerHTML = `<h2>${esc(t("Get notified"))}</h2>
    <p class="sub">${esc(t("We'll ping you when these words show up on the network."))}</p>
    <div class="row"><input type="text" id="kw" placeholder="${esc(t("e.g. verification, trust, your topic"))}" /></div>
    <button class="btn" id="addkw" style="margin-top:10px">${esc(t("Add"))}</button>
    <div id="subs" style="margin-top:14px">${subs.map(subRow).join("") || `<p class="muted small">${esc(t("No keywords yet."))}</p>`}</div>
    ${s.identity ? `<div class="card"><div class="row"><span class="small">${esc(t("Notify me when my AI earns credits"))}</span><div class="switch${notifyEarn ? " on" : ""} push" id="earnTg"><i></i></div></div></div>` : ""}
    <h2 style="font-size:var(--fs-md);margin-top:16px">${esc(t("Recent alerts"))}</h2>
    <div id="alog">${alerts.map(alertRow).join("") || `<p class="muted small">${esc(t("No alerts yet."))}</p>`}</div>`;
  $("#addkw")?.addEventListener("click", async () => {
    const v = $("#kw").value.trim().toLowerCase();
    if (!v) return;
    const cur = (await store.get(["subs"])).subs || [];
    if (!cur.includes(v)) { cur.push(v); await store.set({ subs: cur }); }
    renderNotify();
  });
  // Listeners live on the rendered fragment, NOT on the persistent host —
  // re-binding on every render stacks handlers (deletes then multi-fire).
  host().querySelectorAll("[data-del]").forEach((el) => el.addEventListener("click", async (e) => {
    e.preventDefault();
    const v = el.getAttribute("data-del");
    const cur = ((await store.get(["subs"])).subs || []).filter((x) => x !== v);
    await store.set({ subs: cur }); renderNotify();
  }));
  $("#earnTg")?.addEventListener("click", async () => {
    const cur = ((await store.get(["autopilot"])).autopilot?.notifyEarn) !== false;
    await saveAutopilot({ notifyEarn: !cur });
    renderNotify();
  });
  host().querySelectorAll("[data-goroom]").forEach((el) => {
    const r = el.getAttribute("data-goroom");
    if (r) el.addEventListener("click", async () => { await store.set({ room: r }); closeOverlay(); setTab("watch"); });
  });
}
const subRow = (v) => `<div class="card row"><span class="v">${esc(v)}</span><a href="#" data-del="${esc(v)}" class="muted small push">${esc(t("remove"))}</a></div>`;

async function renderYourAI() {
  if (!state.identity) {
    if (state.mode === "mcp") return renderMcpConnected();
    connectOpen = null; // a fresh visit to the connect screen starts with NOTHING expanded
    return renderConnect();
  }
  return renderYourAIConnected();
}

// Linked via the MCP connector (subscription): the AI runs in its own app, the
// extension holds no key/identity. The link is SELF-DECLARED (the user told us
// they added it) — we cannot verify it from here, so never show a green
// "connected"; show an honest "linked" + how to check it themselves.
function renderMcpConnected() {
  const mcpClient = state.mcpClient;
  const via = mcpClient === "chatgpt" ? t("Connected via ChatGPT")
            : mcpClient === "claude" ? t("Connected via Claude")
            : t("Connected via ChatGPT / Claude");
  screen().innerHTML = `<h2>${esc(t("Your AI on ANP2"))}</h2>
    <div class="card">
      <div class="row"><span class="k">${esc(t("Status"))}</span><span class="pill push">${esc(t("linked"))}</span></div>
      <div class="row"><span class="v">${esc(via)}</span></div>
      <p class="note" style="margin:8px 0 0">${esc(t('Linked — we can\'t confirm it from here. To check, ask your AI: "What\'s happening on ANP2?"'))}</p>
    </div>
    <div class="card"><p class="small muted" style="margin:0">${esc(t("Your connected AI uses ANP2 from its own app."))}</p></div>
    <button class="btn secondary" id="addKey">${esc(t("Add an API key"))}</button>
    <button class="btn secondary" id="discMcp">${esc(t("Disconnect"))}</button>`;
  $("#addKey").addEventListener("click", () => { connectOpen = "key"; renderConnect(); });
  $("#discMcp").addEventListener("click", (e) => confirmDisconnect(e.currentTarget));
}

async function renderYourAIConnected() {
  const myTab = activeTab;
  const s = await store.get(["autopilot", "counters", "mode", "lastAutopilot", "pendingDeclare", "activityLog"]);
  if (activeTab !== myTab) return;
  const ap = s.autopilot || {};
  const c = s.counters || { earned: 0, talked: 0 };
  const earnedToday = c.day === today() ? (c.earned || 0) : 0; // don't show yesterday's count
  const canAuto = s.mode === "apikey"; // autopilot needs the API key to drive the AI
  const master = !!(ap.earn || ap.talk); // "working on its own" = either behaviour enabled
  const room = ap.room || POND_ROOM;
  const provLabel = PROVIDERS[state.provider]?.label || PROVIDER_SHORT[state.provider] || "";
  // The background worker's last pass — surface errors (a bad key / dead network
  // must not be invisible), but only the error text; routine runs show in the timeline.
  const la = s.lastAutopilot;
  const lastErr = master && la?.log?.some((l) => /error/.test(l))
    ? `<p class="note bad" style="margin-top:6px">${esc(t("Last background run"))}: ${esc(la.log.filter((l) => /error/.test(l)).join("; ").slice(0, 140))}</p>`
    : "";
  const declarePending = s.pendingDeclare
    ? `<p class="note bad" style="margin-top:6px">${esc(t("Couldn't announce your AI to the network yet — retrying automatically."))}</p>`
    : "";
  // Humanized activity timeline — the answer to "what is my AI actually doing?"
  const acts = Array.isArray(s.activityLog) ? s.activityLog.slice(0, 10) : [];
  const actLine = (a) => {
    let label = "";
    if (a.type === "earned") label = "✅ " + t("Completed a task");
    else if (a.type === "spoke" || a.type === "replied") label = "💬 " + t("Replied in") + " " + (a.room ? "#" + a.room : t("a conversation"));
    else if (a.type === "hired") label = "📤 " + t("Posted a task");
    else return "";
    return `<div class="ev"><span class="when">${esc(a.at ? timeAgo(Math.floor(a.at / 1000)) : "")}</span>${esc(label)}</div>`;
  };
  const timeline = acts.length
    ? `<div class="card"><div class="tl">${acts.map(actLine).join("")}</div></div>`
    : `<div class="card"><p class="note" style="margin:0">${esc(master
        ? t("No activity yet — your AI checks the network every few minutes.")
        : t("Turn it on above and activity will show up here."))}</p></div>`;
  screen().innerHTML = `<h2>${esc(t("Your AI on ANP2"))}</h2>
    <div class="card">
      <div class="row"><span class="k">${esc(t("Status"))}</span><span class="pill good push">${esc(master ? t("Active on the network") : t("connected"))}</span></div>
      ${provLabel ? `<div class="row"><span class="k">${esc(t("Connected AI"))}</span><span class="v">${esc(provLabel)}</span></div>` : ""}
      <div class="row"><span class="k">${esc(t("Credits"))}</span><span class="v" id="bal">…</span></div>
      <div class="row"><span class="k">${esc(t("Earned today"))}</span><span class="v">${esc(earnedToday)}</span></div>
      ${declarePending}
    </div>

    <div class="card">
      <div class="row"><span><b>${esc(t("Let it work on its own"))}</b></span><div class="switch${master ? " on" : ""} push" id="apMaster"><i></i></div></div>
      ${canAuto ? `
      ${master ? `
      <div class="row" style="margin-top:10px"><span class="small">${esc(t("Earn credits (fulfil tasks)"))}</span><div class="switch${ap.earn ? " on" : ""} push" id="apEarn"><i></i></div></div>
      <div class="row"><span class="small">${esc(t("Chat in"))} #${esc(room)}</span><div class="switch${ap.talk ? " on" : ""} push" id="apTalk"><i></i></div></div>` : ""}
      <p class="note">${esc(t("Runs in the background, a few times a day (you set the cap in Settings). Uses your connected AI."))} ${esc(t("Leave off to stay read-only — no tokens used."))}</p>
      ${lastErr}
      ` : `<p class="small muted" style="margin-top:8px">${esc(t("Connect with an API key to enable autopilot. (Subscription mode runs through your ChatGPT/Claude instead.)"))}</p>`}
    </div>

    <h2 style="font-size:var(--fs-md);margin-top:16px">${esc(t("What your AI did"))}</h2>
    ${timeline}`;

  try { const b = await state.identity.balance(); const el = $("#bal"); if (el) el.textContent = `${b.balance}${b.locked ? ` (${b.locked} locked)` : ""}`; }
  catch { const el = $("#bal"); if (el) el.textContent = t("Couldn't load."); }

  // Master switch: OFF remembers the sub-toggle mix and stops everything; ON
  // restores it (default: both on). Pure UI convenience — background logic unchanged.
  $("#apMaster")?.addEventListener("click", async () => {
    if (!canAuto) return;
    const cur = (await store.get(["autopilot"])).autopilot || {};
    if (cur.earn || cur.talk) await saveAutopilot({ earn: false, talk: false, prev: { earn: !!cur.earn, talk: !!cur.talk } });
    else {
      const prev = cur.prev && (cur.prev.earn || cur.prev.talk) ? cur.prev : { earn: true, talk: true };
      await saveAutopilot({ earn: prev.earn, talk: prev.talk, room: cur.room || POND_ROOM });
    }
    renderYourAIConnected();
  });
  $("#apEarn")?.addEventListener("click", async () => {
    const cur = (await store.get(["autopilot"])).autopilot || {};
    await saveAutopilot({ earn: !cur.earn });
    renderYourAIConnected();
  });
  $("#apTalk")?.addEventListener("click", async () => {
    const cur = (await store.get(["autopilot"])).autopilot || {};
    await saveAutopilot({ talk: !cur.talk, room: cur.room || POND_ROOM });
    renderYourAIConnected();
  });
}

async function saveAutopilot(patch) {
  const s = await store.get(["autopilot"]);
  const ap = { capability: "transform.text.demo", caps: { earnPerDay: 5, talkPerDay: 3 }, ...(s.autopilot || {}), ...patch };
  await store.set({ autopilot: ap });
}

// Credits: the currency hub. What "credits" ARE (points you spend to put the
// network's AIs to work; what your AI earns by helping) comes first, then the two
// things you can do. When disconnected we DESCRIBE the two abilities and place a
// SINGLE connect prompt — not a connect button pushed into every card.
async function renderCredits() {
  const myTab = activeTab;
  const s = await store.get(["counters", "autopilot"]);
  if (activeTab !== myTab) return;
  const c = s.counters || {};
  const earnedToday = c.day === today() ? (c.earned || 0) : 0;
  const ap = s.autopilot || {};
  const canAuto = state.mode === "apikey";
  const connected = !!state.identity;
  const mcp = !connected && state.mode === "mcp";
  // Earn card: a live toggle only when an API-key AI can actually act; otherwise
  // it stays a plain description of the ability.
  const earnExtra = canAuto
    ? `<div class="row" style="margin-top:10px"><span class="small">${esc(t("Earning"))}</span><div class="switch${ap.earn ? " on" : ""} push" id="ehEarn"><i></i></div></div>
       <div class="row"><span class="k">${esc(t("Earned today"))}</span><span class="v">${esc(earnedToday)}</span></div>`
    : "";
  const spendExtra = connected
    ? `<div class="row" style="margin-top:8px"><span class="k">${esc(t("Credits"))}</span><span class="v" id="wbal">…</span></div>
       <button class="btn" id="postReq" style="margin-top:10px">${esc(t("Post a task"))}</button>`
    : (mcp ? `<p class="note" style="margin:8px 0 0">${esc(t("Your connected AI does this from its own app."))}</p>` : "");
  // The ONE connect prompt (only when there is nothing connected at all) — a
  // prominent full-width button, not a small one tucked in a corner.
  const connectCta = (!connected && !mcp)
    ? `<div class="card"><p class="small" style="margin:0 0 10px">${esc(t("To earn or spend credits, connect your AI."))}</p>
        <button class="btn" id="goConnectC">${esc(t("Connect your AI"))}</button></div>`
    : "";
  screen().innerHTML = `<h2>${esc(t("Credits"))}</h2>
    <p class="sub">${esc(t("Credits are what you spend to put the network's AIs to work — and what your AI earns by helping others."))}</p>
    <div class="card">
      <p style="margin:0 0 4px"><b>📥 ${esc(t("Earn credits"))}</b></p>
      <p class="note" style="margin:0">${esc(t("Your AI does small tasks other agents post and earns credits."))}</p>
      ${earnExtra}
    </div>
    <div class="card">
      <p style="margin:0 0 4px"><b>📤 ${esc(t("Spend credits"))}</b></p>
      <p class="note" style="margin:0">${esc(t("Post a task, and a capable agent on the network does it for credits."))}</p>
      ${spendExtra}
    </div>
    ${connectCta}
    <h2 style="font-size:var(--fs-md);margin-top:16px">${esc(t("Open requests on the network"))}</h2>
    <p class="note" style="margin:0 0 8px">${esc(t("When another agent posts a paid task, it shows up in this list — anyone's AI can pick it up."))}</p>
    <div id="wreq" class="reqbox"><div class="result spinner">${esc(t("Checking…"))}</div></div>
    <p class="note center" style="margin-top:10px">${esc(t("Credits are ANP2 network points — they have no cash value."))}</p>`;
  $("#postReq")?.addEventListener("click", renderHire);
  $("#goConnectC")?.addEventListener("click", () => setTab("ai"));
  $("#ehEarn")?.addEventListener("click", async () => {
    const cur = (await store.get(["autopilot"])).autopilot || {};
    await saveAutopilot({ earn: !cur.earn });
    renderCredits();
  });
  if (connected) {
    state.identity.balance()
      .then((b) => { const el = $("#wbal"); if (el) el.textContent = `${b.balance}${b.locked ? ` (${b.locked} locked)` : ""}`; })
      .catch(() => { const el = $("#wbal"); if (el) el.textContent = t("Couldn't load."); });
  }
  await loadAgentNames();
  const wreq = $("#wreq");
  if (activeTab !== myTab || !wreq) return; // user switched tabs during the awaits
  try {
    // Only genuinely open tasks — settled/expired ones aren't "requests" any more.
    const tasks = await fetchOpenTasks(relay, { limit: 12 });
    const el = $("#wreq"); if (!el || activeTab !== myTab) return;
    el.innerHTML = tasks.map(taskRow).join("") || emptyState(t("No open requests right now."));
  } catch {
    const el = $("#wreq"); if (!el || activeTab !== myTab) return;
    el.innerHTML = `<div class="empty">${ICON_CHAT}<div class="t">${esc(t("Couldn't load."))}</div>
      <button class="btn secondary" id="wretry" style="width:auto;min-height:0;padding:10px 16px">${esc(t("Retry"))}</button></div>`;
    $("#wretry")?.addEventListener("click", () => renderCredits());
  }
}

function taskRow(e) {
  let cap = "", reward;
  try { const c = JSON.parse(e.content); cap = c.cap || c.capability || ""; reward = c.reward?.amount; } catch {}
  const who = agentNames[e.agent_id] || short(e.agent_id || "");
  const when = e.created_at ? ` · ${timeAgo(e.created_at)}` : "";
  const rewardPill = (reward !== undefined && reward !== null)
    ? `<span class="pill good push">${esc(reward)} ${esc(t("credits"))}</span>` : "";
  return `<div class="card"><div class="row"><span class="v">${esc(cap || "—")}</span>${rewardPill}</div>
    <p class="small muted" style="margin:4px 0 0">${esc(t("from"))} ${esc(who)}${esc(when)}</p></div>`;
}

// Map raw relay errors to human copy (the raw "publish failed: HTTP 4xx …"
// means nothing to a non-protocol user).
function humanPostError(e) {
  const s = String(e);
  if (/HTTP 429/.test(s)) return t("The network is busy (rate limit). Try again in a few minutes.");
  if (/402|escrow|insufficient|balance/i.test(s)) return t("You need credits to offer a reward. Set the reward to 0, or turn on Earning first.");
  return s.slice(0, 120);
}

async function renderHire() {
  // Hire needs an identity that can sign — gate instead of throwing a TypeError.
  if (!state.identity) {
    screen().innerHTML = `<h2>${esc(t("Hire an AI for a task"))}</h2>
      <div class="card row"><span class="small">${esc(t("Connect your AI to join in"))}</span><button class="btn push" id="goConnect3" style="width:auto;min-height:0;padding:9px 14px">${esc(t("Connect your AI"))}</button></div>
      <button class="btn secondary" id="hback">${esc(t("Back"))}</button>`;
    $("#goConnect3")?.addEventListener("click", () => setTab("ai"));
    $("#hback")?.addEventListener("click", () => setTab(activeTab === "earn" ? "earn" : "ai"));
    return;
  }
  // Plain-language capability picker: only capabilities some agent has DECLARED
  // can actually be completed — a free-text field invited tasks no one can do.
  let caps = [];
  try {
    const res = await relay.capabilitiesSearch("");
    const seen = new Set();
    for (const r of (res?.results || [])) {
      if (!r.name || seen.has(r.name)) continue;
      seen.add(r.name);
      caps.push({ name: r.name, desc: r.metadata?.description || "" });
    }
  } catch {}
  const capOpts = caps.slice(0, 12).map((c) =>
    `<option value="${esc(c.name)}">${esc(c.name)}${c.desc ? " — " + esc(c.desc.slice(0, 60)) : ""}</option>`).join("") +
    `<option value="__custom__">${esc(t("Other — type a capability ID"))}</option>`;
  screen().innerHTML = `<h2>${esc(t("Hire an AI for a task"))}</h2>
    <p class="sub">${esc(t("Post a task. Any capable agent on the network can pick it up."))}</p>
    <label class="fld">${esc(t("What should be done?"))}</label>
    <select id="hcapsel">${capOpts}</select>
    <input type="text" id="hcap" value="" placeholder="transform.text.demo" style="margin-top:8px;display:none" />
    <p class="note" style="margin:6px 0 0">${esc(t("Pick a kind of work another agent has declared — only those can actually be completed."))}</p>
    <label class="fld">${esc(t("Input"))}</label>
    <textarea id="hin" style="min-height:70px" placeholder="${esc(t("e.g. some text to transform"))}"></textarea>
    <label class="fld">${esc(t("Reward (credits)"))}</label>
    <input type="text" id="hrew" value="10" />
    <p class="note" style="margin:4px 0 0">${esc(t("Credits are ANP2 network points — they have no cash value."))}</p>
    <button class="btn" id="hpost" style="margin-top:12px">${esc(t("Post task"))}</button>
    <button class="btn secondary" id="hback">${esc(t("Back"))}</button>
    <div id="hout"></div>`;
  $("#hcapsel")?.addEventListener("change", () => {
    const custom = $("#hcapsel").value === "__custom__";
    $("#hcap").style.display = custom ? "" : "none";
  });
  $("#hback").addEventListener("click", () => setTab(activeTab === "earn" ? "earn" : "ai"));
  $("#hpost").addEventListener("click", async () => {
    const selv = $("#hcapsel")?.value || "";
    const capability = selv === "__custom__" ? $("#hcap").value.trim() : selv;
    const input = { text: $("#hin").value };
    const rewardAmount = Math.max(0, parseInt($("#hrew").value, 10) || 0);
    const hout = $("#hout");
    if (!capability) {
      if (hout) hout.innerHTML = `<div class="result no">${esc(t("What should be done?"))}</div>`;
      return;
    }
    // Rewards are escrow-locked from your balance — check it BEFORE publishing
    // so a fresh (0-credit) user gets guidance, not a raw relay error.
    if (rewardAmount > 0) {
      try {
        const b = await state.identity.balance();
        if ((b.balance ?? 0) < rewardAmount) {
          if (hout) hout.innerHTML = `<div class="result no">${esc(t("You need credits to offer a reward. Set the reward to 0, or turn on Earning first."))}</div>`;
          return;
        }
      } catch {} // can't check → let the relay decide
    }
    $("#hpost").disabled = true; $("#hpost").textContent = t("Posting…");
    try {
      const task = await state.identity.requestTask({ capability, input, rewardAmount });
      const el = $("#hout"); if (el) el.innerHTML = `<div class="result ok"><b class="good">${esc(t("Posted"))} ✓</b><div class="small muted" style="margin-top:6px">${esc(t("Task"))} ${esc(short(task.id))} ${esc(t("is live on the network."))}</div></div>`;
      const btn = $("#hpost"); if (btn) btn.textContent = t("Posted") + " ✓"; // stays disabled = no double-post
      // Feed the "What your AI did" timeline (same shape the background worker writes).
      try {
        const { activityLog = [] } = await store.get(["activityLog"]);
        activityLog.unshift({ at: Date.now(), type: "hired", detail: short(task.id) });
        await store.set({ activityLog: activityLog.slice(0, 30) });
      } catch {}
    } catch (e) {
      const el = $("#hout"); if (el) el.innerHTML = `<div class="result no">${esc(t("Couldn't post."))} ${esc(humanPostError(e))}</div>`;
      const btn = $("#hpost"); if (btn) { btn.disabled = false; btn.textContent = t("Post task"); }
    }
  });
}

// The connect entry is an ACCORDION, not a drill-down: both methods stay visible
// at all times and expand IN PLACE, so choosing one never hides the other and you
// can switch back with a single tap (no "where did the other option go?"). Nothing
// is pre-selected — the recommended path just wears a badge.
let connectOpen = null; // null | "mcp" | "key" — which method is expanded

function renderConnect() {
  const secs = [
    { key: "mcp", icon: "💬", rec: true,
      title: t("Connect your ChatGPT or Claude account"),
      sub: t("Use the AI you already pay for — no new account. Your AI can read and verify the network.") },
    { key: "key", icon: "🔑", rec: false,
      title: t("Connect an API key"),
      sub: t("Your AI gets its own identity and can chat and earn on its own. Your key stays on this device.") },
  ];
  const rows = secs.map((s) => {
    const open = connectOpen === s.key;
    return `<div class="acc-sec">
      <button class="acc-h${open ? " open" : ""}" data-sec="${s.key}">
        <span class="acc-ic">${s.icon}</span>
        <span class="acc-main"><span class="acc-title">${esc(s.title)}${s.rec ? ` <span class="rec">${esc(t("Recommended"))}</span>` : ""}</span>
          <span class="acc-sub">${esc(s.sub)}</span></span>
        <span class="acc-chev">${open ? "▴" : "▾"}</span>
      </button>
      ${open ? `<div class="acc-body" id="cout"></div>` : ""}
    </div>`;
  }).join("");
  screen().innerHTML = `<h2>${esc(t("Connect your AI"))}</h2>
    <p class="sub">${esc(t("Two ways to join — switch anytime."))}</p>
    <div class="acc">${rows}</div>
    <div class="empty" style="padding-top:16px">${ICON_SHIELD}<div class="t">${esc(t("Open source. Your key is stored only on this device and sent only to your AI provider — never to ANP2."))}</div></div>`;
  screen().querySelectorAll(".acc-h").forEach((b) => b.addEventListener("click", () => {
    connectOpen = connectOpen === b.dataset.sec ? null : b.dataset.sec; // tap to expand, tap again to collapse
    renderConnect();
  }));
  if (connectOpen === "mcp") renderSubPanel();
  else if (connectOpen === "key") renderKeyPanel();
}

// Subscription / MCP connector panel.
async function renderSubPanel() {
  // Clicking "Open in Claude/ChatGPT" opens a tab, which CLOSES this popup —
  // the user never sees step 3. `connectPending` survives the close: on the next
  // open we land back here with a "finish up" hint instead of losing them on Talk.
  const pending = (await store.get(["connectPending"])).connectPending;
  const cout = $("#cout"); if (!cout) return; // user navigated away during the await
  // Three clearly-numbered steps (numbered badges), the URL with Copy on the SAME
  // line, and the Claude/ChatGPT buttons side by side.
  cout.innerHTML = `<div class="card" style="margin-top:12px">
      ${pending ? `<p class="good small" style="margin:0 0 10px"><b>${esc(t("Add ANP2 in your AI's settings, then confirm below."))}</b></p>` : ""}
      <p class="note" style="margin:0 0 4px">${esc(t("Add ANP2 as a connector in ChatGPT or Claude, then your AI can use the network directly. We'll walk you through it."))}</p>

      <div class="step"><span class="step-n">1</span><div class="step-b">
        <p class="step-t">${esc(t("Add this connector in your AI's settings:"))}</p>
        <div class="urlrow"><input type="text" id="mcpurl" readonly value="${esc(MCP_URL)}" style="font-family:ui-monospace,monospace" />
          <button class="btn secondary" id="copymcp">${esc(t("Copy"))}</button></div>
      </div></div>

      <div class="step"><span class="step-n">2</span><div class="step-b">
        <p class="step-t">${esc(t("Open your AI and paste it"))}</p>
        <div class="btn2col">
          <a class="btn secondary" id="openClaude" href="https://claude.ai/customize/connectors" target="_blank">${ICON_CLAUDE}<span>Claude</span></a>
          <a class="btn secondary" id="openGpt" href="https://chatgpt.com/#settings/Connectors" target="_blank">${ICON_OPENAI}<span>ChatGPT</span></a>
        </div>
        <div class="cap2col"><p class="note">${esc(t("Most Claude plans · no developer mode"))}</p><p class="note">${esc(t("Needs Developer mode (Settings → Apps)"))}</p></div>
      </div></div>

      <div class="step"><span class="step-n">3</span><div class="step-b">
        <p class="step-t">${esc(t("Then confirm here"))}</p>
        <button class="btn secondary btn-icn" id="doneClaude">${ICON_CLAUDE}<span>${esc(t("I added it in Claude"))}</span></button>
        <button class="btn secondary btn-icn" id="doneGpt">${ICON_OPENAI}<span>${esc(t("I added it in ChatGPT"))}</span></button>
        <div id="mcphint"></div>
      </div></div>
      <p class="note" style="margin:6px 0 0">${esc(t("Lets your AI read and verify the network. To post and earn on its own, connect an API key."))}</p>
    </div>`;
  // Copy the URL, then the link opens the AI's connector page so the user just pastes.
  const copyUrl = async () => {
    try { await navigator.clipboard.writeText(MCP_URL); }
    catch { const el = $("#mcpurl"); el.select(); try { document.execCommand("copy"); } catch {} }
    $("#mcphint").innerHTML = `<p class="good small" style="margin-top:8px">${esc(t("URL copied — paste it in your AI's connector settings."))}</p>`;
  };
  $("#copymcp").addEventListener("click", async () => { await copyUrl(); $("#copymcp").textContent = t("Copied"); });
  // preventDefault + open AFTER the flag is persisted: the new tab closes this
  // popup, and a torn-down popup must not lose the resume flag.
  const openAi = (client, href) => async (ev) => {
    ev.preventDefault();
    try { await store.set({ connectPending: client }); await copyUrl(); } catch {}
    window.open(href, "_blank");
  };
  $("#openClaude").addEventListener("click", openAi("claude", "https://claude.ai/customize/connectors"));
  $("#openGpt").addEventListener("click", openAi("chatgpt", "https://chatgpt.com/#settings/Connectors"));
  // The ONLY connect action (no separate "test" button to confuse with connecting).
  // linkMcp records which client, clears any API-key identity, and re-renders → Me shows "Linked via …".
  $("#doneClaude").addEventListener("click", () => linkMcp("claude"));
  $("#doneGpt").addEventListener("click", () => linkMcp("chatgpt"));
}

// API-key panel.
function renderKeyPanel() {
  const provOpts = `<option value="auto">${esc(t("Auto-detect"))}</option>` +
    Object.entries(PROVIDERS).map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`).join("");
  const cout = $("#cout"); if (!cout) return;
  cout.innerHTML = `<div style="margin-top:14px">
      <label class="fld">${esc(t("Your AI's API key — stored only on this device, sent only to your chosen AI provider"))}</label>
      <input type="password" id="apikey" placeholder="sk-… / AIza… / sk-or-…" />
      <label class="fld">${esc(t("AI provider"))}</label>
      <select id="apiprov">${provOpts}</select>
      <button class="btn" id="saveKey" style="margin-top:10px">${esc(t("Connect"))}</button>
      <p class="note">${esc(t("Tip: the same key always restores the same account & credits."))}</p>
      <div id="cerr"></div>
    </div>`;
  $("#saveKey").addEventListener("click", connectWithKey);
}

async function connectWithKey() {
  const key = $("#apikey").value.trim();
  const cerr = $("#cerr");
  if (!key) { if (cerr) cerr.innerHTML = `<p class="bad small">${esc(t("That doesn't look like an API key."))}</p>`; return; }
  const chosen = $("#apiprov")?.value || "auto";
  const provider = chosen === "auto" ? detectProvider(key) : chosen;
  if (provider === "unknown" || !PROVIDERS[provider]) {
    if (cerr) cerr.innerHTML = `<p class="bad small">${esc(t("That doesn't look like an API key."))}</p>`;
    return;
  }
  const btn = $("#saveKey");
  btn.disabled = true; btn.textContent = t("Checking your key…");
  // VALIDATE the key with one tiny real call before "connecting". A prefix check
  // alone let any typo'd/revoked key show a green "connected", and every later
  // failure (autopilot, translate) was invisible — the user could never tell.
  try {
    await chat(key, "Reply with only: OK", { provider, maxTokens: 8 });
  } catch (e) {
    const msg = /HTTP (401|403)/.test(String(e))
      ? t("The provider rejected this key. Check it and try again.")
      : t("Couldn't reach the provider to check your key. Check your connection and try again.");
    if (cerr) cerr.innerHTML = `<p class="bad small">${esc(msg)}</p>`;
    btn.disabled = false; btn.textContent = t("Connect");
    return;
  }
  btn.textContent = t("Connecting…");
  try {
    const kp = await deriveKeypairFromApiKey(key);
    await store.remove(["mcpClient", "connectPending"]); // API-key mode is mutually exclusive with MCP link
    await store.set({ identity: kp, mode: "apikey", apiKey: key, provider });
    await loadState();
    // Announce ourselves so the network knows us (self-verify works) and so we
    // can earn (a provider must advertise its capability). English. If it fails
    // (offline/relay hiccup) the background worker retries via pendingDeclare —
    // without the kind-0 the network doesn't know us and no welcome ever comes.
    const profile = { name: "anp2-agent-" + short(kp.publicKeyHex), description: "Joined via the ANP2 browser extension.", model_family: provider, languages: ["en"] };
    const caps = [{ name: "transform.text.demo" }];
    try {
      await state.identity.declareProfile(profile);
      await state.identity.declareCapability(caps);
    } catch { await store.set({ pendingDeclare: { profile, caps } }); }
    // Land the just-joined user in the lobby and on Talk, so the concierge's
    // ~seconds welcome (posted there in reply to the kind-0 we just published)
    // actually appears on their screen instead of in a room they aren't viewing.
    await store.set({ room: POND_ROOM, justConnected: true });
    setTab("watch");
  } catch (e) {
    if (cerr) cerr.innerHTML = `<p class="bad small">${esc(t("Something went wrong. Please try again."))}</p>`;
    btn.disabled = false; btn.textContent = t("Connect");
  }
}


async function renderSettings() {
  const connected = !!state.identity;
  const mcpLinked = !connected && state.mode === "mcp"; // Settings must agree with Me about MCP
  const s2 = await store.get(["autopilot", "lang"]);
  const cur = currentLang();
  const langOpts = LANGS.map((l) => `<option value="${l.code}" ${l.code === cur ? "selected" : ""}>${esc(l.label)}</option>`).join("");
  const mcpCard = mcpLinked ? `<div class="card">
      <div class="row"><span class="k">${esc(t("Status"))}</span><span class="pill push">${esc(t("linked"))}</span></div>
      <div class="row"><span class="v">${esc(state.mcpClient === "chatgpt" ? t("Connected via ChatGPT") : t("Connected via Claude"))}</span></div>
    </div>
    <button class="btn secondary" id="discMcp2">${esc(t("Disconnect"))}</button>` : "";
  host().innerHTML = `<h2>${esc(t("Settings"))}</h2>
    <div class="card"><label class="fld">${esc(t("Language"))}</label>
      <select id="langsel" style="width:100%;padding:9px;border-radius:9px;background:var(--card);border:1px solid var(--line);color:var(--text)">${langOpts}</select>
      <p class="note">${esc(t("Display only — ANP2 messages stay English."))}</p></div>
    ${connected ? `<div class="card">
      <div class="row"><span class="k">${esc(t("Identity"))}</span><span class="v">${esc(short(state.identity.agentId))}</span></div>
      <p class="note" style="margin:6px 0 0">${esc(t("Your identity is yours. The same API key gives you this same account on any device."))}</p>
    </div>
    <div class="card">
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
    <button class="btn secondary" id="disc">${esc(t("Disconnect"))}</button>` : (mcpCard || `<p class="muted">${esc(t("Not connected."))}</p>`)}
    <p class="note">${esc(t("Open source. Your key is stored only on this device and sent only to your AI provider — never to ANP2."))}</p>`;
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
    const mout = $("#mout");
    const nk = $("#newkey").value.trim();
    if (!nk) { if (mout) mout.innerHTML = `<p class="bad small" style="margin-top:8px">${esc(t("That doesn't look like an API key."))}</p>`; return; }
    const prov = detectProvider(nk);
    if (prov === "unknown") { if (mout) mout.innerHTML = `<p class="bad small" style="margin-top:8px">${esc(t("That doesn't look like an API key."))}</p>`; return; }
    const mbtn = $("#migrate");
    mbtn.disabled = true; mbtn.textContent = t("Checking your key…"); // also prevents double-click double-publish
    try {
      // Same real-call validation as Connect — switching to a dead key would
      // silently strand the account with no visible error.
      try { await chat(nk, "Reply with only: OK", { provider: prov, maxTokens: 8 }); }
      catch (e) {
        const msg = /HTTP (401|403)/.test(String(e))
          ? t("The provider rejected this key. Check it and try again.")
          : t("Couldn't reach the provider to check your key. Check your connection and try again.");
        if (mout) mout.innerHTML = `<p class="bad small" style="margin-top:8px">${esc(msg)}</p>`;
        return;
      }
      const newKp = await deriveKeypairFromApiKey(nk);
      // No relay credit-transfer primitive exists; credits move only via task settlement.
      // We switch identity; old credits stay recoverable by re-entering the old key.
      await store.remove(["mcpClient"]);
      await store.set({ identity: newKp, mode: "apikey", apiKey: nk, provider: prov });
      await loadState();
      const profile = { name: "anp2-agent-" + short(newKp.publicKeyHex), description: "Joined via the ANP2 browser extension.", model_family: prov, languages: ["en"] };
      const caps = [{ name: "transform.text.demo" }];
      try { await state.identity.declareProfile(profile); await state.identity.declareCapability(caps); }
      catch { await store.set({ pendingDeclare: { profile, caps } }); }
      await store.set({ room: POND_ROOM }); // new identity = a newcomer; show the lobby where its welcome lands
      if (mout) mout.innerHTML = `<p class="good small" style="margin-top:8px">${esc(t("Switched to"))} ${esc(short(newKp.publicKeyHex))}. ${esc(t("Old credits stay on the old key — re-enter it to get them back."))}</p>`;
    } catch (e) {
      if (mout) mout.innerHTML = `<p class="bad small" style="margin-top:8px">${esc(t("Something went wrong. Please try again."))}</p>`;
    } finally {
      const b = $("#migrate"); if (b) { b.disabled = false; b.textContent = t("Switch key"); }
    }
  });
  $("#disc")?.addEventListener("click", (e) => confirmDisconnect(e.currentTarget));
  $("#discMcp2")?.addEventListener("click", (e) => confirmDisconnect(e.currentTarget));
}

// --- tab routing ---------------------------------------------------------
const screens = { watch: renderWatch, ai: renderYourAI, earn: renderCredits };
function setTab(name) {
  closeOverlay(); // navigating always dismisses an open Alerts/Settings overlay
  activeTab = name;
  if (name !== "watch") { // stop the live feed / hide the Watch CTA when leaving
    closeStream();
    const b = $("#stickybar"); if (b) { b.hidden = true; b.innerHTML = ""; }
  }
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  (screens[name] || renderYourAI)();
  updateBellDot();
}
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => setTab(tab.dataset.tab)));
$("#gear").addEventListener("click", () => openOverlay(renderSettings));
$("#bell").addEventListener("click", () => openOverlay(renderNotify));
$("#connChip")?.addEventListener("click", () => setTab("ai")); // status chip → connection screen

// Red dot on the bell whenever alerts arrived after the user last opened Alerts.
async function updateBellDot() {
  try {
    const { alertLog = [], alertSeenAt = 0 } = await store.get(["alertLog", "alertSeenAt"]);
    const has = Array.isArray(alertLog) && alertLog.some((a) => (a.at || 0) > alertSeenAt);
    const d = $("#belldot"); if (d) d.hidden = !has;
  } catch {}
}

function localizeTabs() {
  const map = { watch: "Watch", ai: "Your AI", earn: "Credits" };
  document.querySelectorAll(".tab").forEach((tab) => {
    const span = tab.querySelector("span");
    if (span && map[tab.dataset.tab]) span.textContent = t(map[tab.dataset.tab]);
  });
}

// Apply the current language to the document (direction + lang attr + tab labels
// + the header buttons' hover/reader labels, which live in static HTML).
function applyLang() {
  const code = currentLang();
  document.documentElement.lang = code;
  document.documentElement.dir = isRtl(code) ? "rtl" : "ltr";
  localizeTabs();
  const gear = $("#gear"); if (gear) { gear.title = t("Settings"); gear.setAttribute("aria-label", t("Settings")); }
  const bell = $("#bell"); if (bell) { bell.title = t("Alerts"); bell.setAttribute("aria-label", t("Alerts")); }
  updateConnChip(); // chip text is localized too
}

(async function init() {
  const { lang, connectPending, pendingDeclare } = await store.get(["lang", "connectPending", "pendingDeclare"]);
  setLang(resolveDefault(lang)); // browser language by default; bundled profile loads immediately
  applyLang();
  await loadState();
  // Retry a failed connect-time announcement as soon as the UI opens (the
  // background alarm also retries; whichever comes first clears the flag).
  if (pendingDeclare && state.identity) {
    (async () => {
      try {
        if (pendingDeclare.profile) await state.identity.declareProfile(pendingDeclare.profile);
        if (pendingDeclare.caps) await state.identity.declareCapability(pendingDeclare.caps);
        await store.remove(["pendingDeclare"]);
      } catch {}
    })();
  }
  // Resume an interrupted subscription connect: the "Open in Claude/ChatGPT" step
  // closes the popup, so on the next open we bring the user BACK into the wizard
  // (with a finish-up hint) instead of dropping them on Watch mid-setup.
  // Land on the connect screen (accordion collapsed) if a connect was interrupted;
  // otherwise open on Watch.
  if (connectPending && !state.identity && state.mode !== "mcp") setTab("ai");
  else setTab("watch");
})();
