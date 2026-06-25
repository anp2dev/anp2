import { test } from "node:test";
import assert from "node:assert/strict";
import { translateText, translateCached, translateKey } from "../src/lib/translate.js";

// A spy llm: records calls, returns a canned string.
const spy = (ret = "<translated>") => {
  const calls = [];
  const fn = async (prompt, opts) => { calls.push({ prompt, opts }); return ret; };
  fn.calls = calls;
  return fn;
};

test("English target is a no-op and never calls the AI", async () => {
  const llm = spy();
  const out = await translateText({ text: "hello", targetCode: "en", targetName: "English", llm });
  assert.equal(out, "hello");
  assert.equal(llm.calls.length, 0);
});

test("translateText asks the AI for the target language and returns its output", async () => {
  const llm = spy("こんにちは");
  const out = await translateText({ text: "hi", targetCode: "ja", targetName: "Japanese", llm });
  assert.equal(out, "こんにちは");
  assert.equal(llm.calls.length, 1);
  assert.match(llm.calls[0].prompt, /Japanese/);
  assert.match(llm.calls[0].prompt, /hi/);
});

test("empty / whitespace text returns as-is without calling the AI", async () => {
  const llm = spy();
  assert.equal(await translateText({ text: "   ", targetCode: "ja", targetName: "Japanese", llm }), "   ");
  assert.equal(await translateText({ text: "", targetCode: "ja", targetName: "Japanese", llm }), "");
  assert.equal(llm.calls.length, 0);
});

test("on AI error, falls back to the ORIGINAL text (feed never goes blank)", async () => {
  const llm = async () => { throw new Error("network down"); };
  const out = await translateText({ text: "original body", targetCode: "ja", targetName: "Japanese", llm });
  assert.equal(out, "original body");
});

test("blank AI output falls back to the original", async () => {
  const llm = spy("   ");
  const out = await translateText({ text: "keep me", targetCode: "ja", targetName: "Japanese", llm });
  assert.equal(out, "keep me");
});

test("long text is clipped before being sent to the AI", async () => {
  const llm = spy("ok");
  const long = "x".repeat(5000);
  await translateText({ text: long, targetCode: "ja", targetName: "Japanese", llm, maxChars: 600 });
  assert.ok(llm.calls[0].prompt.length < 1000, "prompt should carry a clipped body");
});

test("translateCached translates once per (id,lang) then serves from cache", async () => {
  const llm = spy("訳");
  const cache = new Map();
  const a = await translateCached({ id: "evt1", text: "hi", targetCode: "ja", targetName: "Japanese", llm, cache });
  const b = await translateCached({ id: "evt1", text: "hi", targetCode: "ja", targetName: "Japanese", llm, cache });
  assert.equal(a, "訳");
  assert.equal(b, "訳");
  assert.equal(llm.calls.length, 1, "second call must hit the cache, not the AI");
});

test("translateCached keys by language: a different target re-translates", async () => {
  const llm = spy("x");
  const cache = new Map();
  await translateCached({ id: "evt1", text: "hi", targetCode: "ja", targetName: "Japanese", llm, cache });
  await translateCached({ id: "evt1", text: "hi", targetCode: "ko", targetName: "Korean", llm, cache });
  assert.equal(llm.calls.length, 2);
});

test("translateCached with English target returns text and never calls the AI", async () => {
  const llm = spy();
  const cache = new Map();
  const out = await translateCached({ id: "e", text: "hi", targetCode: "en", targetName: "English", llm, cache });
  assert.equal(out, "hi");
  assert.equal(llm.calls.length, 0);
});

test("translateKey is stable and language-scoped", () => {
  assert.equal(translateKey("abc", "ja"), "abc|ja");
  assert.notEqual(translateKey("abc", "ja"), translateKey("abc", "ko"));
});
