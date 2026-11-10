#!/usr/bin/env node
/**
 * Structural guard: card illustrations stay one system, stay decoration, and stay hideable.
 *
 *   1. Colour lives only in illustrations.css; kit TSX has no colour literal and no fill or stroke
 *      attribute (check-semantic-colour.mjs does not see SVG attributes).
 *   2. Every fill and stroke there reads an --illo-* token (or none), declared for both themes.
 *   3. Flat: no gradient, filter, blur or SVG animation anywhere in the kit.
 *   4. Only illustration-frame.tsx renders <svg>: aria-hidden, not focusable, with the
 *      data-slot="card-illustration" the hide rule targets. Every scene renders through it with a
 *      measured `view` crop, so silhouettes share one scale and ground line.
 *   5. Hideable: the hide and fallback rules exist and the root layout renders data-illustrations.
 *   6. Outside the kit a scene is never the content of a Button, table cell or menu item, and is
 *      imported from its scenes/<domain> file (an index would ship every scene to every route).
 *   7. Every exported scene is used.
 *
 * Usage: node scripts/checks/check-illustrations.mjs [repo-root]
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const SRC = join(root, "frontend/src");
const KIT = join(SRC, "components/illustrations");
const CSS = join(KIT, "illustrations.css");
const FRAME = join(KIT, "illustration-frame.tsx");

const MAX_SCENE_LINES = 300;
const MAX_KIT_LINES = 500;
// Empty-scan protection: a guard that finds nothing must not pass.
const MIN_SCENES = 20;
const MIN_USE_SITES = 20;

const failures = [];
const fail = (where, message) => failures.push(`${where}: ${message}`);
const rel = (path) => relative(root, path);

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else if (/\.(tsx?|css)$/.test(name)) out.push(path);
  }
  return out;
}

// Comments may quote a hex (the palette's provenance is written beside each token); code may not.
function stripComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " ")).replace(/(^|[^:"'`])\/\/[^\n]*/g, "$1");
}

const lineOf = (source, index) => source.slice(0, index).split("\n").length;

const kitFiles = walk(KIT);
const kitTsx = kitFiles.filter((f) => /\.tsx?$/.test(f));

