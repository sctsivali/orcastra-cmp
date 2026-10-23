#!/usr/bin/env node
// Token refresh against an IdP that rotates refresh tokens like Authentik 2025.10 does by default:
// a grant commits the rotation (revokes the old token, issues a new one) before it answers, and a
// revoked token presented again answers invalid_grant and counts as a SUSPICIOUS_REQUEST. This
// runs the real auth.ts, lib/refresh-grants.ts and middleware.ts against that IdP. Unless a case
// says otherwise, no refresh token may ever be presented twice.
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

globalThis.crypto ??= webcrypto; // Node 18 has no global crypto; CI runs 24

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

// Time: timers run at 1/100 (a 10 s wait takes 100 ms) and Date.now can be moved forward.
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms = 0, ...args) => realSetTimeout(fn, ms / 100, ...args);
const realNow = Date.now.bind(Date);
let offsetMs = 0;
Date.now = () => realNow() + offsetMs;
const nowS = () => Math.floor(Date.now() / 1000);
const sleepReal = (ms) => new Promise((r) => realSetTimeout(r, ms));

process.env.AUTHENTIK_ISSUER = "https://idp.invalid/application/o/orcastra-dashboard/";
const TOKEN_ENDPOINT = "https://idp.invalid/application/o/token/";
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const accessToken = (n) => `${b64({ alg: "none" })}.${b64({ groups: ["role_partner"], n })}.sig`;

let idp;
function resetIdp(mode = "up") {
  idp = { mode, latencyMs: 0, valid: new Set(["rt-0"]), revoked: new Set(), n: 0, grants: 0, suspicious: 0, presented: [] };
}
globalThis.fetch = async (url, init = {}) => {
  if (String(url).endsWith("openid-configuration")) {
    return new Response(JSON.stringify({ token_endpoint: TOKEN_ENDPOINT }), { status: 200 });
  }
  idp.grants += 1;
  const presented = new URLSearchParams(String(init.body)).get("refresh_token");
  idp.presented.push(presented);
  if (idp.mode === "down") throw new TypeError("fetch failed");
  if (!idp.valid.has(presented)) {
    if (idp.revoked.has(presented)) idp.suspicious += 1;
    return new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 });
  }
  let next = presented;
  if (idp.mode !== "no-rotate") {
    idp.valid.delete(presented);
    idp.revoked.add(presented);
    next = `rt-${++idp.n}`;
    idp.valid.add(next);
  }
  // The rotation is committed; from here on, losing the answer loses the new token.
  if (idp.mode === "commit-then-fail") throw new TypeError("connection reset");
  if (idp.latencyMs) await new Promise((r) => setTimeout(r, idp.latencyMs));
  return new Response(JSON.stringify({
    access_token: accessToken(idp.n), expires_in: 300, ...(idp.mode === "no-rotate" ? {} : { refresh_token: next }),
  }), { status: 200 });
};

const quiet = { info: console.info, warn: console.warn };
const logs = [];
console.info = (m) => logs.push(String(m));
console.warn = (m) => logs.push(String(m));

const tokenClaims = load("frontend/src/lib/token-claims.ts");
function world() {
  const grants = load("frontend/src/lib/refresh-grants.ts");
  const nextAuthStub = { __esModule: true, default: (config) => ({ handlers: {}, config }) };
  const auth = load("frontend/src/auth.ts", {
    "next-auth": nextAuthStub,
    "@/lib/auth-redirect": { callbackPath() {}, redirectTo() {}, safeCallbackPath() {} },
    "@/lib/token-claims": tokenClaims,
    "@/lib/refresh-grants": grants,
  });
  return { jwt: auth.authConfig.callbacks.jwt, config: auth.authConfig };
}
const expired = (over = {}) => ({
  sub: "qa", authentikId: "qa@example.invalid", accessToken: "at-0", refreshToken: "rt-0",
  expiresAt: nowS() - 5, groups: ["role_partner"], roles: ["role_partner"], groupsReadAt: nowS() - 600, ...over,
});
async function grantsDuring(fn) {
  const before = idp.grants;
  const out = await fn();
  return { out, granted: idp.grants - before };
}

