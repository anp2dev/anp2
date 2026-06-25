import { test } from "node:test";
import assert from "node:assert/strict";
import { detectProvider, PROVIDERS, chat } from "../src/lib/llm.js";

test("detectProvider recognizes each provider's key shape", () => {
  assert.equal(detectProvider("sk-ant-abc"), "anthropic");
  assert.equal(detectProvider("sk-or-v1-abc"), "openrouter");
  assert.equal(detectProvider("AIzaSyA1234567890abc"), "google");
  assert.equal(detectProvider("sk-proj-xyz"), "openai");
  assert.equal(detectProvider("not-a-key"), "unknown");
});

test("PROVIDERS all have host + model + style", () => {
  for (const [k, v] of Object.entries(PROVIDERS)) {
    assert.ok(v.host?.startsWith("https://"), `${k} host`);
    assert.ok(v.model, `${k} model`);
    assert.ok(["openai", "anthropic", "google"].includes(v.style), `${k} style`);
  }
});

const cap = () => { const c = {}; const f = async (url, init) => { c.url = url; c.init = init; return { ok: true, json: async () => c.res, text: async () => "" }; }; return [c, f]; };

test("chat routes Google (Gemini) to generateContent with key in query", async () => {
  const [c, f] = cap();
  c.res = { candidates: [{ content: { parts: [{ text: "hi from gemini" }] } }] };
  const out = await chat("AIzaSyA1234567890abc", "ping", { fetchImpl: f });
  assert.match(c.url, /generativelanguage\.googleapis\.com.*:generateContent\?key=AIzaSyA1234567890abc/);
  assert.equal(out, "hi from gemini");
});

test("chat routes OpenRouter through the OpenAI-compatible chat/completions", async () => {
  const [c, f] = cap();
  c.res = { choices: [{ message: { content: "hi from openrouter" } }] };
  const out = await chat("sk-or-v1-test", "ping", { fetchImpl: f });
  assert.equal(c.url, "https://openrouter.ai/api/v1/chat/completions");
  assert.equal(c.init.headers["X-Title"], "ANP2");
  assert.equal(out, "hi from openrouter");
});

test("chat honors an explicit provider override", async () => {
  const [c, f] = cap();
  c.res = { content: [{ text: "claude" }] };
  await chat("sk-something", "ping", { provider: "anthropic", fetchImpl: f });
  assert.equal(c.url, "https://api.anthropic.com/v1/messages");
});
