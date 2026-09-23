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
  return module.exports.describeReadError;
}

const describeReadError = compileReadError();
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
