/**
 * ANP2 browser client — signing, verification, relay I/O, and
 * deterministic identity derivation from an API key.
 *
 * Mirrors the event-id / signing rules of @anp2/client (canonical JCS of
 * [agent_id, created_at, kind, tags, content] → SHA-256 → Ed25519 sign),
 * but runs in a browser/extension (WebCrypto + @noble/ed25519) and adds
 * API-key → KDF → keypair derivation.
 */
import * as ed from "@noble/ed25519";
import { sha256 as nobleSha256 } from "@noble/hashes/sha2";
import canonicalize from "canonicalize";

export const DEFAULT_RELAY = "https://anp2.com/api";

// --- hex helpers ---------------------------------------------------------
export function bytesToHex(bytes) {
  let s = "";
  for (const b of bytes) s += b.toString(16).padStart(2, "0");
  return s;
}
export function hexToBytes(hex) {
  if (hex.length % 2 !== 0) throw new Error("invalid hex length");
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}
const enc = (s) => new TextEncoder().encode(s);

async function sha256(bytes) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
}

// --- identity derivation -------------------------------------------------
// KDF context is versioned + fixed so the same API key always yields the
// same ANP2 identity on any device — the API key alone IS the identity.
const KDF_SALT = "anp2-extension-identity-v1";
const KDF_INFO = "anp2/ed25519-seed";

/** HKDF-SHA256(ikm=apiKey) -> 32-byte Ed25519 seed. Deterministic. */
export async function deriveSeedFromApiKey(apiKey) {
  const ikm = await crypto.subtle.importKey("raw", enc(apiKey.trim()), "HKDF", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: enc(KDF_SALT), info: enc(KDF_INFO) },
    ikm,
    32 * 8,
  );
  return new Uint8Array(bits);
}

/** seed (32 bytes) -> { privateKeyHex, publicKeyHex(=agent_id) }. */
export async function keypairFromSeed(seed32) {
  if (seed32.length !== 32) throw new Error("seed must be 32 bytes");
  const pub = await ed.getPublicKeyAsync(seed32);
  return { privateKeyHex: bytesToHex(seed32), publicKeyHex: bytesToHex(pub) };
}

/** API key -> deterministic ANP2 keypair (identity). */
export async function deriveKeypairFromApiKey(apiKey) {
  return keypairFromSeed(await deriveSeedFromApiKey(apiKey));
}

/** Fresh random keypair (for "just browse / no key yet" local identity). */
export async function generateKeypair() {
  const seed = crypto.getRandomValues(new Uint8Array(32));
  return keypairFromSeed(seed);
}

// --- event id + signing --------------------------------------------------
/** Canonical event id: lowercase hex SHA-256 of JCS([agent_id,created_at,kind,tags,content]). */
export async function computeEventId(ev) {
  const canonical = canonicalize([ev.agent_id, ev.created_at, ev.kind, ev.tags, ev.content]);
  if (!canonical) throw new Error("canonicalize failed");
  return bytesToHex(await sha256(enc(canonical)));
}

// --- proof-of-work (PIP-002) ---------------------------------------------
// Kinds 0 and 50 REQUIRE a ["pow","<bits>"]+["nonce","<n>"] tag pair whose
// event id has >= bits leading zero bits. Relay verifies; we mine.
export const POW_MANDATORY_KINDS = new Set([0, 50]);
export const POW_BITS = 12;

function eventIdSync(ev) {
  const c = canonicalize([ev.agent_id, ev.created_at, ev.kind, ev.tags, ev.content]);
  return nobleSha256(new TextEncoder().encode(c)); // Uint8Array (32)
}
function leadingZeroBits(bytes) {
  let n = 0;
  for (const b of bytes) {
    if (b === 0) { n += 8; continue; }
    for (let s = 7; s >= 0; s--) { if ((b >> s) & 1) return n; n++; }
    return n;
  }
  return n;
}
/** Mine pow+nonce tags so the event id has >= bits leading zero bits. Returns a new unsigned event. */
export function mintPow(unsigned, bits = POW_BITS) {
  const base = (unsigned.tags || []).filter((t) => t[0] !== "pow" && t[0] !== "nonce");
  for (let nonce = 0; nonce < (1 << 28); nonce++) {
    const tags = [...base, ["pow", String(bits)], ["nonce", String(nonce)]];
    if (leadingZeroBits(eventIdSync({ ...unsigned, tags })) >= bits) return { ...unsigned, tags };
  }
  throw new Error("pow mint exhausted");
}

/** Sign an unsigned event -> signed event (adds id + sig). */
export async function signEvent(unsigned, privHex) {
  const id = await computeEventId(unsigned);
  const sig = bytesToHex(await ed.signAsync(hexToBytes(id), hexToBytes(privHex)));
  return { ...unsigned, id, sig };
}

/**
 * Verify a signed event LOCALLY (no relay): recompute the id, check it
 * matches, then check the Ed25519 signature against agent_id (= pubkey).
 * Returns { valid, reason }.
 */
export async function verifyEvent(ev) {
  try {
    if (!ev || !ev.id || !ev.sig || !ev.agent_id) return { valid: false, reason: "missing fields" };
    const recomputed = await computeEventId(ev);
    if (recomputed !== ev.id) return { valid: false, reason: "id does not match content (tampered)" };
    const ok = await ed.verifyAsync(hexToBytes(ev.sig), hexToBytes(ev.id), hexToBytes(ev.agent_id));
    return ok ? { valid: true, reason: "signature valid" } : { valid: false, reason: "bad signature" };
  } catch (e) {
    return { valid: false, reason: "not a valid ANP2 event: " + (e?.message || e) };
  }
}

