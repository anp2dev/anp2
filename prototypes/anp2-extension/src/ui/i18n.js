/**
 * UI-display-only localization. Translates by English SOURCE TEXT (no keys),
 * falling back to English when a string is missing. The default follows the
 * browser language; the user can override it in Settings.
 *
 * Translations ship as STATIC, pre-built profiles (locales.js), authored ahead
 * of time — so every offered language displays immediately, with NO runtime AI
 * translation, NO ANP2 resources, and NO third-party service. English needs no
 * translation. Everything sent to ANP2 stays English.
 */
import { LANGS, BASE } from "./strings.js";
import { LOCALES } from "./locales.js";
export { LANGS, BASE };

let CURRENT = "en";
let DICT = {}; // source-text -> translated, for CURRENT lang (from the bundled profile)

export function currentLang() { return CURRENT; }

export function setLang(code) {
  CURRENT = LANGS.some((l) => l.code === code) ? code : "en";
  DICT = CURRENT === "en" ? {} : (LOCALES[CURRENT] || {});
}

// True for languages written right-to-left (so the UI can flip direction).
export function isRtl(code) { return code === "ar"; }

export function resolveDefault(stored) {
  if (stored && LANGS.some((l) => l.code === stored)) return stored;
  const n = (typeof navigator !== "undefined" ? navigator.language || "en" : "en").slice(0, 2).toLowerCase();
  return LANGS.some((l) => l.code === n) ? n : "en";
}

export function t(s) {
  if (CURRENT === "en") return s;
  return DICT[s] || s;
}
