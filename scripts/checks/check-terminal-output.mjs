#!/usr/bin/env node
// Structural guard: text that did not come from the PTY reaches xterm through terminalSafe.
//
// xterm executes what it is given. An instance name comes from the URL and the server echoes it in
// a refusal, so one interpolated string in a write call is a way to clear an operator's screen,
// paint a fake prompt or plant a hyperlink. PTY bytes are the one thing that belongs there raw, and
// they arrive as a Uint8Array, not as a template literal.
//
// Parser-free and dependency-free, for the reason check-jsx-stray-attrs.mjs gives.
//
// Run: node scripts/checks/check-terminal-output.mjs [repo-root]

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// Module constants that hold only colour codes, and a session id held to its allowlist above the call.
const SAFE_NAMES = new Set(['DIM', 'GREEN', 'RESET', 'color', 'sessionId', 'heading', 'line']);

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.tsx?$/.test(full)) yield full;
  }
}

const offenders = [];
let calls = 0;
for (const file of walk(src)) {
  const code = readFileSync(file, 'utf8');
  if (!/@xterm\/xterm|bundleRef|xterm-setup/.test(code)) continue;
  for (const call of code.matchAll(/\.(?:write|writeln)\(\s*`([^`]*)`/g)) {
    calls += 1;
    for (const hole of call[1].matchAll(/\$\{([^}]*)\}/g)) {
      const expression = hole[1].trim();
      if (SAFE_NAMES.has(expression) || /^terminalSafe\(/.test(expression)) continue;
      const line = code.slice(0, call.index).split('\n').length;
      offenders.push(`${relative(root, file)}:${line} writes \${${expression}} to the terminal unsanitized`);
    }
  }
}

// The two names that carry outside text inside the write calls above.
const banner = readFileSync(join(src, 'lib', 'terminal-banner.ts'), 'utf8');
for (const name of ['heading', 'line']) {
  if (!new RegExp(`const ${name} = terminalSafe\\(`).test(banner)) {
    offenders.push(`terminal-banner.ts: ${name} is written to the terminal and no longer passes through terminalSafe`);
  }
}
const socket = readFileSync(join(src, 'lib', 'use-terminal-socket.ts'), 'utf8');
if (!/SESSION_ID\.test\(sessionId\)\) term\?\.writeln/.test(socket)) {
  offenders.push('use-terminal-socket.ts: the session id is written without its allowlist');
}

// A scan over nothing passes for the wrong reason.
if (calls < 3) offenders.push(`found only ${calls} interpolated terminal writes, so this asserts nothing`);

if (offenders.length) {
  console.error('Text from outside the PTY is reaching xterm unsanitized:');
  for (const offender of offenders) console.error(`  ${offender}`);
  process.exit(1);
}
console.log(`terminal output: ${calls} interpolated writes, all through terminalSafe or a constant`);
