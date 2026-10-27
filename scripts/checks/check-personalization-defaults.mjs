#!/usr/bin/env node
// Structural guard: the shipped personalization defaults are ONE colour each, and they are
// legible.
//
// The default sidebar and accent are written down in five places, because the value has to be
// available before React mounts (the CSS seeds), during the pre-paint script, in the client
// bundle, in the settings picker, and to any user with no stored row (the backend). Nothing
// held them together, and they came apart: --personalization-accent read #8b5cf6 while its own
// --personalization-accent-h/s/l triplet read 262 83% 58%, which is #7c3bed. Both were live.
// The hex painted .sidebar-active and the landing hero; the triplet is what globals.css binds
// --primary and --ring to, so it painted every Button, Badge, switch, checkbox and focus ring
// until hydration replaced it. Nobody noticed for the life of the feature, and
// scripts/checks/check-semantic-colour.mjs quietly recorded a contrast baseline for the colour
// the browser stops using.
//
// So this file does three things:
//
//   1. Reads every copy and fails when they disagree.
//   2. Converts each hex itself and fails when a triplet does not describe its own hex. This is
//      the rule that would have caught the defect above, and it is the reason the file exists.
//   3. Measures the shipped pair. A default nobody can read is a defect whether or not the
//      copies agree, and "check it by eye in both themes" is not a gate.
//
// Deliberately parser-free and dependency-free, like check-jsx-stray-attrs.mjs and
// check-terminal-theme.mjs: the CI `guards` job runs checkout and setup-node and no `npm ci`,
// so importing anything out of frontend/node_modules would fail there.
//
// Run: node scripts/checks/check-personalization-defaults.mjs [repo-root]

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');

const FILES = {
  css: 'frontend/src/app/globals.css',
  types: 'frontend/src/types/personalization.ts',
  provider: 'frontend/src/components/personalization-provider.tsx',
  layout: 'frontend/src/app/layout.tsx',
  // important: this file and no other. #8b5cf6 is also a tag colour in frontend/src/types/api.ts,
  // in the monitoring chart palette, and seeded into tag_prefixes by alembic revision 001. A
  // guard that grepped the backend for the hex would report those as drift forever.
  backend: 'backend/app/routers/user_preferences.py',
};

const read = (key) => readFileSync(join(root, FILES[key]), 'utf8');
const offenders = [];
const fail = (msg) => offenders.push(msg);

// ---------------------------------------------------------------------------------------
// Colour maths. Same rounding as hexToHSL in frontend/src/lib/color.ts, on purpose: a guard that rounds
// differently from the code reports drift that does not exist.
// ---------------------------------------------------------------------------------------

const rgb = (hex) => {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex.trim());
  return m ? [1, 2, 3].map((i) => parseInt(m[i], 16)) : null;
};

function hexToHsl(hex) {
  const c = rgb(hex).map((v) => v / 255);
  const max = Math.max(...c);
  const min = Math.min(...c);
  const l = (max + min) / 2;
  let h = 0;
  let s = 0;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === c[0]) h = ((c[1] - c[2]) / d + (c[1] < c[2] ? 6 : 0)) / 6;
    else if (max === c[1]) h = ((c[2] - c[0]) / d + 2) / 6;
    else h = ((c[0] - c[1]) / d + 4) / 6;
  }
  return { h: Math.round(h * 360), s: Math.round(s * 100), l: Math.round(l * 100) };
}

const lum = (c) =>
  0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2]);
