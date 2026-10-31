#!/usr/bin/env node
// Structural guard: semantic colour comes from a token, and a status chip is solid.
//
// The shape this bans is `bg-<hue>-500/15` behind `text-<hue>-600`: a translucent tint of a
// palette step with a saturated step of the SAME hue written on top of it. It is the most
// recognisable generated-UI signature in this app, it spread by copy-paste rather than by
// decision, and documentation/ui/frontend-design-language.md has banned it in prose since the
// anti-slop pass. Prose was all there was, which is why it reached four of the nine variants
// of Badge itself: the component the doc pointed people at was an example of the defect.
//
// It is also a WCAG failure, not only a taste one. Composited over --card in light mode, at
// the 12px semibold the Badge base class asks for, so the 4.5:1 floor applies and not 3:1:
//
//   success  bg-emerald-500/15 + text-emerald-600   3.15:1
//   warning  bg-amber-500/15   + text-amber-600     2.73:1
//   error    bg-red-500/15     + text-red-600       3.82:1
//
// Rule 3 measures that, because the obvious fix is not one. Darkening every text a palette
// step takes success to 4.57 and error to 5.12 and leaves warning at 4.30, so a half-fix that
// reads as a fix still ships a failure. A ban stops the shape. Only a measurement stops the
// shape being replaced by a different failing one.
//
// Deliberately parser-free and dependency-free. The CI `guards` job runs checkout and
// setup-node and no `npm ci`, so importing typescript or tailwindcss/colors from
// frontend/node_modules would fail there. Same rationale as check-jsx-stray-attrs.mjs. The
// consequence is that the palette hexes below are a copy rather than a read. Tailwind is
// pinned at ^3.4.1 and the v3 default palette is frozen, so the copy cannot drift, and a
// hue-step this file does not carry is reported rather than skipped: skipping is how a scan
// reads zero for the wrong reason.
//
// Run: node scripts/checks/check-semantic-colour.mjs [repo-root]

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// ---------------------------------------------------------------------------------------
// Scope
// ---------------------------------------------------------------------------------------

// The Cluster submenu, as defined by components/sidebar/nav-config.tsx, plus the certificates
// child route. Every raw palette class in this tree was cleared, so the ban below can be
// absolute here without grandfathering anything.
const CLEARED = [
  'app/inventory/clusters/',
  'app/inventory/clustering/',
  'app/inventory/server/',
  'app/inventory/operations/',
  'app/inventory/warnings/',
  // The member resource dialog renders inside app/inventory/clustering/ but lives under
  // components/, so the route prefixes above never reached it. It carried a four-step
  // utilisation ramp in raw palette steps the whole time this ban was described as absolute.
  'components/member-resources/',
  // The task dock is new and starts clean: Badge tones and --status-* dots only.
  'components/task-panel/',
  // Settings > Dashboard Host: its utilisation bars are the shared UsageBar ramp, not the
  // green-to-red palette steps they started as.
  'components/system-resources/',
];

