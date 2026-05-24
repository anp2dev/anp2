// build.mjs — generate CommonJS shim alongside ESM output
import { readFile, writeFile, copyFile } from "node:fs/promises";

// Convert dist/index.js (ESM) → dist/index.mjs + dist/index.cjs
// Vanilla transpile (no bundler) is sufficient for this small surface.

await copyFile("./dist/index.js", "./dist/index.mjs");

const esm = await readFile("./dist/index.js", "utf8");
// Minimal ESM → CJS:
//   import X from "Y"      → const X = require("Y")
//   import { a, b } from "Y" → const { a, b } = require("Y")
//   export const X         → module.exports.X / exports.X
//   export class Y         → exports.Y
//   export async function Z → exports.Z
//   export { a, b }        → Object.assign(module.exports, { a, b })
let cjs = esm
    .replace(/^import\s+(\w+)\s+from\s+["']([^"']+)["'];?$/gm,
        "const $1 = require(\"$2\");")
    .replace(/^import\s+\{\s*([^}]+)\s*\}\s+from\s+["']([^"']+)["'];?$/gm,
        "const { $1 } = require(\"$2\");")
    .replace(/^import\s+\*\s+as\s+(\w+)\s+from\s+["']([^"']+)["'];?$/gm,
        "const $1 = require(\"$2\");");

// Default export shim
cjs += "\nObject.assign(module.exports, { Agent, generateKeypair, computeEventId, signEventId });\n";

// Replace ESM exports
cjs = cjs
    .replace(/^export\s+\{\s*([^}]+)\s*\};?$/gm, "")  // already covered by Object.assign
    .replace(/^export\s+(class|async\s+function|function|const|let|var)\s+/gm, "$1 ");

await writeFile("./dist/index.cjs", cjs);

console.log("✓ ESM → dist/index.mjs");
console.log("✓ CJS → dist/index.cjs");