const ch = (v) => {
  const x = v / 255;
  return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
};
const contrast = (a, b) => {
  const [x, y] = [lum(a), lum(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
};
// color-mix(in srgb, A p%, B) and rgba(A, alpha) over an opaque B are the same arithmetic:
// browsers composite both in gamma-encoded sRGB, which is what these rules rely on.
const mix = (a, b, p) => [0, 1, 2].map((i) => Math.round(a[i] * p + b[i] * (1 - p)));

// ---------------------------------------------------------------------------------------
// Read every copy
// ---------------------------------------------------------------------------------------

const css = read('css');
const cssVar = (name) => {
  const m = new RegExp(`--${name}:\\s*([^;]+);`).exec(css);
  if (!m) fail(`${FILES.css}: --${name} is missing`);
  return m ? m[1].trim() : null;
};

const shipped = {
  accent: cssVar('personalization-accent'),
  sidebarBg: cssVar('personalization-sidebar-bg'),
  sidebarText: cssVar('personalization-sidebar-text'),
  sidebarMuted: cssVar('personalization-sidebar-muted'),
};

// Rule 2: every triplet describes its own hex.
for (const [varBase, hex] of [
  ['personalization-accent', shipped.accent],
  ['personalization-sidebar-bg', shipped.sidebarBg],
]) {
  if (!hex || !rgb(hex)) continue;
  const want = hexToHsl(hex);
  const got = {
    h: Number(cssVar(`${varBase}-h`)),
    s: Number((cssVar(`${varBase}-s`) ?? '').replace('%', '')),
    l: Number((cssVar(`${varBase}-l`) ?? '').replace('%', '')),
  };
  if (got.h !== want.h || got.s !== want.s || got.l !== want.l) {
    fail(
      `${FILES.css}: --${varBase} is ${hex}, which is ${want.h} ${want.s}% ${want.l}%, but its ` +
        `own triplet says ${got.h} ${got.s}% ${got.l}%. The stylesheet binds real tokens to the ` +
        `triplet, so these two paint different colours at different moments.`,
    );
  }
}

// The layered --primary/--ring fallback is only reached when the personalization binding is
// invalid, but "only reached on failure" is not a reason for it to be a colour we never ship.
{
  const want = rgb(shipped.accent) ? hexToHsl(shipped.accent) : null;
  for (const token of ['primary', 'ring']) {
    const m = new RegExp(`--${token}:\\s*([\\d.]+)\\s+([\\d.]+)%\\s+([\\d.]+)%;`).exec(css);
    if (!m) {
      fail(`${FILES.css}: the layered --${token} fallback is missing`);
      continue;
    }
    const got = { h: Number(m[1]), s: Number(m[2]), l: Number(m[3]) };
    if (want && (got.h !== want.h || got.s !== want.s || got.l !== want.l)) {
      fail(
        `${FILES.css}: the layered --${token} fallback is ${got.h} ${got.s}% ${got.l}% but the ` +
          `shipped accent ${shipped.accent} is ${want.h} ${want.s}% ${want.l}%.`,
      );
    }
  }
}

// frontend/src/types/personalization.ts
{
  const ts = read('types');
  const block = /DEFAULT_PERSONALIZATION[\s\S]*?colors:\s*\{([\s\S]*?)\}/.exec(ts);
  if (!block) fail(`${FILES.types}: could not read DEFAULT_PERSONALIZATION.colors`);
  const field = (name) => {
    const m = new RegExp(`${name}:\\s*"([^"]+)"`).exec(block?.[1] ?? '');
    return m ? m[1] : null;
  };
  const pairs = [
    ['accentColor', shipped.accent],
    ['sidebarBgColor', shipped.sidebarBg],
    ['sidebarTextColor', shipped.sidebarText],
  ];
  for (const [name, want] of pairs) {
    const got = field(name);
    if (got !== want) fail(`${FILES.types}: ${name} is ${got}, globals.css seeds ${want}`);
  }
  if (!/useCustomSidebarBg:\s*true/.test(block?.[1] ?? '')) {
    fail(
      `${FILES.types}: useCustomSidebarBg must default true, or the shipped sidebar colour is ` +
        `stored but never painted and the aside falls back to bg-card.`,
    );
  }
  // A default the picker cannot show as selected reads to an operator as "no preset chosen",
  // and clicking the swatch that LOOKS like the default silently changes their colours.
  for (const [list, want] of [
    ['COLOR_PRESETS', shipped.accent],
    ['SIDEBAR_BG_PRESETS', shipped.sidebarBg],
  ]) {
    const body = new RegExp(`${list}[\\s\\S]*?\\n\\];`).exec(ts)?.[0] ?? '';
    if (!body.includes(want)) fail(`${FILES.types}: ${list} does not contain the default ${want}`);
  }
  const themes = /THEME_PRESETS[\s\S]*?\n\];/.exec(ts)?.[0] ?? '';
  const hasShipped = new RegExp(
    `accentColor:\\s*"${shipped.accent}",\\s*sidebarBgColor:\\s*"${shipped.sidebarBg}",\\s*sidebarTextColor:\\s*"${shipped.sidebarText}"`,
  ).test(themes);
  if (!hasShipped) {
    fail(
      `${FILES.types}: THEME_PRESETS has no entry matching the shipped triple ` +
        `(${shipped.accent} / ${shipped.sidebarBg} / ${shipped.sidebarText}), so a fresh ` +
        `account shows no theme preset selected and has no way back to it.`,
    );
  }
}

// backend/app/routers/user_preferences.py
{
  const py = read('backend');
  const constant = (name) => new RegExp(`${name}\\s*=\\s*"([^"]+)"`).exec(py)?.[1] ?? null;
  const pairs = [
    ['DEFAULT_ACCENT_COLOR', shipped.accent],
    ['DEFAULT_SIDEBAR_BG_COLOR', shipped.sidebarBg],
    ['DEFAULT_SIDEBAR_TEXT_COLOR', shipped.sidebarText],
  ];
  for (const [name, want] of pairs) {
    const got = constant(name);
    if (got !== want) fail(`${FILES.backend}: ${name} is ${got}, globals.css seeds ${want}`);
  }
  // Two declarations of the same flag: the Pydantic field default and the dict
  // get_default_settings returns. Both are served, so both are checked.
  const declarations = [
    ['ColorSettings', /useCustomSidebarBg:\s*bool\s*=\s*(True|False)/],
    ['get_default_settings()', /"useCustomSidebarBg":\s*(True|False)/],
  ];
  for (const [where, re] of declarations) {
    const got = re.exec(py)?.[1];
    if (got !== 'True') {
      fail(
        `${FILES.backend}: useCustomSidebarBg is ${got ?? 'missing'} in ${where}; it must be ` +
          `True, or the shipped sidebar colour is stored and never painted.`,
      );
    }
  }
}

// frontend/src/components/personalization-provider.tsx and the pre-paint script in layout.tsx
// both spell the muted tiers. They must agree with each other and with the CSS seed.
{
  const provider = read('provider');
  const layout = read('layout');
  const onDark = /SIDEBAR_MUTED_ON_DARK\s*=\s*"([^"]+)"/.exec(provider)?.[1] ?? null;
  const onLight = /SIDEBAR_MUTED_ON_LIGHT\s*=\s*"([^"]+)"/.exec(provider)?.[1] ?? null;
  if (!onDark || !onLight) fail(`${FILES.provider}: SIDEBAR_MUTED_ON_DARK/LIGHT not found`);
  if (onDark && onDark !== shipped.sidebarMuted) {
    fail(
      `${FILES.provider}: SIDEBAR_MUTED_ON_DARK is ${onDark} but globals.css seeds ` +
        `--personalization-sidebar-muted as ${shipped.sidebarMuted}. The shipped ground is dark, ` +
        `so those are the same value in two files.`,
    );
  }
  for (const v of [onDark, onLight]) {
    if (v && !layout.includes(v)) {
      fail(
        `${FILES.layout}: the pre-paint script does not spell ${v}, so it derives a muted tier ` +
          `the provider will replace one tick later.`,
      );
    }
  }
  const textDark = /SIDEBAR_TEXT_ON_DARK\s*=\s*"([^"]+)"/.exec(provider)?.[1] ?? null;
  if (textDark && textDark !== shipped.sidebarText) {
    fail(
      `${FILES.provider}: autoTextColor derives ${textDark} for a dark ground, but the seed for ` +
        `the shipped (dark) ground is ${shipped.sidebarText}. First paint and hydration disagree.`,
    );
  }
}