// Console and terminal chrome does not follow the theme: it paints its own fixed dark surface
// (the --terminal-* tokens, reached as bg-terminal and friends), so a token tuned against --card
// is the wrong measurement there. Counted rather than ignored, so the exemption stays visible.
// That palette has its own guard: scripts/checks/check-terminal-theme.mjs.
const CHROME = [/^app\/console\//, /^app\/terminal\//, /^components\/console\//, /^components\/terminal\//];

// Rendered by nothing, so nothing in it can reach an operator. Counting it would mean
// either a permanently failing check or a baseline that hides real regressions behind dead
// code. Same reasoning as check-icon-button-names.mjs. (vm-table.tsx was deleted.)
const DEAD = new Set(['components/tag-manager-legacy.tsx']);

// ---------------------------------------------------------------------------------------
// Baselines
// ---------------------------------------------------------------------------------------

// The tint-plus-saturated-text pairing everywhere outside the cleared tree. This is what stops
// the pattern spreading into the surfaces the Cluster submenu change deliberately left alone.
// Ratchet: may fall, never rise. At zero since the 2026-10-29 sweep.
const TINT_PAIR_BASELINE = 0;

// A <Badge> naming its own foreground colour at the call site. Badge exists so a chip's colour
// is one decision in one file, and a className override is how the same semantic ends up in
// two shades in two files.
// Ratchet: may fall, never rise.
const BADGE_OVERRIDE_BASELINE = 0;

// --- counted blind spots ----------------------------------------------------------------
//
// important: every rule here reads class strings out of the source. Three shapes put the same
// colour somewhere a class string is not. Counting them keeps the headline from reading as
// "every tinted pill in the app", which is the lesson recorded in
// documentation/ui/table-conventions.md: a guard that inspects only the element you thought of
// misses the same defect one level deeper.

// A tint computed at runtime, `style={{ backgroundColor: `${color}15` }}`. The last live site was
// the tag chip in components/tag-manager.tsx. Its colour is data (getTagColor derives it from the
// tag's prefix), so it now lives in a square swatch beside neutral text (components/tag-chips.tsx)
// instead of tinting the chip under same-hue text that measured near 2:1.
// Ratchet: may fall, never rise.
const COMPUTED_TINT_BASELINE = 0;

// A tint and its foreground split across two keys of one config object. Pairing arbitrary
// sibling keys needs a parser.
// Ratchet: may fall, never rise.
const SPLIT_KEY_BASELINE = 0;

// An at-rest tint with no same-string palette foreground. Most are correct: an alert container
// whose prose inherits --foreground and whose icon alone is amber is the sanctioned shape, and
// a proximity rule would fire on the reference implementation. This is the population a genuine
// cross-line pair could hide inside.
// Ratchet: may fall, never rise. At zero since the 2026-10-29 sweep moved the last alert
// containers onto a token border and a toned icon, and the chrome hovers onto bg-terminal-raised.
const UNPAIRED_TINT_BASELINE = 0;

// --- measured contrast failures ---------------------------------------------------------
//
// Every entry is a pair rule 3 measures and a written reason it is still here. Checked in BOTH
// directions: a pair that got worse fails, a pair not listed that fails at all fails, and a
// listed pair that now PASSES also fails, so the map cannot go stale claiming a defect somebody
// already fixed.
const KNOWN_CONTRAST_FAILURES = new Map([
  // Numbers are the worse of --card and --background, which is what the rule scores.
  // A token change, not a call-site change. --destructive at 0 84.2% 60.2% is too light to
  // carry white, and moving it moves every destructive Button and confirm dialog at once.
  ['badge#destructive#light', 3.61],
  ['button#destructive#light', 3.61],
  // The same token read the other way round: text-destructive on a plain surface. The token fix
  // above retires this one too, so it is carried here rather than patched in alert.tsx.
  ['alert#destructive#light', 3.42],
  // Same shape on --success and --warning. bg-warning under text-warning-foreground at 2.13:1
  // is the worst pair in the system, and it is white on amber, so no lightening of the
  // foreground rescues it. The token has to darken.
  ['button#success#light', 3.33],
  ['button#warning#light', 2.13],
  ['button#warning#dark', 3.29],
  // The muted badge used to sit here at 3.79. It was recorded as an app-wide typography
  // decision rather than a badge one, and that decision was taken: --muted-foreground moved
  // from 47% to 42% so it clears AA on --card, --background and --muted alike. Retired rather
  // than relaxed, which is why the entry is gone instead of renumbered.
  // --- the --primary family -----------------------------------------------------------
  //
  // Five entries, and four of them are new here without anything on screen having got worse.
  // They were invisible rather than absent. globals.css seeded --personalization-accent as
  // #8b5cf6 while its own -h/-s/-l triplet read 262 83% 58%, which is #7c3bed, and the reader
  // below takes the triplet. So this file scored #7c3bed: a colour the browser paints only
  // until React mounts and then discards. #8b5cf6, the one an operator actually looks at,
  // measured 4.23 for the solid pairs and 3.85 for the light link the whole time. Making the
  // seed self-consistent is what surfaced them; scripts/checks/check-personalization-defaults.mjs
  // is what stops the two halves separating again.
  //
  // The accent is now #8467e4, the sidebar ground's own hue at the luminance where one value
  // serving both themes is equidistant from AA on a light and a dark surface. Every number
  // below moves by less than 0.1 against what shipped before it.
  //
  // All five are carried for the reason the link variant always was: --primary is the
  // user-configurable personalization accent and the picker has no contrast clamp, so any
  // value chosen here is one an operator can overwrite the same afternoon. Darkening the token
  // does not retire them, and it cannot: white-on-accent wants a dark accent, accent-on-dark
  // wants a light one, and the two requirements cross. What retires them is splitting the
  // brand hex into roles, a solid fill and a separately clamped text colour, the way Radix
  // Colors separates step 9 from step 11. That is a token-system change across ~35 call sites
  // and belongs in its own pass, recorded in documentation/ui/personalization-defaults.md.
  ['badge#default#light', 4.17],
  ['badge#default#dark', 4.17],
  ['button#default#light', 4.17],
  ['button#default#dark', 4.17],
  ['button#link#light', 3.78],
  // 4.18, not the 3.07 recorded before, and the difference is the seed defect above rather
  // than a change in what an operator sees: #8b5cf6 measured 4.11 here.
  ['button#link#dark', 4.18],
]);

// ---------------------------------------------------------------------------------------
// Colour
// ---------------------------------------------------------------------------------------

const HUES = [
  'slate', 'gray', 'zinc', 'neutral', 'stone', 'red', 'orange', 'amber', 'yellow', 'lime',
  'green', 'emerald', 'teal', 'cyan', 'sky', 'blue', 'indigo', 'violet', 'purple', 'fuchsia',
  'pink', 'rose',
];
const HUE = `(?:${HUES.join('|')})`;

// Tailwind v3.4 defaults for the steps this tree actually spells.
const PALETTE = {
  'emerald-400': '#34d399', 'emerald-500': '#10b981', 'emerald-600': '#059669', 'emerald-700': '#047857',
  'green-400': '#4ade80', 'green-500': '#22c55e', 'green-600': '#16a34a', 'green-700': '#15803d',
  'amber-400': '#fbbf24', 'amber-500': '#f59e0b', 'amber-600': '#d97706', 'amber-700': '#b45309',
  'yellow-200': '#fef08a', 'yellow-300': '#fde047', 'yellow-400': '#facc15', 'yellow-500': '#eab308', 'yellow-600': '#ca8a04',
  'orange-400': '#fb923c', 'orange-500': '#f97316', 'orange-600': '#ea580c', 'orange-700': '#c2410c',
  'red-400': '#f87171', 'red-500': '#ef4444', 'red-600': '#dc2626', 'red-700': '#b91c1c',
  'rose-400': '#fb7185', 'rose-500': '#f43f5e', 'rose-600': '#e11d48',
  'blue-400': '#60a5fa', 'blue-500': '#3b82f6', 'blue-600': '#2563eb', 'blue-700': '#1d4ed8',
  'sky-400': '#38bdf8', 'sky-500': '#0ea5e9', 'sky-600': '#0284c7',
  'cyan-400': '#22d3ee', 'cyan-500': '#06b6d4', 'cyan-600': '#0891b2',
  'purple-400': '#c084fc', 'purple-500': '#a855f7', 'purple-600': '#9333ea',
  'violet-400': '#a78bfa', 'violet-500': '#8b5cf6', 'violet-600': '#7c3aed',
  'gray-100': '#f3f4f6', 'gray-200': '#e5e7eb', 'gray-300': '#d1d5db', 'gray-400': '#9ca3af',
  'gray-500': '#6b7280', 'gray-600': '#4b5563', 'gray-700': '#374151', 'gray-800': '#1f2937',
  'zinc-400': '#a1a1aa', 'zinc-500': '#71717a',
  'slate-400': '#94a3b8', 'slate-500': '#64748b', 'slate-600': '#475569',
};

const hexToRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

function hslToRgb(h, s, l) {
  s /= 100;
  l /= 100;
  const k = (n) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [f(0), f(8), f(4)].map((v) => Math.round(v * 255));
}

const channel = (c) => {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
};
const luminance = ([r, g, b]) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);

