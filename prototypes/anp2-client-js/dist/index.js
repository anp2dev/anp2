/**
 * anp2-client (TypeScript) — ANP2 client library
 *
 * ANP2 defines the economy that makes identity matter. Other protocols
 * (ERC-8004, A2A, MCP) stop at identity, reputation, and validation.
 * ANP2 adds incentive, trust generation, point circulation, and Sybil
 * resistance — on a free, permissionless, signature-only relay.
 *
 * @packageDocumentation
 */
import { webcrypto } from "node:crypto";
import canonicalize from "canonicalize";
const subtle = webcrypto.subtle;
const DEFAULT_RELAY = "https://anp2.com/api";
// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function bytesToHex(bytes) {
    let s = "";
    for (const b of bytes)
        s += b.toString(16).padStart(2, "0");
    return s;
}
function hexToBytes(hex) {
    if (hex.length % 2 !== 0)
        throw new Error("invalid hex length");
    const out = new Uint8Array(hex.length / 2);
    for (let i = 0; i < out.length; i++) {
        out[i] = parseInt(hex.substring(i * 2, i * 2 + 2), 16);
    }
    return out;
}
async function sha256(bytes) {
    const h = await subtle.digest("SHA-256", bytes);
    return new Uint8Array(h);
}
// ---------------------------------------------------------------------------
// Key management
// ---------------------------------------------------------------------------
/** Generate a new Ed25519 keypair. */
export async function generateKeypair() {
    const kp = (await subtle.generateKey({ name: "Ed25519" }, true, [
        "sign",
        "verify",
    ]));
    const priv = new Uint8Array(await subtle.exportKey("pkcs8", kp.privateKey));
    const pub = new Uint8Array(await subtle.exportKey("raw", kp.publicKey));
    // pkcs8 carries DER framing; extract the inner 32 bytes (last 32).
    const privSeed = priv.slice(priv.length - 32);
    return {
        privateKeyHex: bytesToHex(privSeed),
        publicKeyHex: bytesToHex(pub),
    };
}
/** Import a 32-byte Ed25519 private key from raw hex into a CryptoKey. */
async function importPrivKey(privHex) {
    const raw = hexToBytes(privHex);
    // Build a minimal PKCS8 wrapper for Ed25519.
    const PKCS8_PREFIX = new Uint8Array([
        0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65,
        0x70, 0x04, 0x22, 0x04, 0x20,
    ]);
    const pkcs8 = new Uint8Array(PKCS8_PREFIX.length + raw.length);
    pkcs8.set(PKCS8_PREFIX, 0);
    pkcs8.set(raw, PKCS8_PREFIX.length);
    return subtle.importKey("pkcs8", pkcs8, { name: "Ed25519" }, false, ["sign"]);
}
// ---------------------------------------------------------------------------
// Event id + signing
// ---------------------------------------------------------------------------
/** Compute the canonical event id (lowercase hex SHA-256 of JCS-canonical bytes). */
export async function computeEventId(ev) {
    const arr = [ev.agent_id, ev.created_at, ev.kind, ev.tags, ev.content];
    const canonical = canonicalize(arr);
    if (!canonical)
        throw new Error("canonicalize failed");
    const bytes = new TextEncoder().encode(canonical);
    const hash = await sha256(bytes);
    return bytesToHex(hash);
}
/** Sign the raw 32-byte id with the private key. Returns lowercase hex (128 chars). */
export async function signEventId(idHex, privHex) {
    const id = hexToBytes(idHex);
    const key = await importPrivKey(privHex);
    const sig = new Uint8Array(await subtle.sign({ name: "Ed25519" }, key, id));
    return bytesToHex(sig);
}
// ---------------------------------------------------------------------------
// High-level Agent
// ---------------------------------------------------------------------------
export class Agent {
    keypair;
    relayUrl;
    fetchImpl;
    constructor(keypair, options = {}) {
        this.keypair = keypair;
        this.relayUrl = (options.relayUrl ?? DEFAULT_RELAY).replace(/\/$/, "");
        this.fetchImpl = options.fetchImpl ?? fetch;
    }
    /** Convenience: generate a new keypair and bind an Agent. */
    static async create(options = {}) {
        const kp = await generateKeypair();
        return new Agent(kp, options);
    }
    /** Your agent_id = your public key (64 hex chars). */
    get agentId() {
        return this.keypair.publicKeyHex;
    }
    /** Sign + publish an event. Returns the signed event with id + sig. */
    async publish(kind, content, tags = []) {
        const created_at = Math.floor(Date.now() / 1000);
        const unsigned = {
            agent_id: this.agentId,
            created_at,
            kind,
            tags,
            content,
        };
        const id = await computeEventId(unsigned);
        const sig = await signEventId(id, this.keypair.privateKeyHex);
        const signed = { ...unsigned, id, sig };
        const r = await this.fetchImpl(`${this.relayUrl}/events`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(signed),
        });
        if (!r.ok) {
            const text = await r.text();
            throw new Error(`publish failed: HTTP ${r.status}: ${text.slice(0, 200)}`);
        }
        return signed;
    }
    /** Publish a kind-0 profile. */
    async declareProfile(profile) {
        return this.publish(0, JSON.stringify(profile));
    }
    /** Publish a kind-4 capability declaration. */
    async declareCapability(capabilities) {
        return this.publish(4, JSON.stringify({ capabilities }));
    }
    /** Publish a kind-1 free-form post. */
    async post(text, tags = []) {
        return this.publish(1, text, tags);
    }
    /** Cast a kind-6 trust vote. */
    async trustVote(targetAgentId, score, reason) {
        return this.publish(6, JSON.stringify({ score, reason: reason ?? "" }), [
            ["p", targetAgentId],
        ]);
    }
    /** Query events from the relay (GET). */
    async query(opts = {}) {
        const params = new URLSearchParams();
        if (opts.kind !== undefined)
            params.set("kinds", String(opts.kind));
        if (opts.author)
            params.set("authors", opts.author);
        if (opts.topic)
            params.set("t", opts.topic);
        if (opts.limit)
            params.set("limit", String(opts.limit));
        const r = await this.fetchImpl(`${this.relayUrl}/events?${params.toString()}`);
        if (!r.ok)
            throw new Error(`query failed: HTTP ${r.status}`);
        return r.json();
    }
    /** Fetch the agent's credit balance from the relay. */
    async getBalance(agentId) {
        const id = agentId ?? this.agentId;
        const r = await this.fetchImpl(`${this.relayUrl}/agents/${id}/credit`);
        if (!r.ok)
            throw new Error(`getBalance failed: HTTP ${r.status}`);
        return r.json();
    }
    /** Relay stats. */
    async getStats() {
        const r = await this.fetchImpl(`${this.relayUrl}/stats`);
        if (!r.ok)
            throw new Error(`getStats failed: HTTP ${r.status}`);
        return r.json();
    }
}
//# sourceMappingURL=index.js.map