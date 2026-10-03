#!/usr/bin/env node
"use strict";

/** Frozen container entrypoint. No files, sockets or child processes by default. */
const fs = require("node:fs");
const net = require("node:net");
const { spawn } = require("node:child_process");

const PROXIES = Object.freeze([[32025, "frontend", 2025], [32405, "backend", 4050], [32090, "authentik-server", 32090]]);
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const SHA = /^[a-f0-9]{64}$/;
const MAX_CONNECTIONS = 512;

function exact(value, keys) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join("\0") !== [...keys].sort().join("\0")) throw new Error("closed launch schema required");
  return value;
}
function configuration(value, now = Date.now()) {
  exact(value, ["schema_version", "run_id", "proposal_digest", "proxies", "deadline_seconds", "deadline_epoch_ms"]);
  if (value.schema_version !== 1 || !UUID.test(value.run_id) || !SHA.test(value.proposal_digest)
      || JSON.stringify(value.proxies) !== JSON.stringify(PROXIES)
      || !Number.isSafeInteger(value.deadline_seconds) || value.deadline_seconds < 1 || value.deadline_seconds > 1800
      || !Number.isSafeInteger(value.deadline_epoch_ms) || value.deadline_epoch_ms <= now || value.deadline_epoch_ms - now > 1800_000) throw new Error("exact bounded launch contract required");
  return value;
}
function privateDirectory(path) {
  const value = fs.lstatSync(path);
  if (!value.isDirectory() || value.isSymbolicLink() || value.uid !== process.getuid() || value.gid !== process.getgid() || (value.mode & 0o777) !== 0o700) throw new Error("private evidence directory required");
}
function fixedJson(path, limit, mode, uid, gid, filesystem = fs) {
  const fd = filesystem.openSync(path, filesystem.constants.O_RDONLY | filesystem.constants.O_NOFOLLOW | filesystem.constants.O_NONBLOCK);
  try {
    const before = filesystem.fstatSync(fd);
    if (!before.isFile() || before.nlink !== 1 || before.uid !== uid || before.gid !== gid || (before.mode & 0o777) !== mode || before.size > limit) throw new Error("bounded fixed input required");
    const bytes = Buffer.alloc(limit + 1);
    const count = filesystem.readSync(fd, bytes, 0, bytes.length, null);
    const after = filesystem.fstatSync(fd);
    for (const field of ["dev", "ino", "size", "mtimeMs", "ctimeMs", "mode", "uid", "gid", "nlink"]) if (before[field] !== after[field]) throw new Error("fixed input changed");
    if (count !== before.size || count > limit) throw new Error("bounded complete input required");
    return JSON.parse(bytes.subarray(0, count).toString("utf8"));
  } finally { filesystem.closeSync(fd); }
}
function writePrivate(path, value) {
  const fd = fs.openSync(path, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
  try { fs.fchmodSync(fd, 0o600); fs.writeFileSync(fd, JSON.stringify(value)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}
async function awaitRelease(value, dependencies = { fs, readJson: fixedJson, sleep: (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)), now: Date.now }) {
  const limit = Math.min(dependencies.now() + 30_000, value.deadline_epoch_ms);
  while (dependencies.now() < limit) {
    if (dependencies.fs.existsSync("/qa/release.json")) {
      const release = dependencies.readJson("/qa/release.json", 16384, 0o600, process.getuid(), process.getgid());
      exact(release, ["run_id", "proposal_digest"]);
      if (release.run_id !== value.run_id || release.proposal_digest !== value.proposal_digest) throw new Error("kernel release identity differs");
      return;
    }
    await dependencies.sleep(100);
  }
  throw new Error("kernel release deadline elapsed");
}
async function createProxies(value, network = net) {
  // One browser uses this fixed raw TCP map. No HTTP rewriting, public bind,
  // upstream selection from requests, reconnect or external DNS target exists.
  configuration(value);
  const servers = [], sockets = new Set();
  const close = () => {
    for (const socket of sockets) socket.destroy();
    for (const server of servers) server.close();
  };
  try {
    for (const [listen, host, port] of value.proxies) {
      const server = network.createServer((incoming) => {
        if (sockets.size >= MAX_CONNECTIONS * 2) { incoming.destroy(); return; }
        sockets.add(incoming);
        let outgoing, finished = false;
        const finish = () => { if (finished) return; finished = true; incoming.destroy(); if (outgoing) outgoing.destroy(); sockets.delete(incoming); if (outgoing) sockets.delete(outgoing); };
        incoming.setTimeout(60_000, finish); incoming.on("error", finish); incoming.on("close", finish);
        outgoing = network.connect({ host, port }); sockets.add(outgoing);
        outgoing.setTimeout(60_000, finish); outgoing.on("error", finish); outgoing.on("close", finish);
        incoming.pipe(outgoing); outgoing.pipe(incoming);
      });
      servers.push(server);
      await new Promise((resolve, reject) => { server.once("error", reject); server.listen({ host: "127.0.0.1", port: listen }, resolve); });
      server.on("error", close);
    }
    return { close };
  } catch { close(); throw new Error("fixed private proxy startup failed"); }
}
async function execute() {
  if (process.getuid() !== 1000 || process.getgid() !== 1000) throw new Error("the frozen non-root user is required");
  privateDirectory("/qa");
  const value = configuration(fixedJson("/input/launch.json", 16384, 0o444, 0, 0));
  await awaitRelease(value);
  fs.mkdirSync("/tmp/orcastra-qa-home", { mode: 0o700 });
  const proxies = await createProxies(value);
  const stdout = fs.openSync("/qa/harness-stdout.private", fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
  const stderr = fs.openSync("/qa/harness-stderr.private", fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
  let child, timer, killTimer;
  let count = 0, terminating = false;
  try {
    const args = ["/input/real_role_harness.cjs", "--execute", "--credentials", "/run/orcastra-qa/actors.json", "--artifacts", "/qa/harness",
      "--frontend", "http://127.0.0.1:32025", "--backend", "http://127.0.0.1:32405", "--auth", "http://auth.orcastra-qa.invalid:32090"];
    child = spawn(process.execPath, args, { cwd: "/qa", stdio: ["ignore", "pipe", "pipe"], env: {
      PATH: "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", NODE_PATH: "/node/node_modules",
      PLAYWRIGHT_BROWSERS_PATH: "/ms-playwright", NODE_OPTIONS: "--max-old-space-size=512", HOME: "/tmp/orcastra-qa-home", LANG: "C.UTF-8", CI: "1",
    } });
    const stop = () => { if (terminating) return; terminating = true; child.kill("SIGTERM"); killTimer = setTimeout(() => child.kill("SIGKILL"), 1000); };
    const capture = (descriptor) => (chunk) => { count += chunk.length; if (count > 1024 * 1024) { stop(); return; } fs.writeSync(descriptor, chunk); };
    child.stdout.on("data", capture(stdout)); child.stderr.on("data", capture(stderr));
    let expired = false;
    const milliseconds = Math.min(value.deadline_seconds * 1000, value.deadline_epoch_ms - Date.now());
    if (milliseconds <= 0) { stop(); throw new Error("overall browser deadline elapsed"); }
    timer = setTimeout(() => { expired = true; stop(); }, milliseconds);
    const code = await new Promise((resolve, reject) => { child.once("error", reject); child.once("exit", (exit, signal) => resolve(signal === null && Number.isInteger(exit) ? exit : 1)); });
    fs.fsyncSync(stdout); fs.fsyncSync(stderr);
    writePrivate("/qa/launch-result.json", { schema_version: 1, run_id: value.run_id, exit_code: expired || count > 1024 * 1024 ? 1 : code, status: expired || code !== 0 || count > 1024 * 1024 ? "stopped" : "completed" });
    if (expired || code !== 0 || count > 1024 * 1024) throw new Error("frozen browser harness failed");
  } finally {
    clearTimeout(timer); clearTimeout(killTimer); proxies.close(); fs.closeSync(stdout); fs.closeSync(stderr);
  }
}
async function main(args = process.argv.slice(2), output = value => process.stdout.write(value)) {
  if (args.length === 0) { output('{"status":"inert","runtime_pending":true}\n'); return; }
  if (args.length !== 1 || args[0] !== "--execute") throw new Error("exact execute flag required");
  await execute();
}
if (require.main === module) main().catch(() => { process.stderr.write("browser_runtime_stopped\n"); process.exitCode = 1; });
module.exports = { configuration, createProxies, awaitRelease, fixedJson, main, PROXIES, MAX_CONNECTIONS };
