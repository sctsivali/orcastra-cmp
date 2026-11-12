#!/usr/bin/env node
// Token refresh against an IdP that rotates refresh tokens, as Authentik 2025.10 does by default:
// a grant commits the rotation (revokes the old token, issues a new one) before it answers, and a
// revoked token presented again answers invalid_grant and counts as a SUSPICIOUS_REQUEST. This
// runs the real auth.ts, lib/auth-refresh.ts, lib/refresh-grants.ts and middleware.ts against that
// IdP. Unless a case says otherwise, no refresh token is presented twice. An invalid_grant ends
// the session (RefreshTokenRevoked); only a failure that says nothing about the token cools down
// and retries (check-auth-refresh-contract.mjs holds that split).
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

// Time: timers and abort deadlines run at 1/100 (the 20 s grant cap takes 200 ms), and Date.now
// can be moved forward.
const realSetTimeout = globalThis.setTimeout;
const realAbortTimeout = AbortSignal.timeout.bind(AbortSignal);
globalThis.setTimeout = (fn, ms = 0, ...args) => realSetTimeout(fn, ms / 100, ...args);
AbortSignal.timeout = (ms) => realAbortTimeout(Math.max(1, ms / 100));
const realNow = Date.now.bind(Date);
let offsetMs = 0;
Date.now = () => realNow() + offsetMs;
const nowS = () => Math.floor(Date.now() / 1000);
const sleepReal = (ms) => new Promise((r) => realSetTimeout(r, ms));

process.env.AUTHENTIK_ISSUER = "https://idp.invalid/application/o/orcastra-dashboard/";
const TOKEN_ENDPOINT = "https://idp.invalid/application/o/token/";
const REVOKE_ENDPOINT = "https://idp.invalid/application/o/revoke/";
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const accessToken = (n, groups) => `${b64({ alg: "none" })}.${b64({ ...(groups ? { groups } : {}), n })}.sig`;

let idp;
function resetIdp(mode = "up") {
  idp = {
    mode, latencyMs: 0, omitGroups: false, valid: new Set(["rt-0"]), revoked: new Set(), n: 0,
    grants: 0, suspicious: 0, presented: [], revokedBySignOut: [], revokeCalls: 0, revokeStatus: 200,
    advertiseRevocation: true,
  };
}

// Lets a test hold one SHA-256 computation open, to land a sign-out exactly inside it.
const realDigest = globalThis.crypto.subtle.digest.bind(globalThis.crypto.subtle);
let digestGate = null;
globalThis.crypto.subtle.digest = async (algorithm, data) => {
  if (digestGate && new TextDecoder().decode(data) === digestGate.value) {
    digestGate.reached();
    await digestGate.release;
  }
  return realDigest(algorithm, data);
};
globalThis.fetch = async (url, init = {}) => {
  if (String(url).endsWith("openid-configuration")) {
    return new Response(JSON.stringify({
      token_endpoint: TOKEN_ENDPOINT, ...(idp.advertiseRevocation ? { revocation_endpoint: REVOKE_ENDPOINT } : {}),
    }), { status: 200 });
  }
  const form = new URLSearchParams(String(init.body));
  if (String(url) === REVOKE_ENDPOINT) {
    idp.revokeCalls += 1;
    if (idp.revokeStatus !== 200) return new Response(null, { status: idp.revokeStatus });
    idp.revokedBySignOut.push(form.get("token"));
    idp.valid.delete(form.get("token"));
    return new Response(null, { status: 200 });
  }
  idp.grants += 1;
  const presented = form.get("refresh_token");
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
  if (idp.latencyMs) {
    await new Promise((resolveWait, reject) => {
      const timer = setTimeout(resolveWait, idp.latencyMs);
      init.signal?.addEventListener("abort", () => {
        clearTimeout(timer);
        reject(new DOMException("The operation was aborted due to timeout", "TimeoutError"));
      });
    });
  }
  return new Response(JSON.stringify({
    access_token: accessToken(idp.n, idp.omitGroups ? null : ["role_partner"]), expires_in: 300,
    ...(idp.mode === "no-rotate" ? {} : { refresh_token: next }),
  }), { status: 200 });
};