function contrast(fg, bg) {
  const a = luminance(fg);
  const b = luminance(bg);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/** Source-over composite of `fg` at `alpha` onto an opaque `bg`. */
const over = (fg, alpha, bg) => fg.map((c, i) => Math.round(alpha * c + (1 - alpha) * bg[i]));

const offenders = [];

// ---------------------------------------------------------------------------------------
// Tokens, read from globals.css so the half most likely to move can never go stale
// ---------------------------------------------------------------------------------------

const GLOBALS = join(src, 'app', 'globals.css');
const css = readFileSync(GLOBALS, 'utf8');

/** Every `--name: <h> <s>% <l>%;` inside the first brace-balanced block opened by `selector`. */
function tokenBlock(source, selector) {
  const at = source.indexOf(selector);
  if (at === -1) return null;
  const open = source.indexOf('{', at);
  let depth = 0;
  let end = -1;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  if (end === -1) return null;
  const out = new Map();
  for (const m of source.slice(open, end).matchAll(/--([a-z0-9-]+):\s*([0-9.]+)\s+([0-9.]+)%\s+([0-9.]+)%/g)) {
    out.set(m[1], hslToRgb(Number(m[2]), Number(m[3]), Number(m[4])));
  }
  return out;
}

const light = tokenBlock(css, ':root');
const dark = tokenBlock(css, '.dark');
if (!light || !dark) offenders.push('globals.css: could not read the :root or .dark token block');

// important: the :root block inside @layer base is not the last word on --primary. globals.css
// rebinds --primary and --ring from the personalization accent in an UNLAYERED :root, and an
// unlayered rule beats a layered one at equal specificity. That override therefore wins in BOTH
// themes, which makes the .dark value dead. Reading the layered value would give the wrong
// dark-mode number for every --primary pair, and that is the pair with the widest blast radius
// because the accent is user-configurable with no contrast clamp.
const accent = (() => {
  const h = css.match(/--personalization-accent-h:\s*([0-9.]+)/);
  const s = css.match(/--personalization-accent-s:\s*([0-9.]+)%/);
  const l = css.match(/--personalization-accent-l:\s*([0-9.]+)%/);
  return h && s && l ? hslToRgb(Number(h[1]), Number(s[1]), Number(l[1])) : null;
})();
if (!accent) offenders.push('globals.css: could not read --personalization-accent-h/s/l');
if (light && accent) {
  light.set('primary', accent);
  light.set('ring', accent);
}
if (dark && accent) {
  dark.set('primary', accent);
  dark.set('ring', accent);
}

const THEMES = { light, dark };

// The fixed-dark console and terminal chrome rebinds the status pair to its dark-theme values
// through .terminal-status, because a light-theme chip measures 3.30:1 on that ground. That
// block is a copy, so it is held equal here: a dark status tuned for contrast must not leave a
// stale twin on the chrome.
const chromeStatus = tokenBlock(css, '.terminal-status');
if (!chromeStatus) offenders.push('globals.css: could not read the .terminal-status token block');
else if (dark) {
  for (const [name, rgb] of chromeStatus) {
    const want = dark.get(name);
    if (!name.startsWith('status-')) {
      offenders.push(`globals.css .terminal-status declares --${name}; it may only rebind --status-* tokens`);
    } else if (!want || want.join() !== rgb.join()) {
      offenders.push(`globals.css .terminal-status --${name} differs from its .dark value`);
    }
  }
  for (const name of dark.keys()) {
    if (name.startsWith('status-') && !chromeStatus.has(name)) {
      offenders.push(`globals.css .terminal-status is missing --${name}, which .dark declares`);
    }
  }
}

/** Resolve one utility's colour, or null when it carries none of its own. */
function resolve(kind, rest, theme, where) {
  if (/^(transparent|current|inherit|none|white|black)$/.test(rest)) return null;
  const step = rest.match(new RegExp(`^(${HUE})-(\\d{2,3})$`));
  if (step) {
    const hex = PALETTE[`${step[1]}-${step[2]}`];
    if (!hex) {
      offenders.push(`${where}: ${kind}-${rest} is a palette step this guard does not carry, add it to PALETTE`);
      return null;
    }
    return hexToRgb(hex);
  }
  return THEMES[theme]?.get(rest) ?? null;
}

// ---------------------------------------------------------------------------------------
// Source scanning
// ---------------------------------------------------------------------------------------

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.(tsx|jsx)$/.test(entry)) yield full;
  }
}

