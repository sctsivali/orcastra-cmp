"use strict";

// Synthetic network and filesystem adapters only. No sockets or credentials.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const runtime = require("./real_browser_launch.cjs");

function configuration(now = Date.now()) {
  return { schema_version: 1, run_id: "2f15fbaa-220f-4c65-bae2-7f0ec6792061", proposal_digest: "a".repeat(64),
    proxies: [[32025, "frontend", 2025], [32405, "backend", 4050], [32090, "authentik-server", 32090]],
    deadline_seconds: 60, deadline_epoch_ms: now + 60_000 };
}
class Socket extends EventEmitter {
  constructor() { super(); this.destroyed = false; this.piped = []; }
  setTimeout(milliseconds, action) { assert.equal(milliseconds, 60_000); this.timeoutAction = action; }
  pipe(socket) { this.piped.push(socket); return socket; }
  destroy() { if (!this.destroyed) { this.destroyed = true; this.emit("close"); } }
}
class Server extends EventEmitter {
  constructor(handler) { super(); this.handler = handler; this.closed = false; }
  listen(endpoint, action) { this.endpoint = endpoint; action(); }
  close() { this.closed = true; }
}
function networkFixture(failAt = -1) {
  const servers = [], connections = [];
  const network = {
    createServer(handler) {
      const server = new Server(handler);
      if (servers.length === failAt) server.listen = () => server.emit("error", new Error("untrusted error includes PRIVATE"));
      servers.push(server); return server;
    },
    connect(endpoint) { const socket = new Socket(); connections.push({ endpoint, socket }); return socket; },
  };
  return { network, servers, connections };
}

test("the fixed source port, addresses and deadline are closed", () => {
  const now = 100_000;
  assert.equal(runtime.configuration(configuration(now), now).proxies[0][2], 2025);
  const changes = [value => value.proxies[0][2] = 3000, value => value.proxies[0][1] = "external.invalid", value => value.proxies.push([443, "foreign", 443]),
    value => value.deadline_seconds = 1801, value => value.deadline_epoch_ms = now, value => value.private_token = "PRIVATE", value => value.run_id = "unbound"];
  for (const change of changes) { const value = configuration(now); change(value); assert.throws(() => runtime.configuration(value, now)); }
});

test("default invocation opens no files, sockets or child processes", async () => {
  const originalOpen = fs.openSync;
  let output = "";
  fs.openSync = () => { throw new Error("file access forbidden"); };
  let pending;
  try { pending = runtime.main([], value => { output += value; }); } finally { fs.openSync = originalOpen; }
  await pending;
  assert.deepEqual(JSON.parse(output), { status: "inert", runtime_pending: true });
});

test("an unknown argument does not enter the filesystem or echo it", async () => {
  const originalOpen = fs.openSync;
  fs.openSync = () => { throw new Error("file access forbidden"); };
  let pending;
  try { pending = runtime.main(["--execute", "PRIVATE-ARGUMENT"]); } finally { fs.openSync = originalOpen; }
  await assert.rejects(pending, error => !error.message.includes("PRIVATE"));
});

test("every raw TCP listener is loopback and preserves bidirectional streams", async () => {
  const fixture = networkFixture();
  const proxies = await runtime.createProxies(configuration(), fixture.network);
  assert.deepEqual(fixture.servers.map(server => server.endpoint), [
    { host: "127.0.0.1", port: 32025 }, { host: "127.0.0.1", port: 32405 }, { host: "127.0.0.1", port: 32090 },
  ]);
  for (const server of fixture.servers) server.handler(new Socket());
  assert.deepEqual(fixture.connections.map(row => row.endpoint), [{ host: "frontend", port: 2025 }, { host: "backend", port: 4050 }, { host: "authentik-server", port: 32090 }]);
  for (const { socket } of fixture.connections) { assert.equal(socket.piped.length, 1); assert.equal(socket.piped[0].piped[0], socket); }
  proxies.close();
  assert.ok(fixture.servers.every(server => server.closed));
  assert.ok(fixture.connections.every(row => row.socket.destroyed));
});

test("connection capacity refuses an extra upstream instead of reconnecting", async () => {
  const fixture = networkFixture();
  const proxies = await runtime.createProxies(configuration(), fixture.network);
  for (let index = 0; index < runtime.MAX_CONNECTIONS; index++) fixture.servers[0].handler(new Socket());
  const refused = new Socket(); fixture.servers[0].handler(refused);
  assert.ok(refused.destroyed);
  assert.equal(fixture.connections.length, runtime.MAX_CONNECTIONS);
  const failed = fixture.connections[0].socket; failed.emit("error", new Error("PRIVATE"));
  assert.equal(fixture.connections.length, runtime.MAX_CONNECTIONS);
  assert.ok(failed.destroyed);
  proxies.close();
});

test("proxy startup failure closes only the newly created synthetic listeners", async () => {
  const fixture = networkFixture(1);
  await assert.rejects(runtime.createProxies(configuration(), fixture.network), error => !error.message.includes("PRIVATE"));
  assert.ok(fixture.servers.every(server => server.closed));
  assert.equal(fixture.connections.length, 0);
});

test("absence of kernel release expires before any network or credentials", async () => {
  let now = 0, reads = 0;
  const value = configuration(now);
  await assert.rejects(runtime.awaitRelease(value, { fs: { existsSync: () => { reads++; return false; } }, sleep: async milliseconds => { now += milliseconds; }, now: () => now }), /release deadline/);
  assert.equal(now, 30_000);
  assert.equal(reads, 300);
});

test("release requires matching private run and proposal identity", async () => {
  const value = configuration();
  await assert.rejects(runtime.awaitRelease(value, { fs: { existsSync: () => true }, readJson: path => {
    assert.equal(path, "/qa/release.json"); return { run_id: value.run_id, proposal_digest: "b".repeat(64) };
  }, sleep: async () => {}, now: Date.now }), /identity differs/);
});

test("private release metadata and read fingerprints reject widening", () => {
  const bytes = Buffer.from(JSON.stringify({ run_id: "synthetic" }));
  for (const bad of ["symlink", "owner", "mode", "size", "changed", "valid"]) {
    let calls = 0;
    const filesystem = { constants: fs.constants, openSync: () => 7, closeSync: () => {},
      readSync: (_fd, destination) => { bytes.copy(destination); return bytes.length; }, fstatSync: () => ({
        isFile: () => bad !== "symlink", nlink: 1, uid: bad === "owner" ? 1 : 1000, gid: 1000,
        mode: bad === "mode" ? 0o644 : 0o600, size: bad === "size" ? 99999 : bytes.length,
        dev: 1, ino: 1, mtimeMs: bad === "changed" ? calls++ : 1, ctimeMs: 1,
      }) };
    if (bad === "valid") assert.deepEqual(runtime.fixedJson("/synthetic", 16384, 0o600, 1000, 1000, filesystem), { run_id: "synthetic" });
    else assert.throws(() => runtime.fixedJson("/synthetic", 16384, 0o600, 1000, 1000, filesystem));
  }
});
