#!/usr/bin/env node
/**
 * The terminal palette is defined once, and the two files that must spell it stay in agreement.
 *
 * Terminal and console chrome paints a fixed dark surface that does not follow the theme.
 * xterm paints into a canvas and takes hex strings (TERMINAL_SURFACE in
 * src/lib/terminal-theme.ts), while the chrome is Tailwind classes resolving --terminal-* HSL
 * triplets (src/app/globals.css).
 *
 * Checks: (1) every token has a hex twin of the same colour, within one 8-bit step per channel
 * because a one-decimal HSL triplet and a hex do not round-trip exactly;
 * (2) TERMINAL_THEME.background and .cursorAccent are the surface, so the cursor cell does not
 * cut a hole into it; (3) no file spells a chrome colour as a retired arbitrary Tailwind value.
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

// Retired chrome literals. Reintroducing one means a missed call site or a fifth dark surface.
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
        // A comment may name a retired literal; a class may not use it.
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
