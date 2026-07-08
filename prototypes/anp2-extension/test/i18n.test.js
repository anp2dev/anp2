// i18n contract test — keeps UI, BASE and every locale profile in lockstep:
//   1. every t("...") literal in the UI/background source exists in BASE
//   2. every BASE string is actually used (or explicitly allowlisted)
//   3. every locale carries a non-empty translation for every BASE string
//   4. no locale carries strings BASE no longer has (dead-string drift)
// This is what previously rotted: locales missing in-use strings, and ~30
// dead strings still being carried in all 10 languages.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { LANGS, BASE } from "../src/ui/strings.js";
import { LOCALES } from "../src/ui/locales.js";

const here = dirname(fileURLToPath(import.meta.url));
const src = ["../src/ui/popup.js", "../src/background.js"]
  .map((p) => readFileSync(join(here, p), "utf8")).join("\n");

// Extract t("...") / t('...') literals (handles \" \' escapes inside).
function extractTLiterals(code) {
  const out = new Set();
  const re = /\bt\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*\)/g;
  let m;
  while ((m = re.exec(code))) {
    const raw = m[1] ?? m[2];
    out.add(raw.replace(/\\(['"\\])/g, "$1"));
  }
  return out;
}

// BASE strings referenced via t(variable) rather than a literal.
const PROGRAMMATIC = new Set([
  "Watch", "Your AI", "Credits",                        // tab map -> t(map[...])
  "identity", "message", "note", "capability", "knowledge",
  "trust vote", "donation", "reply", "task",            // KIND map -> t(kindLabel(k))
]);

const used = extractTLiterals(src);
const base = new Set(BASE);

test("every t() literal in the source is in BASE", () => {
  const missing = [...used].filter((s) => !base.has(s));
  assert.deepEqual(missing, [], `t() strings missing from BASE: ${JSON.stringify(missing)}`);
});

test("every BASE string is used by the UI (no dead strings)", () => {
  const dead = BASE.filter((s) => !used.has(s) && !PROGRAMMATIC.has(s));
  assert.deepEqual(dead, [], `BASE strings no longer used: ${JSON.stringify(dead)}`);
});

test("BASE has no duplicates", () => {
  assert.equal(new Set(BASE).size, BASE.length);
});

for (const { code, name } of LANGS) {
  if (code === "en") continue; // English is the source — no profile needed
  test(`locale ${code} (${name}) covers BASE exactly`, () => {
    const dict = LOCALES[code];
    assert.ok(dict, `locale profile missing: ${code}`);
    const missing = BASE.filter((s) => !(s in dict) || !String(dict[s]).trim());
    assert.deepEqual(missing, [], `${code} missing/empty: ${JSON.stringify(missing.slice(0, 10))}${missing.length > 10 ? ` …+${missing.length - 10}` : ""}`);
    const extra = Object.keys(dict).filter((k) => !base.has(k));
    assert.deepEqual(extra, [], `${code} carries dead strings: ${JSON.stringify(extra.slice(0, 10))}${extra.length > 10 ? ` …+${extra.length - 10}` : ""}`);
  });
}