// ---------------------------------------------------------------------------------------
// Rule 3: the shipped pair is legible
// ---------------------------------------------------------------------------------------

if (rgb(shipped.sidebarBg) && rgb(shipped.sidebarText)) {
  const bg = rgb(shipped.sidebarBg);
  const text = rgb(shipped.sidebarText);
  const accent = rgb(shipped.accent) ?? text;

  // Alpha is stripped and composited rather than ignored: reading rgba() as if it were opaque
  // is how a measurement flatters itself.
  const alpha = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)[,\s/]+([\d.]+)\s*\)/.exec(
    shipped.sidebarMuted ?? '',
  );

  const checks = [
    ['sidebar text on the sidebar ground', text, bg, 4.5],
    // The rules these two model live in globals.css; if one moves, the other must.
    ['the active pill against the sidebar ground', mix(text, accent, 0.88), bg, 4.5],
    ['the active pill under its own text', mix(text, accent, 0.88), bg, 4.5],
    ['a count chip under the sidebar text', text, mix(text, bg, 0.18), 4.5],
  ];
  if (alpha) {
    const muted = mix([+alpha[1], +alpha[2], +alpha[3]], bg, +alpha[4]);
    checks.push(['sidebar muted text on the sidebar ground', muted, bg, 4.5]);
  } else if (shipped.sidebarMuted) {
    fail(`${FILES.css}: --personalization-sidebar-muted (${shipped.sidebarMuted}) is not an rgba()`);
  }

  for (const [what, fg, surface, floor] of checks) {
    const ratio = contrast(fg, surface);
    if (ratio < floor) {
      fail(
        `contrast: ${what} measures ${ratio.toFixed(2)}:1, below the ${floor}:1 floor. The ` +
          `shipped default is the one colour every operator sees before choosing anything.`,
      );
    }
  }
}

// ---------------------------------------------------------------------------------------

if (offenders.length) {
  console.error('personalization defaults: FAILED\n');
  for (const o of offenders) console.error(`  - ${o}`);
  console.error('');
  process.exit(1);
}

console.log(
  `personalization defaults: accent ${shipped.accent}, sidebar ${shipped.sidebarBg} under ` +
    `${shipped.sidebarText}; five copies agree, triplets match their hexes, contrast measured`,
);
