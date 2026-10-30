#!/usr/bin/env node
// Structural guard: nothing in the console or terminal surface may reload the document.
//
// A document reload anywhere in this surface ends every live console and terminal session the
// page holds, not just the panel that failed. A retry button that reloads to recover one dead
// panel therefore takes out the rest of the workspace with it.
//
// Two of these shipped and were removed: the console window's retry and the ISO panel's. The
// comment left behind at components/console/console-window.tsx explains why the fetch is re-run
// instead. Nothing stops the next one being added, which is what this is for.
//
// A retry re-runs the request. A reconnect tears down and rebuilds the socket. Neither needs the
// document.
//
// Run: node scripts/checks/check-no-console-reload.mjs [repo-root]

import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// The surface where a document reload is destructive. Anything reachable while a console or
// terminal session is open.
const AREAS = [
  join('components', 'console'),
  join('components', 'instance-files'),
  join('components', 'terminal'),
  join('app', 'console'),
  join('app', 'terminal'),
];

// A reload written into a comment is documentation of its own removal, not a call.
const CALL = /(?<!\/\/.*)(?:window\.)?location\s*\.\s*reload\s*\(/;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.(tsx?|jsx?)$/.test(entry)) yield full;
  }
}

const offenders = [];
let scanned = 0;

for (const area of AREAS) {
  const dir = join(src, area);
  if (!existsSync(dir)) {
    offenders.push(`${area} no longer exists; update AREAS in this guard`);
    continue;
  }
  for (const file of walk(dir)) {
    scanned += 1;
    const lines = readFileSync(file, 'utf8').split('\n');
    lines.forEach((line, i) => {
      const code = line.split('//')[0];
      if (CALL.test(code)) {
        offenders.push(`${relative(src, file)}:${i + 1} reloads the document`);
      }
    });
  }
}

// A scan over nothing passes for the wrong reason.
if (scanned < 8) {
  offenders.push(`only ${scanned} files scanned across the console and terminal surface`);
}

if (offenders.length) {
  console.error('A document reload in the console or terminal surface kills every open session:');
  for (const o of offenders) console.error(`  ${o}`);
  console.error('\nRe-run the request instead. components/console/console-window.tsx shows the shape,');
  console.error('and carries the note explaining why the reload it replaced was destructive.');
  process.exit(1);
}
console.log(`console reload: ${scanned} files across the console and terminal surface, none reload`);