// At rest means no state or breakpoint modifier. `dark:` is the one modifier that counts,
// because `dark:text-emerald-400` is half of the pattern rather than a hover affordance.
// Anchoring this way excludes hover:, focus:, group-hover:, data-[...]: and sm:/md:/lg: in one
// stroke, which is what keeps `hover:bg-primary/90` out of every rule below with no exception
// list: opacity on a solid surface for a hover state is the correct idiom.
const TINT = new RegExp(`(?:^|\\s)(?:dark:)?bg-${HUE}-\\d{2,3}/\\d{1,3}(?=\\s|$)`);
const PALETTE_FG = new RegExp(`(?:^|\\s)(?:dark:)?text-${HUE}-\\d{2,3}(?=\\s|$)`);
const PALETTE_ANY = new RegExp(
  `(?:^|[\\s"\`{(])(?:dark:)?(?:bg|text|border|ring|fill|stroke|from|to|via)-${HUE}-\\d{2,3}(?:/\\d{1,3})?(?=[\\s"\`)},]|$)`,
  'g',
);
const PALETTE_FG_IN_TAG = new RegExp(`(?:^|[\\s"\`{(])(?:dark:)?text-${HUE}-\\d{2,3}(?=[\\s"\`)},]|$)`, 'g');

// A Tailwind arbitrary value carrying a literal colour, ex; `bg-[#1a1b26]`, `dark:text-[#c0caf5]`.
//
// Every rule above is shaped like hue-plus-step, so an arbitrary hex slips past all of them: it is
// neither a palette step to ban nor a token to measure, and no counter moves when one appears.
// That is a hole, not a permission. It stays closed by naming the utilities that take a colour, so
// a sizing arbitrary (`w-[var(--radix-...)]`, `text-[10px]`, `max-h-[85vh]`) is untouched.
const ARBITRARY_COLOUR = new RegExp(
  `(?:^|[\\s"\`{(])(?:[a-z-]+:)?(?:bg|text|border|ring|fill|stroke|from|to|via|decoration|outline|shadow|accent|caret|divide|placeholder)-\\[#[0-9a-fA-F]{3,8}\\]`,
  'g',
);

