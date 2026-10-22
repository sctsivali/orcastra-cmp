#!/usr/bin/env node
// A refresh that fails on the request path must not stop the background path from recovering.
// Middleware (edge, 3 s budget) and /api/auth/session (nodejs, 10 s budget) run the same jwt
// callback. One IdP answer at 3006 ms used to put a 60 s cooldown into the session that the
// nodejs path honoured too, so the 401 handler's session update returned the expired token and
// the operator got the re-login banner for a minute. This runs the real auth.ts in both runtimes
// against a scripted IdP.
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

const tokenClaims = load("frontend/src/lib/token-claims.ts");

// ON_REQUEST_PATH and the budgets are read at module load, as in each Next.js runtime.
function jwtFor(runtime) {
  const previous = process.env.NEXT_RUNTIME;
  if (runtime === "edge") process.env.NEXT_RUNTIME = "edge";
  else delete process.env.NEXT_RUNTIME;
  try {
    const { authConfig } = load("frontend/src/auth.ts", {
      "next-auth": { __esModule: true, default: () => ({ handlers: {}, auth() {}, signIn() {}, signOut() {} }) },
      "@/lib/auth-redirect": { callbackPath() {}, redirectTo() {}, safeCallbackPath() {} },
      "@/lib/token-claims": tokenClaims,
    });
    return authConfig.callbacks.jwt;
  } finally {
    if (previous === undefined) delete process.env.NEXT_RUNTIME;
    else process.env.NEXT_RUNTIME = previous;
  }
}

process.env.AUTHENTIK_ISSUER = "https://idp.invalid/application/o/orcastra/";
const TOKEN_ENDPOINT = "https://idp.invalid/application/o/token/";
let idp = "up";
let grants = 0;
globalThis.fetch = async (url) => {
  if (String(url).endsWith("openid-configuration")) {
    return new Response(JSON.stringify({ token_endpoint: TOKEN_ENDPOINT }), { status: 200 });
  }
  grants += 1;
  if (idp === "down") throw new DOMException("The operation was aborted due to timeout", "TimeoutError");
  return new Response(
    JSON.stringify({ access_token: `fresh-${grants}`, refresh_token: `rt-${grants}`, expires_in: 300 }),
    { status: 200 },
  );
};

const quiet = { info: console.info, warn: console.warn };
console.info = () => {};
console.warn = () => {};

const edge = jwtFor("edge");
const nodejs = jwtFor("nodejs");
const now = () => Math.floor(Date.now() / 1000);
const expired = () => ({
  sub: "qa", authentikId: "qa@example.invalid", accessToken: "old", refreshToken: "rt-0",
  expiresAt: now() - 5, groups: ["role_admin"], roles: ["role_admin"], groupsReadAt: now() - 600,
});

async function step(jwt, token, params = {}) {
  const before = grants;
  const out = await jwt({ token, ...params });
  return { out, granted: grants - before };
}

try {
  // 1. A slow IdP on the request path: the page renders with the token it has, nothing errors.
  idp = "down";
  const blip = await step(edge, expired());
  assert.equal(blip.granted, 1, "the request path tries the expired token once");
  assert.equal(blip.out.accessToken, "old");
  assert.equal(blip.out.error, undefined, "the request path never marks the session errored");

  // 2. The next navigation inside the cooldown does not pay the IdP budget again.
  const nextNav = await step(edge, blip.out);
  assert.equal(nextNav.granted, 0, "the request path waits out its own failure");

  // 3. The 401 handler's session update and the session poll still refresh on the nodejs path.
  idp = "up";
  for (const params of [{ trigger: "update" }, {}]) {
    const recovered = await step(nodejs, blip.out, params);
    assert.equal(recovered.granted, 1, `a request-path cooldown held back the nodejs path (${JSON.stringify(params)})`);
    assert.match(recovered.out.accessToken, /^fresh-/);
    assert.equal(recovered.out.error, undefined);
    assert.equal(recovered.out.retryAfter, undefined);
    assert.equal(recovered.out.requestRetryAfter, undefined, "a successful grant clears every cooldown");
  }

  // 4. A role recheck on the nodejs path is not blocked by a request-path cooldown either.
  const recheck = await step(nodejs, { ...blip.out, expiresAt: now() + 280 }, {
    trigger: "update", session: { refreshRoles: true },
  });
  assert.equal(recheck.granted, 1, "role recheck honours only the background cooldown");

  // 5. An IdP that is really down still reaches the operator: the nodejs path errors and cools.
  idp = "down";
  const outage = await step(nodejs, blip.out, { trigger: "update" });
  assert.equal(outage.granted, 1);
  assert.equal(outage.out.error, "RefreshAccessTokenError", "a background failure raises the banner");
  assert.ok(outage.out.retryAfter > now(), "a background failure starts the cooldown");

  // 6. Nothing hammers a down IdP: both paths honour the background cooldown.
  assert.equal((await step(nodejs, outage.out, { trigger: "update" })).granted, 0, "nodejs waits out its cooldown");
  assert.equal((await step(edge, outage.out)).granted, 0, "edge waits out the background cooldown");
  const recheckDuringOutage = await step(nodejs, { ...outage.out, groupsReadAt: now() - 600 }, {
    trigger: "update", session: { refreshRoles: true },
  });
  assert.equal(recheckDuringOutage.granted, 0, "role recheck waits out the background cooldown");

  // 7. Once the cooldown has passed, both paths try again, and a recovery clears the error.
  idp = "up";
  const lapsed = { ...outage.out, retryAfter: now() - 1 };
  const back = await step(nodejs, lapsed);
  assert.equal(back.granted, 1);
  assert.equal(back.out.error, undefined, "a successful grant clears the banner's error");
  assert.equal((await step(edge, { ...expired(), requestRetryAfter: now() - 1 })).granted, 1);

  // 8. A token with time left is never refreshed on the request path.
  assert.equal((await step(edge, { ...expired(), expiresAt: now() + 30 })).granted, 0);
} finally {
  console.info = quiet.info;
  console.warn = quiet.warn;
}

console.log("token refresh cooldown: ok");