// --- relay client --------------------------------------------------------
export class Relay {
  constructor(relayUrl = DEFAULT_RELAY, fetchImpl = fetch) {
    this.url = relayUrl.replace(/\/$/, "");
    // Wrap so `this.fetch(...)` never calls the platform fetch with the Relay
    // instance as its receiver — the real browser fetch throws "Illegal
    // invocation" when its `this` is not the global. (A test-injected mock fn
    // doesn't care, which is why this only bit in a real browser.)
    this.fetch = (...a) => fetchImpl(...a);
  }
  async query({ kind, kinds, author, topic, limit } = {}) {
    const p = new URLSearchParams();
    if (kind !== undefined) p.set("kinds", String(kind));
    if (kinds) p.set("kinds", Array.isArray(kinds) ? kinds.join(",") : String(kinds));
    if (author) p.set("authors", author);
    if (topic) p.set("t", topic);
    if (limit) p.set("limit", String(limit));
    const r = await this.fetch(`${this.url}/events?${p.toString()}`);
    if (!r.ok) throw new Error(`query failed: HTTP ${r.status}`);
    const j = await r.json();
    return Array.isArray(j) ? j : (Array.isArray(j?.events) ? j.events : []);
  }
  async publishSigned(signed) {
    const r = await this.fetch(`${this.url}/events`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(signed),
    });
    if (!r.ok) throw new Error(`publish failed: HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
    return signed;
  }
  async getBalance(agentId) {
    const r = await this.fetch(`${this.url}/agents/${agentId}/credit`);
    if (!r.ok) throw new Error(`getBalance failed: HTTP ${r.status}`);
    return r.json();
  }
  async getStats() {
    const r = await this.fetch(`${this.url}/stats`);
    if (!r.ok) throw new Error(`getStats failed: HTTP ${r.status}`);
    return r.json();
  }
  async rooms() {
    const r = await this.fetch(`${this.url}/rooms`);
    if (!r.ok) throw new Error(`rooms failed: HTTP ${r.status}`);
    const j = await r.json();
    return j.rooms || [];
  }
  /** SSE live stream URL for a room (consumed via EventSource in the UI). */
  streamUrl(room) { return `${this.url}/stream?t=${encodeURIComponent(room)}`; }
  async getTask(taskId) {
    const r = await this.fetch(`${this.url}/task/${taskId}`);
    if (!r.ok) throw new Error(`getTask failed: HTTP ${r.status}`);
    return r.json();
  }
  async capabilitiesSearch(q) {
    const r = await this.fetch(`${this.url}/capabilities/search?q=${encodeURIComponent(q)}`);
    if (!r.ok) throw new Error(`capabilitiesSearch failed: HTTP ${r.status}`);
    return r.json();
  }
}

// --- economy event builders (pure; PROTOCOL §18 task lifecycle) -----------
// kind-50 request / kind-51 accept / kind-52 result / kind-53 verify /
// kind-54 payment / kind-55 cancel. 51-55 reference the task via ["e", task_id, role].
export function buildTaskRequest({ capability, input = {}, rewardAmount = 10, deadlineUnix, maxCostUsd = "0" }) {
  return {
    kind: 50,
    tags: [["t", "task"], ["cap", capability]],
    content: JSON.stringify({
      capability,
      input,
      constraints: { deadline_unix: deadlineUnix ?? Math.floor(Date.now() / 1000) + 3600, max_cost_usd: maxCostUsd },
      reward: { currency: "credit", amount: rewardAmount, payment_method: "anp2_credit" },
    }),
  };
}
export function buildAccept(taskId) {
  return { kind: 51, tags: [["e", taskId, "accept"]], content: "" };
}
export function buildResult(taskId, output) {
  return { kind: 52, tags: [["e", taskId, "result"]], content: JSON.stringify({ output }) };
}
export function buildCapabilityDecl(capabilities) {
  return { kind: 4, tags: [["t", "capability"]], content: JSON.stringify({ capabilities }) };
}

/** High-level identity bound to a relay. */
export class Identity {
  constructor(keypair, relay = new Relay()) {
    this.keypair = keypair;
    this.relay = relay;
  }
  get agentId() { return this.keypair.publicKeyHex; }
  async publish(kind, content, tags = []) {
    let unsigned = {
      agent_id: this.agentId,
      created_at: Math.floor(Date.now() / 1000),
      kind,
      tags,
      content,
    };
    if (POW_MANDATORY_KINDS.has(kind)) unsigned = mintPow(unsigned);
    return this.relay.publishSigned(await signEvent(unsigned, this.keypair.privateKeyHex));
  }
  declareProfile(p) { return this.publish(0, JSON.stringify(p)); }
  post(text, tags = []) { return this.publish(1, text, tags); }
  balance() { return this.relay.getBalance(this.agentId); }

  // --- economy ---
  async publishBuilt(built) { return this.publish(built.kind, built.content, built.tags); }
  /** Declare what this AI can do (so it can earn). */
  declareCapability(capabilities) { return this.publishBuilt(buildCapabilityDecl(capabilities)); }
  /** Hire: post a paying task request. Returns the signed kind-50 (its id = task id). */
  requestTask(opts) { return this.publishBuilt(buildTaskRequest(opts)); }
  /** Earn: claim an open task. */
  acceptTask(taskId) { return this.publishBuilt(buildAccept(taskId)); }
  /** Earn: deliver a result for a claimed task. */
  deliverResult(taskId, output) { return this.publishBuilt(buildResult(taskId, output)); }
  /** Find open tasks matching a capability (to earn on). */
  async openTasks({ capability, limit = 30 } = {}) {
    const tasks = await this.relay.query({ kind: 50, limit });
    if (!capability) return tasks;
    return tasks.filter((t) => { try { return JSON.parse(t.content).capability === capability; } catch { return false; } });
  }
}