try {
  // A burst of session reads on one expired cookie spends the refresh token once.
  {
    resetIdp();
    const { jwt } = world();
    const c0 = expired();
    const { out, granted } = await grantsDuring(() => Promise.all(Array.from({ length: 20 }, () => jwt({ token: { ...c0 } }))));
    assert.equal(granted, 1, "twenty concurrent reads made one grant");
    assert.ok(out.every((t) => t.refreshToken === "rt-1" && !t.error), "every reader got the rotated token");
    assert.equal(idp.suspicious, 0);
  }

  // An older cookie after a rotation is healed from the registry, across two rotations.
  {
    resetIdp();
    const { jwt } = world();
    const c0 = expired();
    const c1 = await jwt({ token: { ...c0 } });
    assert.equal(c1.refreshToken, "rt-1");
    const healed = await grantsDuring(() => jwt({ token: { ...c0 } }));
    assert.equal(healed.granted, 0, "a spent refresh token is not presented again");
    assert.equal(healed.out.refreshToken, "rt-1");
    assert.equal(healed.out.accessToken, c1.accessToken);
    offsetMs += 25_000; // past the role recheck floor
    const c2 = await jwt({ token: { ...c1 }, trigger: "update", session: { refreshRoles: true } });
    assert.equal(c2.refreshToken, "rt-2", "a role recheck forces a grant with the current token");
    const twoHops = await grantsDuring(() => jwt({ token: { ...c0 } }));
    assert.equal(twoHops.granted, 0);
    assert.equal(twoHops.out.refreshToken, "rt-2", "the chain resolves to the newest rotation");
    assert.ok(logs.some((l) => /rotation replay served hops=2/.test(l)));

    // The newest token has expired too: one grant, with the newest refresh token.
    offsetMs += 400_000;
    const later = await grantsDuring(() => jwt({ token: { ...c0 } }));
    assert.equal(later.granted, 1);
    assert.equal(idp.presented.at(-1), "rt-2", "the grant used the newest refresh token, never a spent one");
    assert.equal(later.out.refreshToken, "rt-3");
    assert.equal(idp.suspicious, 0);
    offsetMs = 0;
  }

  // A grant slower than the caller's wait: the caller gets its token back unchanged and no error,
  // and the rotation the IdP committed is not lost.
  {
    resetIdp();
    idp.latencyMs = 15_000;
    const { jwt } = world();
    const c0 = expired();
    const first = await grantsDuring(() => jwt({ token: { ...c0 } }));
    assert.equal(first.out.accessToken, "at-0", "the caller stopped waiting");
    assert.equal(first.out.error, undefined, "a slow grant does not raise the re-login banner");
    assert.equal(first.out.retryAfter, undefined, "a slow grant does not start a cooldown");
    const joined = await grantsDuring(() => jwt({ token: { ...c0 } }));
    assert.equal(joined.granted, 0, "a read while the grant runs joins it");
    await sleepReal(200);
    const healed = await grantsDuring(() => jwt({ token: { ...c0 } }));
    assert.equal(healed.granted, 0);
    assert.equal(healed.out.refreshToken, "rt-1", "the late rotation reached the next read");
    assert.equal(idp.suspicious, 0);
  }

  // An IdP that does not rotate: no self-link, every expiry is a plain grant.
  {
    resetIdp("no-rotate");
    const { jwt } = world();
    for (let i = 0; i < 3; i++) {
      const t = await jwt({ token: expired() });
      assert.equal(t.refreshToken, "rt-0");
      assert.equal(t.error, undefined);
    }
    assert.equal(idp.grants, 3);
  }

  // The window closes: after it, a spent token reaches the IdP and is refused.
  {
    resetIdp();
    const { jwt } = world();
    const c0 = expired();
    await jwt({ token: { ...c0 } });
    offsetMs += 11 * 60_000;
    const stale = await jwt({ token: { ...c0, expiresAt: nowS() - 5 } });
    assert.equal(stale.error, "RefreshAccessTokenError");
    assert.equal(idp.suspicious, 1, "past the window the reuse is the IdP's to see");
    offsetMs = 0;
  }

  // Another user's token carrying a spent refresh token is not healed into this chain.
  {
    resetIdp();
    const { jwt } = world();
    await jwt({ token: expired() });
    const other = await jwt({ token: expired({ authentikId: "other@example.invalid", sub: "other" }) });
    assert.notEqual(other.refreshToken, "rt-1");
    assert.equal(other.error, "RefreshAccessTokenError");
  }

  // Sign-out: a cookie from before it is not healed, and a grant in flight settles as signed out.
  {
    resetIdp();
    const { jwt, config } = world();
    const c0 = expired();
    const c1 = await jwt({ token: { ...c0 } });
    await config.events.signOut({ token: c1 });
    const replay = await grantsDuring(() => jwt({ token: { ...c0 } }));
    assert.equal(replay.out, null, "a pre-sign-out cookie is signed out, not healed into the chain");
    assert.equal(replay.granted, 0, "and its spent token is not presented to the IdP");
    assert.equal(idp.suspicious, 0);

    resetIdp();
    idp.latencyMs = 5_000;
    const fresh = world();
    const pending = fresh.jwt({ token: expired() });
    await sleepReal(10);
    await fresh.config.events.signOut({ token: expired() });
    assert.equal(await pending, null, "a grant that lands after the sign-out does not write a session");
  }

  // A committed rotation whose answer is lost is the documented residual: the next attempt is
  // refused once, after the cooldown.
  {
    resetIdp("commit-then-fail");
    const { jwt } = world();
    const c0 = expired();
    const lost = await jwt({ token: { ...c0 } });
    assert.equal(lost.error, "RefreshAccessTokenError");
    assert.ok(lost.retryAfter > nowS());
    const cooling = await grantsDuring(() => jwt({ token: { ...lost } }));
    assert.equal(cooling.granted, 0, "the cooldown holds");
    idp.mode = "up";
    offsetMs += 61_000;
    const after = await jwt({ token: { ...lost } });
    assert.equal(after.error, "RefreshAccessTokenError");
    assert.equal(idp.suspicious, 1);
    offsetMs = 0;
  }

  // An IdP that cannot be reached: error and cooldown, then recovery once it answers.
  {
    resetIdp("down");
    const { jwt } = world();
    const down = await jwt({ token: expired() });
    assert.equal(down.error, "RefreshAccessTokenError");
    assert.equal((await grantsDuring(() => jwt({ token: { ...down } }))).granted, 0);
    idp.mode = "up";
    offsetMs += 61_000;
    const back = await jwt({ token: { ...down } });
    assert.equal(back.error, undefined);
    assert.equal(back.refreshToken, "rt-1");
    offsetMs = 0;
  }

  // The role recheck floor holds, measured against the newest grant.
  {
    resetIdp();
    const { jwt } = world();
    const c1 = await jwt({ token: expired() });
    const held = await grantsDuring(() => jwt({ token: { ...c1 }, trigger: "update", session: { refreshRoles: true } }));
    assert.equal(held.granted, 0);
  }

  // The registry is bounded: the oldest rotation is evicted, which costs a refused grant, never a
  // wrong token.
  {
    resetIdp();
    const { jwt } = world();
    const firstSpent = expired({ authentikId: "user-0", sub: "user-0" });
    idp.valid = new Set();
    for (let i = 0; i <= 4100; i++) idp.valid.add(`seed-${i}`);
    await jwt({ token: { ...firstSpent, refreshToken: "seed-0" } });
    for (let i = 1; i <= 4100; i++) await jwt({ token: expired({ authentikId: `user-${i}`, sub: `user-${i}`, refreshToken: `seed-${i}` }) });
    const evicted = await jwt({ token: { ...firstSpent, refreshToken: "seed-0" } });
    assert.equal(evicted.error, "RefreshAccessTokenError", "an evicted rotation falls back to the IdP");
    const kept = await grantsDuring(() => jwt({ token: expired({ authentikId: "user-4100", sub: "user-4100", refreshToken: "seed-4100" }) }));
    assert.equal(kept.granted, 0, "a recent rotation is still served");
  }

  // Middleware reads the session and never refreshes it.
  {
    resetIdp();
    const { config } = world();
    let middlewareConfig;
    load("frontend/src/middleware.ts", {
      "next-auth": { __esModule: true, default: (c) => { middlewareConfig = c; return { auth: (handler) => handler }; } },
      "@/auth": { authConfig: config },
      "@/lib/request-security": { inspectRequestTarget: () => null },
      "@/lib/session-cookie-echo": { dropUnchangedSessionCookie: async (_q, r) => r, sessionSecret: () => "x" },
      "next/server": { NextResponse: Response },
      "@/app/settings/settings-tabs": { isSettingsTab: () => true },
    });
    assert.ok(middlewareConfig, "middleware builds its own NextAuth instance");
    assert.notEqual(middlewareConfig.callbacks.jwt, config.callbacks.jwt);
    const c0 = expired({ retryAfter: undefined });
    for (const params of [{ token: c0 }, { token: c0, trigger: "update", session: { refreshRoles: true } }]) {
      const out = await middlewareConfig.callbacks.jwt(params);
      assert.equal(out, c0, "middleware returned the token it was given");
    }
    assert.equal(idp.grants, 0, "middleware made a grant");
  }
} finally {
  console.info = quiet.info;
  console.warn = quiet.warn;
  globalThis.setTimeout = realSetTimeout;
}

console.log("token rotation: one writer, no refresh token presented twice, late grants kept");