// The one file allowed to paint a fixed surface outside the chrome trees.
//
// A snippet is destined for a shell or a config file, so CodeBlock paints it as terminal output
// rather than as themed app surface. It reuses the xterm surface and green this app already ships
// rather than inventing a third dark surface, and it carries the reasoning in its own header. It
// is listed here by exact path so the exemption cannot spread by directory.
const FIXED_SURFACE = new Set(['components/ui/code-block.tsx']);

/** Every class string in a file: quoted literals plus each literal chunk of a template. */
function classStrings(code) {
  const out = [];
  for (const m of code.matchAll(/"([^"\n]*)"/g)) out.push([m[1], m.index]);
  for (const m of code.matchAll(/`([^`]*)`/g)) {
    for (const part of m[1].split(/\$\{[^}]*\}/)) out.push([part, m.index]);
  }
  return out;
}

/** Opening tags for `name` with their body, brace- and quote-aware. */
function tags(code, name) {
  const found = [];
  const open = new RegExp(`<${name}\\b`, 'g');
  let m;
  while ((m = open.exec(code)) !== null) {
    let i = m.index + m[0].length;
    let depth = 0;
    let quote = null;
    for (; i < code.length; i += 1) {
      const c = code[i];
      if (quote) {
        if (c === quote) quote = null;
        continue;
      }
      if (c === '"' || c === "'" || c === '`') quote = c;
      else if (c === '{') depth += 1;
      else if (c === '}') depth -= 1;
      else if (c === '>' && depth === 0) break;
    }
    found.push({ tag: code.slice(m.index, i + 1), index: m.index });
  }
  return found;
}

const lineOf = (code, index) => code.slice(0, index).split('\n').length;

/**
 * A per-site waiver, on the same line or the one above. Per SITE, never per file: a file-wide
 * form is what made an earlier guard inert for eight columns at once.
 */
function waived(code, index) {
  const before = code.slice(0, index).split('\n');
  const here = code.split('\n')[before.length - 1] ?? '';
  const above = before[before.length - 2] ?? '';
  return /\/\/\s*semantic-colour:/.test(here) || /\/\/\s*semantic-colour:/.test(above);
}

let files = 0;
let badgeTags = 0;
let tintPairs = 0;
let badgeOverrides = 0;
let chromeHits = 0;
let computedTints = 0;
let splitKeyTints = 0;
let unpairedTints = 0;
const blindSpots = [];

