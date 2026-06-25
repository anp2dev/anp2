/**
 * Browser E2E for the feed-translation feature (run: `npm run e2e`).
 *
 * Loads the real built popup (dist/popup.html) in a real Chromium via
 * playwright-core, with the relay AND the AI provider MOCKED (route) so the run
 * is deterministic and spends no tokens / touches no live state. Exercises the
 * exact popup.js + translate.js + anp2.js code paths in a real browser.
 *
 * Cases:
 *   1. feed renders agent messages (also guards the relay-fetch path)
 *   2. translate toggle is shown when the UI language ≠ English
 *   3. message starts in its original language
 *   4. toggle ON → each body is replaced by the (mocked) translation, and the
 *      user's AI was actually called
 *   5. toggle shows the ON state
 *   6. toggle OFF → the ORIGINAL text is restored
 *   7. with NO API key, toggling ON shows the "connect an API key" hint, does
 *      not turn on, and never calls the AI (no tokens, never blanks the feed)
 */
import { chromium } from "playwright-core";
import http from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";

const DIST = fileURLToPath(new URL("../dist/", import.meta.url));
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml" };

const CANNED = [
  { id: "a".repeat(64), agent_id: "b".repeat(64), kind: 1, created_at: 1781900000,
    content: "Hello from another agent — trust is earned, not declared.", tags: [], sig: "0".repeat(128) },
  { id: "c".repeat(64), agent_id: "d".repeat(64), kind: 1, created_at: 1781900001,
    content: "Verification beats reputation every time.", tags: [], sig: "0".repeat(128) },
];
const ORIGINAL_0 = CANNED[0].content;
const TRANSLATION = "ホンヤク済みテキスト"; // what the mocked AI "translates" everything into

const results = [];
const check = (name, ok) => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- static server for dist/ ---------------------------------------------
const server = http.createServer(async (req, res) => {
  try {
    let p = (req.url || "/").split("?")[0];
    if (p === "/") p = "/popup.html";
    const buf = await readFile(join(DIST, p));
    res.writeHead(200, { "content-type": MIME[extname(p)] || "application/octet-stream" });
    res.end(buf);
  } catch { res.writeHead(404); res.end("not found"); }
});
await new Promise((r) => server.listen(0, r));
const base = `http://localhost:${server.address().port}`;

let llmCalls = 0;
const browser = await chromium.launch({ headless: true });
try {
  const ctx = await browser.newContext();

  // Context-level mocks shared by every page. Relay = deterministic + offline.
  await ctx.route(/anp2\.com\/api\/rooms/, (r) => r.fulfill({ json: { rooms: [{ room: "meta" }] } }));
  await ctx.route(/anp2\.com\/api\/stream/, (r) => r.abort());                 // SSE not needed here
  await ctx.route(/anp2\.com\/api\/events/, (r) => {
    const u = new URL(r.request().url());
    const k = u.searchParams.get("kinds") || u.searchParams.get("kind") || "";
    return r.fulfill({ json: k === "0" ? [] : CANNED });                       // kind-0 = no profiles; else feed
  });
  // The user's AI provider (OpenAI shape). Translation engine = fixed string.
  await ctx.route(/api\.openai\.com/, (r) => {
    llmCalls++;
    return r.fulfill({ json: { choices: [{ message: { content: TRANSLATION } }] } });
  });

  // --- main page: Japanese UI + an API-key AI ---------------------------
  const page = await ctx.newPage();
  await page.addInitScript(() => {
    localStorage.setItem("lang", JSON.stringify("ja"));
    localStorage.setItem("apiKey", JSON.stringify("sk-test-key-xxxxxxxx"));
    localStorage.setItem("provider", JSON.stringify("openai"));
    localStorage.removeItem("translate");
  });
  await page.goto(`${base}/popup.html`);
  await page.waitForSelector("#live .msg", { timeout: 8000 }).catch(() => {});

  check("feed renders agent messages", (await page.locator("#live .msg").count()) >= CANNED.length);

  const toggle = page.locator("#txTg");
  check("translate toggle is shown for non-English UI", (await toggle.count()) === 1);

  const firstBody = page.locator("#live .mtxt[data-tx]").first();
  check("message starts in its original language", (await firstBody.textContent())?.trim() === ORIGINAL_0);

  await toggle.click();
  let translated = false;
  for (let i = 0; i < 40 && !translated; i++) { await sleep(100); translated = (await firstBody.textContent())?.trim() === TRANSLATION; }
  check("toggle ON → body replaced by the translation", translated);
  check("the user's connected AI was actually called", llmCalls > 0);
  check("toggle shows the ON state", ((await toggle.getAttribute("class")) || "").includes("on"));
  await page.screenshot({ path: join(DIST, "..", "e2e-translated.png") }).catch(() => {});

  await toggle.click();
  let restored = false;
  for (let i = 0; i < 20 && !restored; i++) { await sleep(100); restored = (await firstBody.textContent())?.trim() === ORIGINAL_0; }
  check("toggle OFF → original text restored", restored);

  // --- second page: Japanese UI but NO API key -------------------------
  const page2 = await ctx.newPage();
  await page2.addInitScript(() => {
    // Same-origin pages SHARE localStorage, so explicitly clear the key the main
    // page set — this case verifies the no-AI-key behavior.
    localStorage.setItem("lang", JSON.stringify("ja"));
    localStorage.setItem("translate", JSON.stringify(false));
    localStorage.removeItem("apiKey");
    localStorage.removeItem("provider");
  });
  await page2.goto(`${base}/popup.html`);
  await page2.waitForSelector("#txTg", { timeout: 8000 }).catch(() => {});
  const callsBefore = llmCalls;
  await page2.locator("#txTg").click();
  await sleep(500);
  const note = (await page2.locator("#txNote").textContent().catch(() => "")) || "";
  check("no API key → shows the 'connect an API key' hint", /API key/i.test(note));
  check("no API key → toggle does NOT turn on", !((await page2.locator("#txTg").getAttribute("class")) || "").includes("on"));
  check("no API key → the AI is NOT called", llmCalls === callsBefore);
} finally {
  await browser.close();
  server.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) { console.error("FAILED:", failed.map((f) => f.name).join("; ")); process.exit(1); }
console.log("E2E OK");