// 1. No colour in the kit's TSX.
const COLOUR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\((?!var\()/g;
const PAINT_ATTRIBUTE = /\s(fill|stroke)=\{?["'`]/g;
const PAINT_STYLE = /\b(fill|stroke|color|backgroundColor)\s*:/g;
for (const file of kitTsx) {
  const code = stripComments(readFileSync(file, "utf8"));
  for (const pattern of [COLOUR_LITERAL, PAINT_ATTRIBUTE, PAINT_STYLE]) {
    for (const m of code.matchAll(pattern)) {
      fail(`${rel(file)}:${lineOf(code, m.index)}`, `"${m[0].trim()}" paints a colour outside illustrations.css`);
    }
  }
}

// 2. Every paint in the stylesheet reads a token declared for both themes.
const css = stripComments(readFileSync(CSS, "utf8"));
const block = (selector) => {
  const at = css.search(new RegExp(`(^|\\n)${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{`));
  if (at < 0) return "";
  const open = css.indexOf("{", at);
  return css.slice(open + 1, css.indexOf("}", open));
};
const declared = (body) => new Set([...body.matchAll(/(--illo-[a-z0-9-]+)\s*:/g)].map((m) => m[1]));
const lightTokens = declared(block(":root"));
const darkTokens = declared(block(".dark"));
if (lightTokens.size < 10) fail(rel(CSS), `only ${lightTokens.size} --illo-* tokens on :root; the palette is missing`);
for (const token of lightTokens) if (!darkTokens.has(token)) fail(rel(CSS), `${token} has no dark value in .dark`);
for (const token of darkTokens) if (!lightTokens.has(token)) fail(rel(CSS), `${token} is set for dark only`);
// Set per element rather than per theme: a face's fill and hairline, and the stroke width per size.
const LOCAL = new Set(["--illo-f", "--illo-e", "--illo-sw"]);
for (const m of css.matchAll(/(?:^|[;{\s])(fill|stroke)\s*:\s*([^;}]+)/g)) {
  const value = m[2].trim();
  const ok = value === "none" || /^var\(--illo-(f|e)\)$/.test(value) || /^hsl\(var\(--illo-[a-z0-9-]+\)\)$/.test(value);
  if (!ok) fail(`${rel(CSS)}:${lineOf(css, m.index)}`, `${m[1]}: ${value} does not read an --illo-* token`);
}
for (const m of css.matchAll(/var\((--illo-[a-z0-9-]+)\)/g)) {
  if (!LOCAL.has(m[1]) && !lightTokens.has(m[1])) fail(`${rel(CSS)}:${lineOf(css, m.index)}`, `${m[1]} is used but never declared`);
}
if (/#[0-9a-fA-F]{3,8}\b/.test(css)) fail(rel(CSS), "a hex value outside a comment; tokens are HSL triplets");

// 3. Flat.
const NOT_FLAT = /<(linearGradient|radialGradient|filter|fe[A-Z]\w*|animate\w*)\b|\bblur\(|drop-shadow\(|@keyframes/g;
for (const file of kitFiles) {
  const code = stripComments(readFileSync(file, "utf8"));
  for (const m of code.matchAll(NOT_FLAT)) fail(`${rel(file)}:${lineOf(code, m.index)}`, `"${m[0]}" is not flat`);
}

// 4. One svg root, decorative, and every scene goes through it.
for (const file of kitTsx) {
  if (file === FRAME) continue;
  const code = stripComments(readFileSync(file, "utf8"));
  const svg = code.search(/<svg\b/);
  if (svg >= 0) fail(`${rel(file)}:${lineOf(code, svg)}`, "renders its own <svg>; render through IllustrationFrame");
}
const frame = stripComments(readFileSync(FRAME, "utf8"));
for (const needed of ['aria-hidden="true"', 'focusable="false"', 'data-slot="card-illustration"', "pointer-events-none"]) {
  if (!frame.includes(needed)) fail(rel(FRAME), `the svg root lost ${needed}`);
}
const sceneDir = join(KIT, "scenes");
const scenes = new Map();
for (const file of readdirSync(sceneDir).filter((f) => f.endsWith(".tsx")).map((f) => join(sceneDir, f))) {
  const source = readFileSync(file, "utf8");
  const lines = source.split("\n").length;
  if (lines > MAX_SCENE_LINES) fail(rel(file), `${lines} lines; a scene file stays under ${MAX_SCENE_LINES}`);
  const code = stripComments(source);
  const exports = [...code.matchAll(/export function (\w+Illustration)\b/g)];
  exports.forEach((m, i) => {
    const body = code.slice(m.index, exports[i + 1]?.index ?? code.length);
    if (!body.includes("<IllustrationFrame")) fail(`${rel(file)}:${lineOf(code, m.index)}`, `${m[1]} does not render through IllustrationFrame`);
    else if (!/<IllustrationFrame\b[^>]*\bview=\{\[[\d.\s,-]+\]\}/.test(body)) {
      fail(`${rel(file)}:${lineOf(code, m.index)}`, `${m[1]} has no measured view crop; see IllustrationView in illustration-frame.tsx`);
    }
    scenes.set(m[1], file);
  });
}
for (const file of kitTsx.filter((f) => !f.includes("/scenes/"))) {
  const lines = readFileSync(file, "utf8").split("\n").length;
  if (lines > MAX_KIT_LINES) fail(rel(file), `${lines} lines; kit files stay under ${MAX_KIT_LINES}`);
}
if (scenes.size < MIN_SCENES) fail(rel(sceneDir), `found ${scenes.size} scenes, expected at least ${MIN_SCENES}; is the scan reading the right tree?`);

// 5. Hideable from the first byte, and the classic icon comes back.
if (!/html\[data-illustrations="hide"\]\s*\[data-slot="card-illustration"\]\s*\{[^}]*display:\s*none/.test(css)) {
  fail(rel(CSS), "the hide rule for html[data-illustrations=\"hide\"] is missing");
}
if (!/html\[data-illustrations="hide"\]\s*\[data-slot="illustration-fallback"\]\s*\{[^}]*display:\s*contents/.test(css)) {
  fail(rel(CSS), "the fallback rule that brings the icon back when scenes are hidden is missing");
}
const layout = readFileSync(join(SRC, "app/layout.tsx"), "utf8");
if (!/<html[\s\S]{0,200}data-illustrations=\{/.test(layout)) fail("frontend/src/app/layout.tsx", "the root layout no longer renders data-illustrations");
if (!/illustrations\.css/.test(layout)) fail("frontend/src/app/layout.tsx", "the root layout no longer imports illustrations.css");

// 6 and 7. Use sites outside the kit.
const used = new Set();
let useSites = 0;
const HOSTS = ["Button", "TableCell", "TableHead", "DropdownMenuItem", "SelectItem", "TabsTrigger"];
for (const file of walk(SRC).filter((f) => /\.tsx?$/.test(f) && !f.startsWith(KIT))) {
  const source = readFileSync(file, "utf8");
  if (!source.includes("components/illustrations")) continue;
  const code = stripComments(source);
  for (const m of code.matchAll(/from\s+"@\/components\/illustrations(\/[^"]*)?"/g)) {
    const path = m[1] ?? "";
    if (!/^\/(scenes\/[a-z-]+|illustration-frame)$/.test(path)) {
      fail(`${rel(file)}:${lineOf(code, m.index)}`, `imports "@/components/illustrations${path}"; import a scene from its scenes/<domain> file`);
    }
  }
  // Import statements are blanked first, so a scene that is imported but never rendered counts as unused.
  const body = code.replace(/^import[\s\S]*?from\s+"[^"]+";/gm, (m) => m.replace(/[^\n]/g, " "));
  for (const name of scenes.keys()) {
    const refs = [...body.matchAll(new RegExp(`(<${name}\\b|[={(,:]\\s*${name}\\b)`, "g"))];
    if (refs.length === 0) continue;
    used.add(name);
    useSites += refs.length;
    for (const ref of refs) {
      if (!ref[0].startsWith("<")) continue;
      const before = body.slice(0, ref.index);
      for (const host of HOSTS) {
        const opens = (before.match(new RegExp(`<${host}\\b`, "g")) ?? []).length;
        const selfClosing = (before.match(new RegExp(`<${host}\\b[^>]*\\/>`, "g")) ?? []).length;
        const closes = (before.match(new RegExp(`</${host}>`, "g")) ?? []).length;
        if (opens - selfClosing > closes) fail(`${rel(file)}:${lineOf(body, ref.index)}`, `${name} sits inside a <${host}>; a scene is not an icon`);
      }
    }
  }
}
for (const [name, file] of scenes) if (!used.has(name)) fail(rel(file), `${name} is exported but no card renders it; delete it or use it`);
if (useSites < MIN_USE_SITES) fail("frontend/src", `found ${useSites} scene use sites, expected at least ${MIN_USE_SITES}`);

if (failures.length) {
  console.error(`illustrations: ${failures.length} problem(s)`);
  for (const f of failures) console.error(`  ${f}`);
  process.exit(1);
}
console.log(
  `illustrations: ${scenes.size} scenes, ${useSites} use sites, ${lightTokens.size} tokens in both themes; ` +
    "colour only in the stylesheet, flat, one decorative svg root, hideable, no scene used as an icon",
);