for (const file of walk(src)) {
  files += 1;
  const code = readFileSync(file, 'utf8');
  const rel = relative(src, file).split(sep).join('/');
  const isChrome = CHROME.some((r) => r.test(rel));
  const isDead = DEAD.has(rel);
  const isCleared = CLEARED.some((p) => rel.startsWith(p));
  const badges = tags(code, 'Badge');
  badgeTags += badges.length;

  // --- Rule 1: no raw palette at all inside the cleared tree --------------------------
  //
  // important: a hard ban, never a ratchet. The Cluster submenu change cleared every one of
  // these to zero, so there is no legitimate existing case to grandfather, and one surviving
  // example is what feeds the next copy-paste. That is the documented spread mechanism.
  if (isCleared) {
    PALETTE_ANY.lastIndex = 0;
    let m;
    while ((m = PALETTE_ANY.exec(code)) !== null) {
      if (waived(code, m.index)) continue;
      offenders.push(
        `${rel}:${lineOf(code, m.index)} writes a raw palette step "${m[0].replace(/^[^a-z]+/, '')}" inside the Cluster submenu. ` +
          'Use a --status-* token, or a Badge tone variant.',
      );
    }
  }

  // --- Rule 1b: no arbitrary colour hex outside the fixed-surface trees ---------------
  //
  // A hard ban for the same reason Rule 1 is one: a census over frontend/src found zero of these
  // outside the chrome trees, so there is nothing to grandfather and no baseline to drift. The
  // chrome trees and CodeBlock paint a fixed surface on purpose and say so in place; everywhere
  // else a colour comes from a token, and a hex is how a screen quietly stops following the theme.
  if (!isChrome && !isDead && !FIXED_SURFACE.has(rel)) {
    ARBITRARY_COLOUR.lastIndex = 0;
    let a;
    while ((a = ARBITRARY_COLOUR.exec(code)) !== null) {
      if (waived(code, a.index)) continue;
      offenders.push(
        `${rel}:${lineOf(code, a.index)} paints "${a[0].replace(/^[^a-z]+/, '')}", an arbitrary colour ` +
          'that follows no theme. Use a token, or state the case in FIXED_SURFACE.',
      );
    }
  }

  for (const [text, index] of classStrings(code)) {
    const hasTint = TINT.test(text);
    const hasPaletteFg = PALETTE_FG.test(text);

    // --- Rule 2: a tint and a palette foreground in one class string ------------------
    if (hasTint && hasPaletteFg && !isDead && !waived(code, index)) {
      if (isChrome) chromeHits += 1;
      else tintPairs += 1;
    }
    if (hasTint && !hasPaletteFg && !isDead) unpairedTints += 1;
  }

  // --- a Badge naming its own foreground colour --------------------------------------
  for (const { tag, index } of badges) {
    PALETTE_FG_IN_TAG.lastIndex = 0;
    if (!PALETTE_FG_IN_TAG.test(tag)) continue;
    if (isDead || isChrome || waived(code, index)) continue;
    badgeOverrides += 1;
  }

  // --- counted blind spots ------------------------------------------------------------
  for (const m of code.matchAll(/backgroundColor:\s*`[^`]*\$\{[^}]*\}[^`]*`/g)) {
    computedTints += 1;
    blindSpots.push(`  computed tint      ${rel}:${lineOf(code, m.index)}`);
  }
  for (const m of code.matchAll(/\bbgColor:\s*"[^"]*"/g)) {
    splitKeyTints += 1;
    blindSpots.push(`  split config keys  ${rel}:${lineOf(code, m.index)}`);
  }
}

// ---------------------------------------------------------------------------------------
// Rule 3: measure every variant of every cva primitive, in both themes
// ---------------------------------------------------------------------------------------
//
// Rules 1 and 2 stop the SHAPE. This stops the shape being replaced by a different failing
// one, and it reaches what no ban can: `bg-warning text-warning-foreground` carries no tint
// and no palette step and measures 2.13:1.

/** Split a cva file into [baseClassString, Map<variantName, classString>]. */
function readVariants(code) {
  const cvaAt = code.indexOf('cva(');
  if (cvaAt === -1) return null;
  const base = code.slice(cvaAt).match(/"((?:[^"\\]|\\.)*)"/);
  const blockAt = code.indexOf('variant: {', cvaAt);
  if (!base || blockAt === -1) return null;
  let depth = 0;
  let end = -1;
  for (let i = code.indexOf('{', blockAt); i < code.length; i += 1) {
    if (code[i] === '{') depth += 1;
    else if (code[i] === '}') {
      depth -= 1;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  if (end === -1) return null;
  const variants = new Map();
  for (const m of code.slice(blockAt, end).matchAll(/(\w+):\s*\n?\s*"((?:[^"\\]|\\.)*)"/g)) {
    variants.set(m[1], m[2]);
  }
  return [base[1], variants];
}

