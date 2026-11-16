#!/usr/bin/env node
// Structural guard: the orca mark keeps the colours it is drawn in.
//
// The orca loader is the product's brand mark. A personalization accent or a seller's branding
// colours buttons, the active menu item and the card illustrations, never the mark: a green brand
// once turned the orca green by rotating every fill of the animation. Rule 1: the player renders
// the animation JSON as shipped, with no colour helper, personalization read or filter. Rule 2: the
// animation JSON is loaded by that player only, so no second path can recolour it. Rule 3: no CSS
// filter or hue rotation sits on a line that renders an orca asset. Rule 4: a grey accent has no
// hue, so the illustrations stay as drawn; the helper and the pre-paint script share one threshold.
//
// Run: node scripts/checks/check-brand-mark-untinted.mjs [repo-root]

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');
const PLAYER = 'frontend/src/components/loading-lottie-player.tsx';
const ANIMATION = 'loading-lotttie-orca.json';
const offenders = [];
const fail = (msg) => offenders.push(msg);
const read = (path) => readFileSync(join(root, path), 'utf8');

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(tsx?|css)$/.test(name)) out.push(full);
  }
  return out;
}

// Rule 1
const player = read(PLAYER);
for (const [pattern, why] of [
  [/personalization/i, 'reads personalization'],
  [/@\/lib\/(color|personalization-colors)/, 'imports a colour helper'],
  [/hue|rotate|filter|recolou?r/i, 'turns or filters colours'],
]) {
  if (pattern.test(player.replace(/^\s*\/\/.*$/gm, ''))) fail(`${PLAYER}: the orca player ${why}; it must render the animation as shipped`);
}
if (!/animationData=\{loadingAnimation\}/.test(player)) {
  fail(`${PLAYER}: Lottie must receive the imported animation unchanged (animationData={loadingAnimation})`);
}

// Rules 2 and 3
for (const file of walk(src)) {
  const rel = relative(root, file);
  const text = readFileSync(file, 'utf8');
  // A quoted path is a load (import, fetch, src); a comment naming the file is not.
  if (rel !== PLAYER && /["'`][^"'`]*loading-lotttie-orca\.json["'`]/.test(text)) fail(`${rel}: loads ${ANIMATION}; only ${PLAYER} may`);
  text.split('\n').forEach((line, index) => {
    if (/orca-logo-|loading-lotttie-orca|LottiePlayer|LoadingLottie/.test(line) && /hue-rotate|filter\s*:|\bfilter=|\[filter/.test(line)) {
      fail(`${rel}:${index + 1}: a filter on a line that renders the orca mark`);
    }
  });
}

// Rule 4
const colors = read('frontend/src/lib/personalization-colors.ts');
const layout = read('frontend/src/app/layout.tsx');
const threshold = /export const ACHROMATIC_SATURATION = (\d+);/.exec(colors)?.[1];
if (!threshold) fail('frontend/src/lib/personalization-colors.ts: ACHROMATIC_SATURATION is missing');
else {
  if (!/current\.s < ACHROMATIC_SATURATION\)\s*return 0/.test(colors)) {
    fail('frontend/src/lib/personalization-colors.ts: accentHueShift must return 0 below ACHROMATIC_SATURATION');
  }
  if (!new RegExp(`var shift = a\\[1\\] < ${threshold} \\? 0 :`).test(layout)) {
    fail(`frontend/src/app/layout.tsx: the pre-paint hue shift must be 0 below saturation ${threshold}, as accentHueShift is`);
  }
}

if (offenders.length) {
  console.error('brand mark untinted: FAILED\n');
  for (const o of offenders) console.error(`  - ${o}`);
  console.error('');
  process.exit(1);
}
console.log('brand mark untinted: the orca renders as drawn, one loader path, no filter, grey accent shifts nothing');
