#!/usr/bin/env node
/**
 * The terminal palette is defined once, and the two files that must spell it stay in agreement.
 *
 * Terminal and console chrome paints a fixed dark surface that does not follow the theme. Before
 * this guard, that surface existed as ~70 arbitrary Tailwind values (`bg-[#1a1b26]` and friends)
 * across eight files, plus four verbatim copies of the same 22-key xterm ITheme object. Retinting
 * it meant touching all of them, and missing one would have left a stray blue-grey panel on a
 * purple page that no typecheck or lint could see.
 *
 * It now lives in two places, and it has to, because they cannot share a representation: xterm
 * paints into a canvas and takes hex strings, while the surrounding chrome is Tailwind classes
 * resolving CSS variables. So:
 *
 *   src/app/globals.css     --terminal-* HSL triplets, consumed via tailwind.config.ts
 *   src/lib/terminal-theme.ts   TERMINAL_SURFACE hex, consumed by the four xterm call sites
 *
 * This checks three things:
 *   1. every --terminal-* token has a hex twin in TERMINAL_SURFACE, and they are the same colour
 *   2. TERMINAL_THEME.background and .cursorAccent are the surface, so the cursor cell does not
 *      cut a hole of the old colour into the new one
 *   3. no file has gone back to spelling a chrome colour as an arbitrary Tailwind value
 *
 * Rounding: an HSL triplet with one decimal and an 8-bit hex do not round-trip exactly in every
 * direction, so the comparison allows one 8-bit step per channel rather than demanding equality.
 * A genuine drift is a whole colour apart, never one step.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const CSS = "frontend/src/app/globals.css";
const TS = "frontend/src/lib/terminal-theme.ts";
const ROOT = "frontend/src";

// --terminal-<key> in the CSS pairs with TERMINAL_SURFACE.<value> in the module.
const PAIRS = [
  ["bg", "bg"],
  ["deep", "deep"],
  ["raised", "raised"],
  ["border", "border"],
];

// The literals this palette used to be spelled as. Anything reintroducing one of them is either a
// missed call site or a fifth dark surface; both are the defect this guard exists for.
const RETIRED = ["#1a1b26", "#16161e", "#1f2335", "#292e42"];

const hslToRgb = (h, s, l) => {
  s /= 100;
  l /= 100;
  const k = (n) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [f(0), f(8), f(4)].map((v) => Math.round(v * 255));
};
const hexToRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const near = (a, b) => a.every((v, i) => Math.abs(v - b[i]) <= 1);
const show = (rgb) => `#${rgb.map((v) => v.toString(16).padStart(2, "0")).join("")}`;

const css = readFileSync(CSS, "utf8");
const ts = readFileSync(TS, "utf8");
const offenders = [];

// 1. token vs hex
for (const [cssKey, tsKey] of PAIRS) {
  const triplet = css.match(
    new RegExp(`--terminal-${cssKey}:\\s*([\\d.]+)\\s+([\\d.]+)%\\s+([\\d.]+)%\\s*;`)
  );
  if (!triplet) {
    offenders.push(`${CSS} declares no --terminal-${cssKey}`);
    continue;
  }
  const hex = ts.match(new RegExp(`\\b${tsKey}:\\s*"(#[0-9a-fA-F]{6})"`));
  if (!hex) {
    offenders.push(`${TS} TERMINAL_SURFACE has no ${tsKey}`);
    continue;
  }
  const fromCss = hslToRgb(+triplet[1], +triplet[2], +triplet[3]);
  const fromTs = hexToRgb(hex[1]);
  if (!near(fromCss, fromTs)) {
    offenders.push(
      `--terminal-${cssKey} is ${show(fromCss)} but TERMINAL_SURFACE.${tsKey} is ${hex[1]}`
    );
  }
}

// 2. the xterm surface and the cursor cell are the declared surface
const surfaceHex = ts.match(/\bbg:\s*"(#[0-9a-fA-F]{6})"/)?.[1];
for (const key of ["background", "cursorAccent"]) {
  const line = ts.match(new RegExp(`\\b${key}:\\s*([^,\\n]+),`));
  if (!line) {
    offenders.push(`${TS} TERMINAL_THEME has no ${key}`);
  } else if (!line[1].includes("TERMINAL_SURFACE.bg")) {
    const literal = line[1].match(/#[0-9a-fA-F]{6}/)?.[0];
    if (!literal || !surfaceHex || literal.toLowerCase() !== surfaceHex.toLowerCase()) {
      offenders.push(`${TS} TERMINAL_THEME.${key} does not resolve to TERMINAL_SURFACE.bg`);
    }
  }
}

// 3. nothing spells a retired literal again
const walk = (dir) => {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p);
    else if (/\.(tsx?|css)$/.test(name)) {
      const body = readFileSync(p, "utf8");
      body.split("\n").forEach((line, i) => {
        // a comment may name the old palette to explain the history; a class may not use it
        if (/^\s*(\/\/|\*|\/\*)/.test(line)) return;
        for (const dead of RETIRED) {
          if (line.includes(`[${dead}]`)) {
            offenders.push(`${p}:${i + 1} spells the retired chrome literal ${dead}`);
          }
        }
      });
    }
  }
};
walk(ROOT);

if (offenders.length) {
  console.error("terminal theme: the chrome palette has drifted");
  for (const o of offenders) console.error(`  ${o}`);
  console.error(`  The tokens live in ${CSS} and their hex twin in ${TS}.`);
  console.error("  Chrome classes are bg-terminal, bg-terminal-deep, bg-terminal-raised,");
  console.error("  border-terminal-border and text-terminal-accent.");
  process.exit(1);
}

console.log(
  `terminal theme: ${PAIRS.length} surface tokens agree with their hex twin, no retired literals`
);
