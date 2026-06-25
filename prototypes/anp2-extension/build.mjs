import { build } from "esbuild";
import { cpSync, mkdirSync } from "node:fs";

mkdirSync("dist", { recursive: true });

await build({
  entryPoints: { popup: "src/ui/popup.js", background: "src/background.js" },
  bundle: true,
  format: "esm",
  target: "es2022",
  charset: "utf8", // keep CJK/Arabic/etc. as real UTF-8, not \uXXXX escapes
  outdir: "dist",
  sourcemap: false,
  legalComments: "none",
  minify: false,
});

// static assets
cpSync("src/ui/popup.html", "dist/popup.html");
cpSync("src/ui/popup.css", "dist/popup.css");
cpSync("manifest.json", "dist/manifest.json");
cpSync("icons", "dist/icons", { recursive: true });

console.log("build OK -> dist/");
