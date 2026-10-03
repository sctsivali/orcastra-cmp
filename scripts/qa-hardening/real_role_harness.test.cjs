"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const harness = require("./real_role_harness.cjs");
function credentials() { return Buffer.from(JSON.stringify({ schema_version: 1, actors: Object.fromEntries(Object.keys(harness.ACTORS).map((name) => [name, { password: `Synthetic-${name}-password-123` }])) })); }
function jwt(claims) { return `header.${Buffer.from(JSON.stringify(claims)).toString("base64url")}.signature`; }
test("credential adapter accepts only the six password fields", () => {
  assert.deepEqual(Object.keys(harness.parseCredentialsBytes(credentials())).sort(), Object.keys(harness.ACTORS).sort());
  const invalid = JSON.parse(credentials()); invalid.actors.admin.hash = "forbidden";
  assert.throws(() => harness.parseCredentialsBytes(Buffer.from(JSON.stringify(invalid))), /unexpected shape/);
});
test("credential file rejects links and loose modes before a browser exists", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "role-harness-"));
  try { const file = path.join(root, "credentials.json"); fs.writeFileSync(file, credentials(), { mode: 0o600 }); fs.chmodSync(file, 0o644); assert.throws(() => harness.privateRegular(file), /mode 0600/); fs.chmodSync(file, 0o600); fs.symlinkSync(file, path.join(root, "linked.json")); assert.throws(() => harness.privateRegular(path.join(root, "linked.json")), /regular/); fs.chmodSync(root, 0o755); assert.throws(() => harness.privateDirectory(root), /mode 0700/); } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
test("credential read rejects a symlinked ancestor and keeps role claims exclusive", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "role-harness-"));
  try {
    const secure = path.join(root, "secure"); fs.mkdirSync(secure, { mode: 0o700 }); fs.chmodSync(secure, 0o700);
    const file = path.join(secure, "credentials.json"); fs.writeFileSync(file, credentials(), { mode: 0o600 }); fs.chmodSync(file, 0o600);
    assert.deepEqual(Object.keys(harness.readCredentials(file)).sort(), Object.keys(harness.ACTORS).sort());
    fs.symlinkSync(secure, path.join(root, "linked-parent"));
    assert.throws(() => harness.readCredentials(path.join(root, "linked-parent", "credentials.json")), /unsafe ancestor/);
    const claims = { iss: harness.ISSUER, aud: harness.AUDIENCE, email: harness.ACTORS["tenant-a1"].email, groups: ["role_tenant", "role_admin"], exp: Math.floor(Date.now() / 1000) + 60 };
    assert.throws(() => harness.assertClaims(jwt(claims), "tenant-a1"), /unexpected private JWT claims/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
test("sanitized artifacts refuse bodies, tokens, and undeclared paths", () => {
  const row = { case_id: "auth.admin", method: "GET", path: "/api/v1/system/settings", status: 200, assertion: true };
  assert.deepEqual(harness.sanitizeRecord(row), row);
  assert.throws(() => harness.sanitizeRecord({ ...row, token: "secret" }), /unexpected shape/);
  assert.throws(() => harness.sanitizeRecord({ ...row, path: "/api/v1/organizations/123" }), /unsafe/);
});
test("execution is denied until exact loopback endpoints and explicit authority exist", () => {
  assert.throws(() => harness.requireExecution({ execute: false }), /refusing execution/);
  assert.throws(() => harness.requireExecution({ execute: true, credentials: "/private", artifacts: "/new", frontend: harness.FRONTEND, backend: "http://127.0.0.1:9", auth: harness.AUTH_ORIGIN }), /refusing execution/);
});
test("public failure output never contains an error message", () => {
  assert.equal(harness.failureNotice("auth.admin"), "real role harness stopped: execution_failed case=auth.admin\n");
  assert.equal(harness.failureNotice("password=leak"), "real role harness stopped: execution_failed case=startup\n");
});
