#!/usr/bin/env node
// Structural guard: every capacity reading is drawn and captioned on the one usage ramp.
//
// A bar that says how full something is (memory, storage, a pool, vCPU or GPU allocation) is a
// severity signal, so its colour has to come from lib/usage-tone.ts, through UsageBar for the
// fill and UsageCaption for the number beside it. The defects this stops each shipped once:
// capacity drawn with the task-progress primitive in the purple accent, a bar recoloured with a
// `[&>div]:bg-*` override on its own scale, the 75/90 thresholds copied into a component, and a
// percentage toned by hand under a rule of its own.
//
// Rules
//   1. `<Progress` only in a file that shows task progress (upload, transfer, migration, unseal,
//      loader, rebuild). A capacity reading uses UsageBar.
//   2. No `[&>div]:bg-` fill override anywhere: a bar's colour is its band, not a call-site choice.
//   3. USAGE_CRITICAL_PERCENT and USAGE_ELEVATED_PERCENT are read only by the ramp itself and by
//      lib/cpu-allocation.ts, which derives its amber ratio from them.
//   4. usageTone() and toneForLevel() are called only by the primitives that draw the ramp.
//   5. No hand-built `bg-primary` bar sized by an inline width, outside task progress.
//
// Blind spot, counted in words rather than code: a literal `>= 75` or `>= 90` is not matched,
// because the loader's stage names use `< 90` for a different purpose.
//
// Run: node scripts/checks/check-capacity-bars.mjs [repo-root]

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// Task progress: how far a job has got, where a full bar is the goal. The accent is right there.
const TASK_PROGRESS = new Set([
  'app/inventory/migrations/components/migration-job-dialog.tsx',
  'app/inventory/migrations/components/migration-job-table.tsx',
  'app/inventory/migrations/components/migration-resource-table.tsx',
  // The Running metric card: running of total instances, where high is healthy.
  'app/inventory/monitoring/monitoring-content.tsx',
  'components/console/graphics-console.tsx',
  'components/host-health/unseal-vault-dialog.tsx',
  'components/image-upload-dialog.tsx',
  'components/instance-files/transfer-panel.tsx',
  'components/instance-files/transfer-tray.tsx',
  'components/iso-upload-dialog.tsx',
  'components/loading-lottie.tsx',
  'components/rebuild-dialog.tsx',
  'components/task-panel/task-detail-dialog.tsx',
  'components/ui/progress.tsx',
]);

const THRESHOLD_READERS = new Set(['lib/usage-tone.ts', 'lib/cpu-allocation.ts']);
const TONE_CALLERS = new Set([
  'lib/usage-tone.ts',
  'components/ui/usage-bar.tsx',
  'components/ui/usage-caption.tsx',
  'components/monitoring/gauge-chart.tsx',
]);

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else if (/\.(tsx?|jsx?)$/.test(name)) out.push(path);
  }
  return out;
}

const failures = [];
const fail = (file, line, message) => failures.push(`${file}:${line}: ${message}`);

for (const path of walk(src)) {
  const file = relative(src, path).split(sep).join('/');
  const text = readFileSync(path, 'utf8');
  const lines = text.split('\n');

  lines.forEach((line, i) => {
    const at = i + 1;
    if (/<Progress\b/.test(line) && !TASK_PROGRESS.has(file)) {
      fail(file, at, 'a capacity reading is drawn with UsageBar; <Progress> is for task progress (add the file to TASK_PROGRESS only if it is)');
    }
    if (line.includes('[&>div]:bg-')) {
      fail(file, at, 'a fill override recolours a bar outside its band; pass `level` to UsageBar instead');
    }
    if (/\bUSAGE_(CRITICAL|ELEVATED)_PERCENT\b/.test(line) && !THRESHOLD_READERS.has(file)) {
      fail(file, at, 'the 75/90 thresholds are read through usageLevel(), not copied');
    }
    if (/\b(usageTone|toneForLevel)\(/.test(line) && !TONE_CALLERS.has(file)) {
      fail(file, at, 'tone a capacity number with UsageCaption and a bar with UsageBar, not usageTone() at the call site');
    }
  });

  // A tag carrying both bg-primary and an inline width is a hand-built accent bar.
  for (const match of text.matchAll(/<div\b[^>]*>/g)) {
    const tag = match[0];
    if (/\bbg-primary\b/.test(tag) && /style=\{\{\s*width:/.test(tag) && !TASK_PROGRESS.has(file)) {
      const at = text.slice(0, match.index).split('\n').length;
      fail(file, at, 'a hand-built accent bar; a capacity reading uses UsageBar');
    }
  }
}

if (failures.length) {
  console.error(`check-capacity-bars: ${failures.length} problem(s)\n${failures.join('\n')}`);
  process.exit(1);
}
console.log('check-capacity-bars: every capacity bar and caption is on the usage ramp');
