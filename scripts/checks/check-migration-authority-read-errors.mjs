#!/usr/bin/env node
/** Exercise Migration Center's allowlisted authority error copy without a browser runner. */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(
  process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."),
);
const requireFromFrontend = createRequire(join(root, "frontend/package.json"));
const ts = requireFromFrontend("typescript");

class TestAPIError extends Error {
  constructor(message, status, detail, retryAfter) {
    super(message);
    this.name = "APIError";
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
  }
}

function compileReadError() {
  const file = "frontend/src/lib/read-error.ts";
  const source = readFileSync(join(root, file), "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      esModuleInterop: true,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
    },
    fileName: file,
  }).outputText;
  const module = { exports: {} };
  const localRequire = (specifier) => {
    if (specifier === "@/types/api") return { APIError: TestAPIError };
    throw new Error(`Unexpected runtime import while checking read errors: ${specifier}`);
  };
  new Function("require", "module", "exports", output)(
    localRequire,
    module,
    module.exports,
  );
  return module.exports;
}

const { describeReadError, isVaultUnavailable, retryUnlessUnavailable } = compileReadError();
const serverDiagnostic = "sensitive-upstream-identity-diagnostic";

assert.deepEqual(
  describeReadError(
    new TestAPIError("unsafe server message", 403, {
      error: "identity_binding_required",
      message: serverDiagnostic,
    }),
  ),
  {
    kind: "forbidden",
    reason:
      "this sign-in could not be bound to a current identity; sign out, sign in again, then retry",
  },
);

assert.deepEqual(
  describeReadError(
    new TestAPIError("unsafe server message", 403, {
      error: "current_role_denied",
      message: serverDiagnostic,
    }),
  ),
  {
    kind: "forbidden",
    reason:
      "your current role does not allow Migration Center access; ask an administrator to assign the Admin or Partner role",
  },
);

assert.deepEqual(
  describeReadError(
    new TestAPIError("unsafe server message", 503, {
      error: "authority_unavailable",
      message: serverDiagnostic,
    }),
  ),
  {
    kind: "failed",
    reason:
      "current access could not be verified; retry shortly or ask an administrator to check the identity service",
  },
);

assert.deepEqual(
  describeReadError(
    new TestAPIError("unsafe server message", 403, {
      error: "unknown_authority_code",
      message: serverDiagnostic,
    }),
  ),
  { kind: "forbidden", reason: "you are not allowed to read this" },
);

assert.deepEqual(
  describeReadError(
    new TestAPIError("unsafe server message", 403, {
      error: "authority_unavailable",
      message: serverDiagnostic,
    }),
  ),
  { kind: "forbidden", reason: "you are not allowed to read this" },
);

for (const error of [
  { error: "identity_binding_required", status: 403 },
  { error: "current_role_denied", status: 403 },
  { error: "authority_unavailable", status: 503 },
  { error: "identity_provider_unavailable", status: 503 },
]) {
  const rendered = JSON.stringify(
    describeReadError(
      new TestAPIError("unsafe server message", error.status, {
        error: error.error,
        message: serverDiagnostic,
      }),
    ),
  );
  assert.equal(rendered.includes(serverDiagnostic), false);
  assert.equal(rendered.includes("unsafe server message"), false);
}

// An identity-provider outage is a 503 too, and it must not be read as "Vault is not configured".
assert.deepEqual(
  describeReadError(
    new TestAPIError("unsafe server message", 503, {
      error: "identity_provider_unavailable",
      message: serverDiagnostic,
    }),
  ),
  {
    kind: "failed",
    reason: "the sign-in service could not be reached to verify this session; retry shortly",
  },
);
assert.equal(
  isVaultUnavailable(new TestAPIError("x", 503, { error: "identity_provider_unavailable", message: "m" })),
  false,
);
assert.equal(isVaultUnavailable(new TestAPIError("x", 503, "Vault is not configured")), true);
assert.equal(
  isVaultUnavailable(new TestAPIError("x", 503, "Vault is not configured. Enable Vault for integrations.")),
  true,
);
assert.equal(
  isVaultUnavailable(new TestAPIError("x", 503, "Authentication is enabled but not configured on this server")),
  false,
);
assert.equal(isVaultUnavailable(new TestAPIError("x", 500, "Vault is not configured")), false);

// Every page that swaps itself for the Vault notice must decide that with the shared helper.
for (const file of [
  "frontend/src/components/integrations-settings.tsx",
  "frontend/src/components/extensions/extensions-overview.tsx",
]) {
  const source = readFileSync(join(root, file), "utf8");
  assert.ok(source.includes("isVaultUnavailable("), `${file} must use isVaultUnavailable`);
  assert.ok(!/status\)?\s*===\s*503/.test(source.replace(/retry:[^\n]*\n(?:[^\n]*\n){0,4}/g, "")),
    `${file} must not read a bare 503 as Vault`);
}

// react-query runs a shared query with whichever observer's options it holds, so every observer of
// the API key list must carry the same no-retry-on-503 rule. One without it retried an admin's 503.
assert.equal(retryUnlessUnavailable(0, new TestAPIError("x", 503, "Vault is not configured")), false);
assert.equal(retryUnlessUnavailable(0, new TestAPIError("x", 502, "upstream")), true);
assert.equal(retryUnlessUnavailable(2, new TestAPIError("x", 502, "upstream")), false);
for (const file of [
  "frontend/src/components/integrations-settings.tsx",
  "frontend/src/components/integrations/synced-organizations-panel.tsx",
  // the tab shell reads the same list for the count on its section switcher (2026-11-03)
  "frontend/src/components/integrations/integrations-sections.tsx",
]) {
  const source = readFileSync(join(root, file), "utf8");
  assert.ok(source.includes('queryKey: ["integrations", "api-keys"]'), `${file} no longer observes the key list`);
  assert.ok(source.includes("retry: retryUnlessUnavailable"), `${file} must pass retryUnlessUnavailable`);
}

// A 401 that reaches a page is a session the console could not renew, not a permission. It sits
// next to the re-login banner, so it must not read as "you are not allowed" (2026-10-23).
assert.deepEqual(describeReadError(new TestAPIError("Unauthorized", 401, "Not authenticated")), {
  kind: "unauthenticated",
  reason: "your session is no longer valid; sign in again to continue",
});
assert.deepEqual(describeReadError(new TestAPIError("Forbidden", 403, "Not allowed")), {
  kind: "forbidden",
  reason: "you are not allowed to read this",
});

console.log("read errors: authority, identity-provider and Vault answers are told apart");
