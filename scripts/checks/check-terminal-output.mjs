#!/usr/bin/env node
// Structural guard: text that did not come from the PTY reaches xterm through terminalSafe.
//
// xterm executes what it is given. An instance name comes from the URL and the server echoes it in
// a refusal, so one interpolated string in a write call is a way to clear an operator's screen,
// paint a fake prompt or plant a hyperlink. PTY bytes are the one thing that belongs there raw, and
// they arrive as a Uint8Array. Every write is read, not only the template literals: a mutation test
// passed `term.write(reason)` straight through the first version of this guard.
//
// Parser-free and dependency-free, for the reason check-jsx-stray-attrs.mjs gives.
//
// Run: node scripts/checks/check-terminal-output.mjs [repo-root]

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { closing, endOfTemplate, firstArgument, isWholeCall, templateHoles } from './source-scan.mjs';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// A whole first argument that may reach xterm as it is: PTY bytes and fixed escape sequences.
const RAW_ARGUMENTS = new Set(['chunk', 'END_DEAD_PROGRAM', 'LEAVE_ALT_SCREEN', '"\\r\\n".repeat(BOX_ROWS)']);
// Module constants that hold only colour codes, and a session id held to its allowlist above the call.
const SAFE_NAMES = new Set(['DIM', 'GREEN', 'RESET', 'color', 'sessionId', 'heading', 'line']);
// What terminalSafe must strip: C0, ESC, DEL, C1 and CSI, then the format characters that reorder text.
const MUST_STRIP = '\x00\x07\x1b\x7f\x80\x9b\x9f\u061c\u200e\u200f\u2028\u2029\u202a\u202e\u2066\u2069';
const HTML_SINK = /\b(?:innerHTML|outerHTML|insertAdjacentHTML|dangerouslySetInnerHTML)\b/;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.tsx?$/.test(full)) yield full;
  }
}

/** Why this first argument cannot reach xterm as it is, or null when it is safe. */
function unsafeArgument(argument) {
  if (RAW_ARGUMENTS.has(argument) || isWholeCall(argument, 'terminalSafe')) return null;
  if (!argument.startsWith('`') || endOfTemplate(argument, 0) !== argument.length - 1) {
    return `writes ${argument} to the terminal, which is neither terminalSafe() nor a fixed sequence`;
  }
  for (const hole of templateHoles(argument)) {
    if (!SAFE_NAMES.has(hole) && !isWholeCall(hole, 'terminalSafe')) {
      return `writes \${${hole}} to the terminal unsanitized`;
    }
  }
  return null;
}

const offenders = [];
let calls = 0;
for (const file of walk(src)) {
  const code = readFileSync(file, 'utf8');
  if (!/@xterm\/xterm|bundleRef|xterm-setup/.test(code)) continue;
  for (const call of code.matchAll(/\.(?:write|writeln)\(/g)) {
    calls += 1;
    const open = call.index + call[0].length - 1;
    const problem = unsafeArgument(firstArgument(code.slice(open + 1, closing(code, open))));
    if (problem) {
      const line = code.slice(0, call.index).split('\n').length;
      offenders.push(`${relative(root, file)}:${line} ${problem}`);
    }
  }
}

// The two names that carry outside text inside the write calls above.
const bannerFile = join(src, 'lib', 'terminal-banner.ts');
const socketFile = join(src, 'lib', 'use-terminal-socket.ts');
const banner = readFileSync(bannerFile, 'utf8');
const socket = readFileSync(socketFile, 'utf8');
for (const name of ['heading', 'line']) {
  const draw = banner.slice(banner.indexOf('export function drawConnectBox'));
  const value = draw.match(new RegExp(`const ${name} = (.*);$`, 'm'))?.[1] ?? '';
  if (!isWholeCall(value, 'terminalSafe')) {
    offenders.push(`terminal-banner.ts: ${name} is written to the terminal and no longer is one terminalSafe() call`);
  }
}

// terminalSafe is only as good as its pattern, so the pattern is run rather than read.
const unsafe = banner.match(/const UNSAFE = \/(.+)\/g;/)?.[1];
if (!unsafe || !/return text\.replace\(UNSAFE, ""\);/.test(banner)) {
  offenders.push('terminal-banner.ts: terminalSafe no longer strips UNSAFE');
} else {
  for (const character of MUST_STRIP) {
    if (!new RegExp(unsafe).test(character)) {
      const code = character.codePointAt(0).toString(16).padStart(4, '0');
      offenders.push(`terminal-banner.ts: UNSAFE lets U+${code} through`);
    }
  }
}

if (!/SESSION_ID\.test\(sessionId\)\) term\?\.writeln/.test(socket)) {
  offenders.push('use-terminal-socket.ts: the session id is written without its allowlist');
}
if (!socket.includes('const SESSION_ID = /^[\\w.:-]{1,128}$/;')) {
  offenders.push('use-terminal-socket.ts: the session id allowlist changed. Widening it widens what reaches xterm');
}

// The name comes from the URL, so the page refuses a bad one before any socket opens.
const names = readFileSync(join(src, 'lib', 'lxd-names.ts'), 'utf8');
if (!names.includes('export const LXD_NAME_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$/;')) {
  offenders.push('lxd-names.ts: LXD_NAME_PATTERN changed, and two terminal routes rely on it');
}
for (const route of ['terminal', 'console']) {
  const page = readFileSync(join(src, 'app', route, '[id]', 'page.tsx'), 'utf8');
  const gate = /if \(!LXD_NAME_PATTERN\.test\(instanceName\)\) \{\s*return \(/.exec(page);
  if (!gate || gate.index > page.lastIndexOf('return (')) {
    offenders.push(`app/${route}/[id]/page.tsx: a link that is not an instance name is no longer refused`);
  }
}

// The box is built from text nodes, so nothing in a name can become markup.
for (const file of [bannerFile, socketFile, join(src, 'lib', 'xterm-setup.ts')]) {
  if (HTML_SINK.test(readFileSync(file, 'utf8'))) {
    offenders.push(`${relative(root, file)} writes markup, where a name must stay text`);
  }
}

// A scan over nothing passes for the wrong reason.
if (calls < 9) offenders.push(`found only ${calls} terminal writes, so this asserts nothing`);

if (offenders.length) {
  console.error('Text from outside the PTY is reaching xterm unsanitized:');
  for (const offender of offenders) console.error(`  ${offender}`);
  process.exit(1);
}
console.log(`terminal output: ${calls} writes, all PTY bytes, fixed sequences or terminalSafe`);