const quiet = { info: console.info, warn: console.warn };
const logs = [];
console.info = (m) => logs.push(String(m));
console.warn = (m) => logs.push(String(m));

const tokenClaims = load("frontend/src/lib/token-claims.ts");
function world() {
  const registry = load("frontend/src/lib/refresh-grants.ts");
  const auth = load("frontend/src/auth.ts", {
    "next-auth": { __esModule: true, default: (config) => ({ handlers: {}, config }) },
    "@/lib/auth-redirect": { callbackPath() {}, redirectTo() {}, safeCallbackPath() {} },
    "@/lib/token-claims": tokenClaims,
    "@/lib/refresh-grants": registry,
    "@/lib/auth-refresh": load("frontend/src/lib/auth-refresh.ts"),
    "@/types/rbac": load("frontend/src/types/rbac.ts"),
    "@/lib/token-stamp": load("frontend/src/lib/token-stamp.ts"),
    "@/lib/session-cookie": { pinnedSessionCookies: () => undefined },
  });
  return { jwt: auth.authConfig.callbacks.jwt, config: auth.authConfig, registry };
}
const SUB = "qa@example.invalid";
const expired = (over = {}) => ({
  sub: "qa", authentikId: SUB, accessToken: "at-0", refreshToken: "rt-0",
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

  // A read that was in flight with the older cookie when the rotation landed is healed, and the
  // replay is logged without token material.
  {
    resetIdp();
    const { jwt } = world();
    const c0 = expired();
    const c1 = await jwt({ token: { ...c0 } });
    assert.equal(c1.refreshToken, "rt-1");
    const healed = await grantsDuring(() => jwt({ token: { ...c0 } }));
    assert.equal(healed.granted, 0, "a spent refresh token is not presented again");
    assert.equal(healed.out.accessToken, c1.accessToken);
    assert.ok(logs.some((l) => /rotation replay served hops=1 age_ms=\d+ subject=[\w-]{8}$/.test(l)));
    assert.ok(!logs.some((l) => /rt-\d|at-\d|example\.invalid/.test(l)), "no token material or subject in the log");
    assert.equal(idp.suspicious, 0);
  }

  // The chain is followed for two hops and no further.
  {
    resetIdp();
    const { registry } = world();
    const fields = (rt) => ({ accessToken: `at-${rt}`, refreshToken: rt, expiresAt: nowS() + 300, groupsReadAt: nowS() });
    for (const [from, to] of [["rt-a", "rt-b"], ["rt-b", "rt-c"], ["rt-c", "rt-d"]]) {
      await (await registry.grantOnce(SUB, from, async () => ({ ok: true, fields: fields(to) }))).outcome;
    }
    assert.equal((await registry.upgradeSpent(SUB, "rt-a")).fields.refreshToken, "rt-c", "more than two hops were followed");
  }

  // The settled-entry race: a grant settled between a reader's lookup and its own grant is taken,
  // never repeated with the spent token and never overwritten.
  {
    resetIdp();
    const { jwt, registry } = world();
    await jwt({ token: expired() });
    let called = 0;
    const { outcome } = await registry.grantOnce(SUB, "rt-0", async () => { called += 1; return { ok: false }; });
    const settled = await outcome;
    assert.equal(called, 0, "a settled grant was repeated");
    assert.ok(logs.at(-1).includes("rotation replay served"), "a replay through the grant path is logged too");
    assert.equal(settled.ok && settled.fields.refreshToken, "rt-1");
    assert.equal((await grantsDuring(() => jwt({ token: expired() }))).granted, 0, "the link survived");
  }

  // A slow IdP inside the cap: the read waits for the grant, so the new token is never dropped,
  // and a reader arriving meanwhile joins it.
  {
    resetIdp();
    idp.latencyMs = 15_000;
    const { jwt } = world();
    const c0 = expired();
    const first = jwt({ token: { ...c0 } });
    await sleepReal(50);
    const second = jwt({ token: { ...c0 } });
    const [a, b] = await Promise.all([first, second]);
    assert.equal(idp.grants, 1, "the second reader joined");
    for (const t of [a, b]) {
      assert.equal(t.refreshToken, "rt-1");
      assert.equal(t.error, undefined);
    }
    assert.equal(idp.suspicious, 0);
  }

  // Past the cap the answer is lost. The IdP has committed the rotation, so this session ends (a
  // known limitation). The timeout says nothing about the token and cools down; the retry after it
  // is refused with invalid_grant, which ends the session instead of cooling down again.
  {
    resetIdp();
    idp.latencyMs = 25_000;
    const { jwt } = world();
    const lost = await jwt({ token: expired() });
    assert.equal(lost.error, "RefreshAccessTokenError");
    assert.ok(lost.retryAfter > nowS());
    idp.latencyMs = 0;
    assert.equal((await grantsDuring(() => jwt({ token: { ...lost } }))).granted, 0, "the cooldown holds");
    offsetMs += 61_000;
    const after = await jwt({ token: { ...lost } });
    assert.equal(after.error, "RefreshTokenRevoked");
    assert.equal(after.refreshToken, undefined, "a refused refresh token is dropped");
    assert.equal(idp.suspicious, 1);
    assert.equal((await grantsDuring(() => jwt({ token: { ...after } }))).granted, 0, "an ended session asks nothing more");
    offsetMs = 0;
  }

  // The same residual when the connection drops after the commit.
  {
    resetIdp("commit-then-fail");
    const { jwt } = world();
    const lost = await jwt({ token: expired() });
    assert.equal(lost.error, "RefreshAccessTokenError");
    idp.mode = "up";
    offsetMs += 61_000;
    assert.equal((await jwt({ token: { ...lost } })).error, "RefreshTokenRevoked");
    assert.equal(idp.suspicious, 1);
    offsetMs = 0;
  }

  // The window is short: a copy presented after it reaches the IdP and is refused, which is what
  // lets rotation see a second copy of a session.
  {
    resetIdp();
    const { jwt } = world();
    const c0 = expired();
    await jwt({ token: { ...c0 } });
    offsetMs += 16_000;
    const stale = await jwt({ token: { ...c0, expiresAt: nowS() - 5 } });
    assert.equal(stale.error, "RefreshTokenRevoked", "a copy the IdP refused is over");
    assert.equal(idp.suspicious, 1, "past the window the reuse is the IdP's to see");
    offsetMs = 0;
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

  // Another user's token carrying a spent refresh token is not healed into this chain.
  {
    resetIdp();
    const { jwt } = world();
    await jwt({ token: expired() });
    const other = await jwt({ token: expired({ authentikId: "other@example.invalid", sub: "other" }) });
    assert.notEqual(other.refreshToken, "rt-1");
    assert.equal(other.error, "RefreshAccessTokenError");
  }

  // Sign-out revokes the newest refresh token at the IdP, signs out an older cookie without
  // presenting it, and turns a grant in flight into a sign-out.
  {
    resetIdp();
    const { jwt, config } = world();
    const c0 = expired();
    const c1 = await jwt({ token: { ...c0 } });
    await config.events.signOut({ token: c1 });
    assert.deepEqual(idp.revokedBySignOut, ["rt-1"], "the session's refresh token is revoked at the IdP");
    const replay = await grantsDuring(() => jwt({ token: { ...c0 } }));
    assert.equal(replay.out, null, "a pre-sign-out cookie is signed out, not healed into the chain");
    assert.equal(replay.granted, 0, "and its spent token is not presented to the IdP");

    resetIdp();
    const lagging = world();
    await lagging.jwt({ token: expired() });
    await lagging.config.events.signOut({ token: expired() });
    assert.deepEqual(idp.revokedBySignOut, ["rt-1"], "a lagging cookie's sign-out revokes the newest token");

    offsetMs += 3 * 60_000;
    assert.equal(await jwt({ token: { ...c0 } }), null, "the sign-out still holds after three minutes");
    offsetMs = 0;

    resetIdp();
    idp.latencyMs = 5_000;
    const fresh = world();
    const pending = fresh.jwt({ token: expired() });
    await sleepReal(10);
    await fresh.config.events.signOut({ token: expired() });
    assert.equal(await pending, null, "a grant that lands after the sign-out does not write a session");
    assert.equal(idp.suspicious, 0);

    // A sign-out that lands while the grant is hashing its new token: the new token is not
    // linked and is revoked at the IdP, since no session will hold it.
    resetIdp();
    const racing = world();
    let reached;
    digestGate = { value: "rt-1", release: null, reached: null };
    digestGate.release = new Promise((r) => { digestGate.open = r; });
    const atDigest = new Promise((r) => { reached = r; });
    digestGate.reached = reached;
    const inFlight = racing.jwt({ token: expired() });
    await atDigest;
    await racing.config.events.signOut({ token: expired() });
    digestGate.open();
    digestGate = null;
    assert.equal(await inFlight, null, "a sign-out inside the settle step was undone");
    await sleepReal(20);
    assert.ok(idp.revokedBySignOut.includes("rt-1"), "the orphaned rotation was not revoked");
    assert.equal((await grantsDuring(() => racing.jwt({ token: expired({ refreshToken: "rt-1" }) }))).granted, 0);

    // The IdP refuses the revocation: the chain is held for a day and the revocation retried.
    resetIdp();
    idp.revokeStatus = 503;
    const refused = world();
    const r1 = await refused.jwt({ token: expired() });
    await refused.config.events.signOut({ token: r1 });
    assert.ok(logs.some((l) => /revocation answered 503/.test(l)));
    offsetMs += 11 * 60_000;
    assert.equal(await refused.jwt({ token: expired() }), null, "an unrevoked chain was released after ten minutes");
    offsetMs = 0;
    idp.revokeStatus = 200;
    await sleepReal(450); // the first retry, scaled from 30 s
    assert.ok(idp.revokedBySignOut.includes("rt-1"), "the revocation was not retried");

    // Sign-out against an IdP without a revocation endpoint logs that it advertises none.
    resetIdp();
    idp.advertiseRevocation = false;
    const silent = world();
    const s1 = await silent.jwt({ token: expired() });
    await silent.config.events.signOut({ token: s1 });
    assert.ok(logs.some((l) => /advertises no revocation_endpoint/.test(l)));
  }

  // An IdP that cannot be reached: error and cooldown, then recovery once it answers.
  {
    resetIdp("down");
    const { jwt } = world();
    const down = await jwt({ token: expired() });
    assert.equal(down.error, "RefreshAccessTokenError");
    assert.equal((await grantsDuring(() => jwt({ token: { ...down } }))).granted, 0);
    const copy = await grantsDuring(() => jwt({ token: expired() }));
    assert.equal(copy.granted, 0, "an older copy without the cooldown made the IdP refuse the token again");
    assert.equal(copy.out.error, "RefreshAccessTokenError");
    idp.mode = "up";
    offsetMs += 61_000;
    const back = await jwt({ token: { ...down } });
    assert.equal(back.error, undefined);
    assert.equal(back.refreshToken, "rt-1");
    offsetMs = 0;
  }

  // No refresh token: nothing can renew it, so the session is over, without asking the IdP.
  {
    resetIdp();
    const { jwt } = world();
    const none = await grantsDuring(() => jwt({ token: expired({ refreshToken: undefined }) }));
    assert.equal(none.granted, 0);
    assert.equal(none.out.error, "RefreshTokenRevoked");
  }

  // A grant without a groups claim carries the session's groups into the registry, so a healed
  // older cookie does not get its old groups back.
  {
    resetIdp();
    idp.omitGroups = true;
    const { jwt } = world();
    const c0 = expired();
    const c1 = await jwt({ token: { ...c0, groups: ["role_tenant"], roles: ["role_tenant"] } });
    assert.deepEqual(c1.groups, ["role_tenant"]);
    const healed = await jwt({ token: { ...c0 } });
    assert.deepEqual(healed.groups, ["role_tenant"], "a healed cookie kept its stale groups");
  }

  // The role recheck floor holds, measured against the newest grant.
  {
    resetIdp();
    const { jwt } = world();
    const c1 = await jwt({ token: expired() });
    const held = await grantsDuring(() => jwt({ token: { ...c1 }, trigger: "update", session: { refreshRoles: true } }));
    assert.equal(held.granted, 0);
  }

  // Bounds: a user at the limit is refused without asking the IdP; nothing of theirs is evicted, so
  // cycling through old cookies cannot reopen a remembered failure.
  {
    resetIdp();
    const { jwt } = world();
    idp.valid = new Set(Array.from({ length: 20 }, (_, i) => `own-${i}`));
    const first = await grantsDuring(async () => {
      const out = [];
      for (let i = 0; i < 20; i++) out.push(await jwt({ token: expired({ refreshToken: `own-${i}` }) }));
      return out;
    });
    assert.equal(first.granted, 16, "the IdP was asked past the per-user limit");
    assert.ok(first.out.slice(16).every((t) => t.error === "RefreshAccessTokenError"));
    assert.ok(logs.some((l) => /grant refused: subject=[\w-]{8} holds 16/.test(l)));
    assert.equal((await grantsDuring(() => jwt({ token: expired({ refreshToken: "own-0" }) }))).granted, 0, "a kept rotation was evicted");

    resetIdp();
    const failing = world();
    idp.valid = new Set();
    const cycle = async () => {
      for (let i = 0; i < 20; i++) await failing.jwt({ token: expired({ refreshToken: `spent-${i}` }) });
    };
    const rounds = await grantsDuring(async () => { await cycle(); await cycle(); });
    assert.equal(rounds.granted, 16, "old cookies cycled past the failure cache");
  }

  // The whole registry stays bounded across users.
  {
    resetIdp();
    const many = world();
    idp.valid = new Set(Array.from({ length: 4101 }, (_, i) => `seed-${i}`));
    for (let i = 0; i <= 4100; i++) {
      await many.jwt({ token: expired({ authentikId: `user-${i}`, sub: `user-${i}`, refreshToken: `seed-${i}` }) });
    }
    const evicted = await many.jwt({ token: expired({ authentikId: "user-0", sub: "user-0", refreshToken: "seed-0" }) });
    assert.equal(evicted.error, "RefreshTokenRevoked", "the oldest rotation was evicted, so the IdP saw the spent token");
    const kept = await grantsDuring(() => many.jwt({ token: expired({ authentikId: "user-4100", sub: "user-4100", refreshToken: "seed-4100" }) }));
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
      "@/lib/csp": load("frontend/src/lib/csp.ts"),
      "@/lib/session-cookie": { legacySessionCookieExpiries: () => [] },
      "@/lib/session-cookie-echo": { dropUnchangedSessionCookie: async (_q, r) => r, sessionSecret: () => "x" },
      "next/server": { NextResponse: Response },
      "@/app/settings/settings-tabs": { isSettingsTab: () => true },
    });
    assert.ok(middlewareConfig, "middleware builds its own NextAuth instance");
    assert.notEqual(middlewareConfig.callbacks.jwt, config.callbacks.jwt);
    const c0 = expired();
    for (const params of [{ token: c0 }, { token: c0, trigger: "update", session: { refreshRoles: true } }]) {
      assert.equal(await middlewareConfig.callbacks.jwt(params), c0, "middleware returned the token it was given");
    }
    assert.equal(idp.grants, 0, "middleware made a grant");
  }
} finally {
  console.info = quiet.info;
  console.warn = quiet.warn;
  globalThis.setTimeout = realSetTimeout;
}

console.log("token rotation: one writer, no refresh token presented twice, 15 s replay window, failures cached, sign-out revokes");
