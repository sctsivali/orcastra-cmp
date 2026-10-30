#!/usr/bin/env node
// The console's guest actions (2026-10-27). Ctrl+Alt+Del never reached a guest: the console window
// called the vendored global sendCtrlAltDel, which assigns an undeclared `sc` and throws, and the
// full-page console called a method SpiceMainConn does not have. Both now send scancodes through
// lib/spice/input.ts. This pins that, keeps the menu's combination ids in step with the ids the
// backend accepts for the audit record, and checks a combination is always released.
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const read = (path) => readFileSync(join(root, path), "utf8");
const code = (source) => source.split("\n").filter((line) => !/^\s*(\/\/|\*)/.test(line)).join("\n");

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(ts|tsx)$/.test(entry)) out.push(full);
  }
  return out;
}

for (const file of walk(join(root, "frontend/src"))) {
  const body = code(readFileSync(file, "utf8"));
  assert.ok(!/window as any\)\.sendCtrlAltDel|spiceRef\.current\??\.sendCtrlAltDel/.test(body),
    `${file}: calls the broken vendored or nonexistent sendCtrlAltDel; use sendCombo(conn, COMBO_BY_ID["ctrl-alt-del"])`);
}

// Both the console window and the full page render ConsoleWorkspace, which mounts the actions.
assert.match(read("frontend/src/components/console/console-workspace.tsx"), /<ConsoleActions\b/, "the shared console actions are not mounted");
for (const surface of ["frontend/src/components/console/console-window.tsx", "frontend/src/app/console/[id]/page.tsx"]) {
  assert.match(read(surface), /<(?:Memo)?(?:ConsoleWorkspace|Workspace)\b/, `${surface}: does not render the shared console workspace`);
}

const keys = read("frontend/src/lib/spice/keys.ts");
const named = new Set([...keys.matchAll(/\bid: "([a-z0-9-]+)"/g)].map((m) => m[1]));
for (let n = 1; n <= 12; n += 1) named.add(`ctrl-alt-f${n}`);
const backend = read("backend/app/routers/console_actions.py");
const listed = backend.match(/COMBO_IDS = frozenset\(\{([\s\S]*?)\}\)/)?.[1] ?? "";
const accepted = new Set([...listed.matchAll(/"([a-z0-9-]+)"/g)].map((m) => m[1]));
if (/ctrl-alt-f\{n\}/.test(listed)) for (let n = 1; n <= 12; n += 1) accepted.add(`ctrl-alt-f${n}`);
assert.deepEqual([...named].sort(), [...accepted].sort(), "the toolbar's combinations and the audit route's accepted ids differ");

const input = code(read("frontend/src/lib/spice/input.ts"));
assert.match(input, /\} finally \{[\s\S]*pressed\.reverse\(\)/, "a combination must be released in reverse even when a send fails");

const actions = read("frontend/src/components/console/console-actions.tsx");
assert.match(actions, /action: "keys\.send", combo: combo\.id/, "a sent combination is recorded by its id");
assert.match(actions, /action: "screenshot\.take"/, "a screenshot is recorded");
assert.match(actions, /combo\.confirmUnlessWindows && !isWindowsGuest\(guestOs\)/, "Ctrl+Alt+Del asks first unless the guest is Windows");
assert.match(actions, /disabled=\{!canUseConsole\}/, "the guest actions follow the console permission");

for (const notice of ["COPYING", "COPYING.LESSER", "LICENSE.txt"]) {
  assert.ok(statSync(join(root, "frontend/public/spice-html5", notice)).isFile(), `spice-html5 ships without ${notice} (LGPL)`);
}

console.log(`console actions: ${named.size} combinations in step with the audit route, no broken Ctrl+Alt+Del path, LGPL notices shipped`);