/** WCAG 1.4.3: 3:1 for 24px and up, or 18.66px and up when bold. 4.5:1 otherwise. */
function floorFor(base) {
  const px = { 'text-xs': 12, 'text-sm': 14, 'text-base': 16, 'text-lg': 18, 'text-xl': 20, 'text-2xl': 24 };
  const size = Object.keys(px).find((k) => new RegExp(`(?:^|\\s)${k}(?=\\s|$)`).test(base));
  const pt = size ? px[size] : 16;
  const bold = /(?:^|\s)font-(?:semibold|bold|extrabold|black)(?=\s|$)/.test(base);
  return pt >= 24 || (pt >= 18.66 && bold) ? 3 : 4.5;
}

const measured = [];
const CVAS = [
  ['badge', join(src, 'components', 'ui', 'badge.tsx')],
  ['button', join(src, 'components', 'ui', 'button.tsx')],
  ['alert', join(src, 'components', 'ui', 'alert.tsx')],
];

for (const [name, path] of CVAS) {
  const parsed = readVariants(readFileSync(path, 'utf8'));
  if (!parsed) {
    offenders.push(`${name}: could not read its cva block, so this guard measures nothing for it`);
    continue;
  }
  const [base, variants] = parsed;
  const floor = floorFor(base);

  for (const [variant, classes] of variants) {
    const list = classes.split(/\s+/).filter(Boolean);
    for (const theme of ['light', 'dark']) {
      // important: in dark mode a `dark:` utility OVERRIDES its unmodified twin, so the
      // resolved list has to prefer it. Taking the first match instead measures the light
      // foreground against the dark backdrop, which invents failures that do not render. The
      // first version of this file did exactly that and reported badge "info" at 3.33:1 in
      // dark, where it actually measures 6.77:1.
      const rest = [];
      for (const c of list) {
        if (c.startsWith('dark:')) {
          if (theme === 'dark') rest.push(c.slice(5));
        } else if (!c.includes(':')) {
          rest.push(c);
        }
      }
      const pick = (test) => {
        let out;
        for (const c of rest) if (test(c)) out = c;
        return out;
      };

      const bgClass = pick((c) => c.startsWith('bg-'));
      const fgClass = pick((c) => c.startsWith('text-') && !/^text-(xs|sm|base|lg|\d?xl|\[)/.test(c));
      if (!fgClass) continue;

      const where = `${relative(root, path)} variant "${variant}"`;
      const fg = resolve('text', fgClass.slice(5), theme, where);
      if (!fg) continue;

      // Both surfaces a chip actually sits on, scored on the worse of the two. A chip that
      // clears its floor on --card and fails on --background has still failed, and --background
      // is the darker of the two in light mode.
      let worst = Infinity;
      let worstSurface = null;
      for (const surfaceName of ['card', 'background']) {
        const surface = THEMES[theme]?.get(surfaceName);
        if (!surface) continue;

        let backdrop = surface;
        if (bgClass) {
          const parts = bgClass.slice(3).match(/^(.*?)(?:\/(\d{1,3}))?$/);
          const colour = resolve('bg', parts[1], theme, where);
          if (colour) backdrop = over(colour, parts[2] ? Number(parts[2]) / 100 : 1, surface);
        }

        const ratio = contrast(fg, backdrop);
        if (ratio < worst) {
          worst = ratio;
          worstSurface = surfaceName;
        }
      }
      if (worstSurface === null) continue;

      const key = `${name}#${variant}#${theme}`;
      measured.push({ key, ratio: worst, floor });

      if (worst >= floor) continue;
      const known = KNOWN_CONTRAST_FAILURES.get(key);
      if (known === undefined) {
        offenders.push(
          `${where} measures ${worst.toFixed(2)}:1 on --${worstSurface} in ${theme} mode, below the ` +
            `${floor}:1 floor its own base classes require.`,
        );
      } else if (Math.abs(worst - known) > 0.05) {
        // important: the comparison is two-sided, not "must not get worse". A one-sided rule
        // accepts the half-fix, and the half-fix is the thing this rule exists to reject:
        // darkening every tinted badge text one palette step moves warning from 2.61 to 4.30,
        // which is an improvement and still a failure. Anything between the recorded number
        // and the floor would then pass silently under a number nobody re-measured. Moving a
        // carried failure means either finishing it, or recording where it actually landed.
        const direction = worst < known ? 'fell from' : 'moved off';
        offenders.push(
          `${where} ${direction} its recorded ${known}:1 to ${worst.toFixed(2)}:1 on --${worstSurface} in ` +
            `${theme} mode, and still misses the ${floor}:1 floor. Either finish the fix, or ` +
            'update the number in KNOWN_CONTRAST_FAILURES.',
        );
      }
    }
  }
}

// The other direction: a recorded failure that now passes everywhere is a stale entry, and a
// stale entry is a claim this file makes about the product that is no longer true.
for (const key of KNOWN_CONTRAST_FAILURES.keys()) {
  const rows = measured.filter((m) => m.key === key);
  if (!rows.length) {
    offenders.push(`KNOWN_CONTRAST_FAILURES has "${key}" but nothing by that name is measured any more`);
  } else if (rows.every((r) => r.ratio >= r.floor)) {
    offenders.push(`KNOWN_CONTRAST_FAILURES still lists "${key}" but it now passes. Delete the entry.`);
  }
}

// ---------------------------------------------------------------------------------------
// Ratchets
// ---------------------------------------------------------------------------------------

for (const [label, count, baseline] of [
  ['tint under same-hue text', tintPairs, TINT_PAIR_BASELINE],
  ['Badges naming their own colour', badgeOverrides, BADGE_OVERRIDE_BASELINE],
  ['tints computed at runtime', computedTints, COMPUTED_TINT_BASELINE],
  ['tints split across two config keys', splitKeyTints, SPLIT_KEY_BASELINE],
  ['at-rest tints whose foreground could be on another line', unpairedTints, UNPAIRED_TINT_BASELINE],
]) {
  if (count > baseline) {
    offenders.push(`${label}: ${count} (baseline ${baseline}, may fall and never rise)`);
  }
}

// A scan over nothing passes for the wrong reason. The third floor matters most: rewrite
// badge.tsx into a shape readVariants cannot parse and the contrast rule would otherwise
// measure nothing and report success.
if (files < 150) offenders.push(`only ${files} .tsx files scanned under frontend/src, expected at least 150`);
if (badgeTags < 100) offenders.push(`only ${badgeTags} <Badge> tags found, expected at least 100`);
if (measured.length < 20) offenders.push(`only ${measured.length} colour pairs measured, expected at least 20`);

// ---------------------------------------------------------------------------------------
// Report
// ---------------------------------------------------------------------------------------

console.log(`semantic colour: ${files} files, ${badgeTags} badges, ${measured.length} colour pairs measured`);
console.log(`  Cluster submenu raw palette: 0 (hard ban across ${CLEARED.length} trees)`);
console.log(
  `  arbitrary colour hex: 0 outside the chrome trees (hard ban, ${FIXED_SURFACE.size} named exemption)`,
);
console.log(`  tint under same-hue text: ${tintPairs} (baseline ${TINT_PAIR_BASELINE}), ${chromeHits} more inside the fixed-dark chrome exemption`);
console.log(`  Badges naming their own colour: ${badgeOverrides} (baseline ${BADGE_OVERRIDE_BASELINE})`);
console.log(
  `  blind spots this file cannot see: ${computedTints} computed (baseline ${COMPUTED_TINT_BASELINE}), ` +
    `${splitKeyTints} split-key (baseline ${SPLIT_KEY_BASELINE}), ${unpairedTints} unpaired tints (baseline ${UNPAIRED_TINT_BASELINE})`,
);
for (const b of blindSpots) console.log(b);
console.log(`  contrast failures carried with a written reason: ${KNOWN_CONTRAST_FAILURES.size}`);

if (offenders.length) {
  console.error('\nSemantic colour:');
  for (const o of offenders) console.error(`  ${o}`);
  console.error('\nA status is a token, and a chip is solid. Use a Badge tone variant');
  console.error('(positive, caution, neutral, negative), or plain text where a chip is too loud:');
  console.error('  components/ui/badge.tsx        the tone variants and their measurements');
  console.error('  lib/cluster-status.ts          the one status vocabulary');
  console.error('  components/terminal/session-status.tsx   the chip on fixed-dark chrome');
  console.error('See documentation/ui/frontend-design-language.md, "Status colour is a token,');
  console.error('and a pill is not the default container".');
  process.exit(1);
}

console.log('semantic colour: the Cluster submenu is clean, and no variant regressed');
