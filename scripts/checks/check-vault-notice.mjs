#!/usr/bin/env node
// The Vault notice. A sealed Vault would make the dashboard read as a deployment with no clusters,
// so the backend answers one 503 {error: vault_unavailable}; this pins that the console names it,
// mounts the notice on every page below the access gate, and does not count a failed read as zero
// on the two overview pages.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const ts = createRequire(join(root, "frontend/package.json"))("typescript");

function load(relativePath, dependencies = {}) {
  const output = ts.transpileModule(readFileSync(join(root, relativePath), "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", output)((name) => {
    if (Object.hasOwn(dependencies, name)) return dependencies[name];
    throw new Error(`Unexpected module dependency: ${name}`);
  }, module, module.exports);
  return module.exports;
}

class APIError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}
const { describeReadError } = load("frontend/src/lib/read-error.ts", { "@/types/api": { APIError } });
const sealed = new APIError("Cluster data cannot be read", 503, { error: "vault_unavailable", message: "x" });
assert.match(describeReadError(sealed).reason, /Vault\) is sealed or unavailable/);
assert.equal(describeReadError(sealed).kind, "failed");
const wrongStatus = new APIError("x", 500, { error: "vault_unavailable", message: "x" });
assert.doesNotMatch(describeReadError(wrongStatus).reason, /Vault/, "the code is honoured only on its own status");

const layout = readFileSync(join(root, "frontend/src/components/main-layout.tsx"), "utf8");
const gate = layout.indexOf('access.state !== "allowed"');
const notice = layout.indexOf("<VaultNotice />");
assert.ok(notice > gate && gate > 0, "the Vault notice belongs inside the shell, after the access gate");
assert.ok(notice > layout.indexOf("<Header />"), "the notice sits below the header");

const hook = readFileSync(join(root, "frontend/src/hooks/use-vault-status.ts"), "utf8");
assert.match(hook, /&& sessionHasRole;/, "a caller with no role must not poll a route that refuses it");
assert.match(hook, /!query\.isError/, "a failed status read must not raise the notice");
assert.match(hook, /Boolean\(session\) && status !== "unauthenticated"/, "the notice must not blink on a session refresh");

assert.match(hook, /data\.state === "token_refused"/, "a refused token is the token's own answer and shows on the first read");

const noticeSource = readFileSync(join(root, "frontend/src/components/vault-notice.tsx"), "utf8");
assert.match(noticeSource, /^  token_refused: \{$/m, "a refused backend token has its own Administrator copy");
assert.match(noticeSource, /ADMIN_COPY\[state\] \?\? UNKNOWN_ADMIN_COPY/, "a state this build does not know renders a fallback, not a crash");
const api = readFileSync(join(root, "frontend/src/lib/vault-status-api.ts"), "utf8");
assert.match(api, /\| "token_refused"/, "the status type names token_refused");

const home = readFileSync(join(root, "frontend/src/app/page.tsx"), "utf8");
for (const value of ["animatedTotalNodes", "animatedTotalVMs", "animatedRunningVMs", "animatedTotalProjects"]) {
  assert.match(home, new RegExp(`Failed \\? EMPTY_VALUE : ${value}`), `${value} renders 0 for a failed read`);
}
const dashboard = readFileSync(join(root, "frontend/src/components/dashboard.tsx"), "utf8");
assert.match(dashboard, /if \(vmsFailed && !vmsData\)/, "the inventory overview renders zeros for a failed read");

console.log("vault notice: ok");
