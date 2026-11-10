#!/usr/bin/env node
// The vendored spice-html5 patches that decide correctness, run for real, plus the structural
// pieces the console engine relies on.
//
// - wire.js reassembles a SPICE message from LXD's 32 KiB frames with one copy, and still
//   processes a message completed by the remainder in the same call.
// - utils.js encodes an extended key as exactly e0 xx, with no trailing byte; Print Screen is
//   e0 37.
// - The console's own input engine owns the canvas (display.js external_input), only the focused
//   console sends keys (no window-level forwarding), and no console reads the global
//   spice_surface_<id> canvas by id (two consoles share that id).
//
// Run: node scripts/checks/check-spice-client.mjs [repo-root]
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const spice = (name) => readFileSync(join(root, "frontend/public/spice-html5", name), "utf8");

// ── wire.js: fragments in, whole messages out ────────────────────────────────────────────────
{
  const context = vm.createContext({ console, navigator: { userAgent: "Chrome" } });
  vm.runInContext(`${spice("atKeynames.js")}\n${spice("utils.js")}\n${spice("wire.js")}`, context);
  const delivered = [];
  const sc = { ws: { addEventListener() {} } };
  const reader = new context.SpiceWireReader(sc, (mb) => delivered.push(new Uint8Array(mb)));
  const bytes = (from, length) => Uint8Array.from({ length }, (_, i) => (from + i) % 256).buffer;

  // A 100-byte message in four fragments, then the next 10-byte message hidden in the last one.
  reader.request(100);
  reader.inbound(bytes(0, 30));
  reader.inbound(bytes(30, 30));
  assert.equal(delivered.length, 0, "a partial message must wait");
  reader.request(100);
  reader.inbound(bytes(60, 30));
  // The callback asks for the next size as the real channels do.
  reader.callback = (mb) => {
    delivered.push(new Uint8Array(mb));
    reader.request(10);
  };
  reader.inbound(bytes(90, 20)); // 10 to finish the first, 10 that are the whole second message
  assert.equal(delivered.length, 2, "the second message, complete in the remainder, is processed in the same call");
  assert.deepEqual(Array.from(delivered[0]), Array.from(new Uint8Array(bytes(0, 100))), "fragments joined in order");
  assert.deepEqual(Array.from(delivered[1]), Array.from(new Uint8Array(bytes(100, 10))), "the remainder is the next message");

  const joined = new Uint8Array(context.combine_array_buffers(bytes(0, 3), bytes(3, 2)));
  assert.deepEqual(Array.from(joined), [0, 1, 2, 3, 4], "combine_array_buffers copies both buffers");
}

// ── utils.js: extended scancodes ─────────────────────────────────────────────────────────────
{
  const context = vm.createContext({ console, navigator: { userAgent: "Chrome" } });
  vm.runInContext(`${spice("atKeynames.js")}\n${spice("utils.js")}`, context);
  const le = (n) => [n & 0xff, (n >> 8) & 0xff, (n >> 16) & 0xff, (n >>> 24) & 0xff].filter((b, i, all) => b || all.slice(i).some(Boolean));
  assert.deepEqual(le(context.keycode_to_start_scan(36)), [0xe0, 0x47], "Home is e0 47");
  assert.deepEqual(le(context.keycode_to_end_scan(36)), [0xe0, 0xc7], "Home release is e0 c7");
  assert.deepEqual(le(context.keycode_to_start_scan(44)), [0xe0, 0x37], "Print Screen is e0 37, never a backtick");
  assert.deepEqual(le(context.keycode_to_start_scan(65)), [0x1e], "A is 1e");
}

// ── structure the engine depends on ──────────────────────────────────────────────────────────
const display = spice("display.js");
assert.match(display, /if \(this\.parent\.external_input\)/, "display.js must let the console engine own the canvas");
assert.ok(!/getElementById\("spice_surface_"/.test(display), "display.js must not find a surface canvas by its shared id");
assert.ok(!/tmpstr \+= +'%'/.test(display), "JPEG frames must not be percent-encoded into data URLs");
assert.match(spice("inputs.js"), /this\.onmotionack\(\)/, "inputs.js must hand motion ACKs to the pointer engine");
assert.match(spice("main.js"), /onerror : this\.child_error\.bind\(this\)/, "a failing display, inputs or cursor channel must report");
assert.match(spice("spiceconn.js"), /e\.code = code;/, "a channel error must carry its close code");
assert.match(spice("cursor.js"), /var flags = cursor_set\.cursor\.flags;/, "cursor flags are read from the cursor");

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(ts|tsx)$/.test(entry)) out.push(full);
  }
  return out;
}
const code = (source) => source.split("\n").filter((line) => !/^\s*(\/\/|\*)/.test(line)).join("\n");
// The files that drive a guest's keyboard. (A panel toggle on window, like the resource bar's
// Ctrl+I, sends nothing to a guest and is not what this guards.)
const drivers = walk(join(root, "frontend/src/lib/spice")).concat(
  ["graphics-console.tsx", "console-workspace.tsx", "console-window.tsx"].map((f) => join(root, "frontend/src/components/console", f)),
);
for (const file of drivers) {
  const body = code(readFileSync(file, "utf8"));
  assert.ok(
    !/window\.addEventListener\(\s*["']key(down|up)["']/.test(body),
    `${file}: a console must not listen for keys on window; only the focused canvas sends keys to its guest`,
  );
}

console.log("spice client: wire assembly, extended scancodes, engine hooks and focus-scoped keys hold");
