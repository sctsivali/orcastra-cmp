#!/usr/bin/env node
// What a failed token refresh means, and what the session does about it.
//
// Authentik deletes a refresh token with the session it belongs to and rotates it on every grant,
// so `invalid_grant` means the token is gone for good. auth.ts used to treat every failure as
// transient: it kept the expired access token, retried every minute, and middleware still read the
// cookie as signed in, so a deep link rendered and every socket ticket came back 401 "Token has
// expired". This runs the real lib/auth-refresh.ts and auth.ts against a fake IdP and holds:
//
// - invalid_grant ends the session (RefreshTokenRevoked, no refresh token, no further grant), and
//   middleware sends an ended session through sign-in, back to the same page;
// - a timeout, a network failure, a 5xx or a 429 cools down and retries (RefreshAccessTokenError);
// - concurrent refreshes of one token make one grant, and a read with the spent token inside the
//   replay window takes the result instead of a second grant;
// - a forced refresh (a socket's ticket refused) runs at most once per 30 s per session;
// - no log line carries token material.
// check-token-rotation.mjs covers the rotation registry itself.
//
// Run: node scripts/checks/check-auth-refresh-contract.mjs [repo-root]

import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

globalThis.crypto ??= webcrypto;

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..'));
const ts = createRequire(join(root, 'frontend/package.json'))('typescript');

function load(relativePath, dependencies = {}) {
  const output = ts.transpileModule(readFileSync(join(root, relativePath), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', output)((name) => {
    if (Object.hasOwn(dependencies, name)) return dependencies[name];
    throw new Error(`Unexpected module dependency: ${name}`);
  }, module, module.exports);
  return module.exports;
}

const SECRET_MARKERS = ['rt-', 'at-', 'client-secret-value', 'example.invalid'];
const logs = [];
const quiet = { info: console.info, warn: console.warn, error: console.error };
const capture = (level) => (line) => logs.push({ level, line: String(line) });

// ---------------------------------------------------------------- lib/auth-refresh.ts alone
const refreshLib = load('frontend/src/lib/auth-refresh.ts');
const { createIdpClient, classifyRefreshFailure, REFRESH_TOKEN_REVOKED, REFRESH_FAILED } = refreshLib;
assert.equal(REFRESH_TOKEN_REVOKED, 'RefreshTokenRevoked');
assert.equal(REFRESH_FAILED, 'RefreshAccessTokenError');

for (const [status, error, kind, level] of [
  [400, 'invalid_grant', 'revoked', 'warn'],
  [401, 'invalid_grant', 'revoked', 'warn'],
  [400, 'invalid_request', 'transient', 'warn'],
  [401, 'invalid_client', 'transient', 'error'],
  [400, 'unauthorized_client', 'transient', 'error'],
  [500, undefined, 'transient', 'warn'],
  [503, undefined, 'transient', 'warn'],
  [429, undefined, 'transient', 'warn'],
  [undefined, undefined, 'transient', 'warn'],
]) {
  assert.deepEqual(classifyRefreshFailure(status, error), { kind, level }, `${status} ${error}`);
}

function idpClient(answer, { now = () => Date.now(), timeoutMs = 200 } = {}) {
  const seen = { discovery: 0, grants: 0 };
  const client = createIdpClient({
    issuer: 'https://idp.invalid/application/o/orcastra/',
    clientId: 'orcastra',
    clientSecret: 'client-secret-value',
    timeoutMs,
    now,
    runtime: 'nodejs',
    log: (level, line) => logs.push({ level, line }),
    fetch: async (url, init = {}) => {
      if (String(url).endsWith('openid-configuration')) {
        seen.discovery += 1;
        return new Response(JSON.stringify({ token_endpoint: 'https://idp.invalid/token/' }), { status: 200 });
      }
      seen.grants += 1;
      return answer(new URLSearchParams(String(init.body)), init);
    },
  });
  return { client, seen };
}
const json = (status, body) => new Response(JSON.stringify(body), { status });

{
  const { client, seen } = idpClient(() => json(400, { error: 'invalid_grant', error_description: 'rt-0 was revoked' }));
  assert.deepEqual(await client.grant('rt-0'), { ok: false, kind: 'revoked' });
  assert.match(logs.at(-1).line, /^\[AUTH\] refresh failed runtime=nodejs kind=revoked status=400 error=invalid_grant ms=\d+$/);
  assert.equal(logs.at(-1).level, 'warn');
  await client.grant('rt-0');
  assert.equal(seen.discovery, 1, 'discovery is cached across grants');
}
{
  // A timeout says nothing about the token.
  const { client } = idpClient((_form, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener('abort', () => reject(init.signal.reason));
  }), { timeoutMs: 30 });
  // note: AbortSignal.timeout's timer does not hold the event loop open; this one does.
  const keepAlive = setTimeout(() => {}, 5_000);
  assert.deepEqual(await client.grant('rt-0'), { ok: false, kind: 'transient' });
  clearTimeout(keepAlive);
  assert.match(logs.at(-1).line, /kind=transient status=none error=TimeoutError ms=\d+$/);
}
{
  const { client } = idpClient(() => { throw new TypeError('fetch failed rt-0'); });
  assert.deepEqual(await client.grant('rt-0'), { ok: false, kind: 'transient' });
  assert.match(logs.at(-1).line, /error=TypeError/);
}
for (const status of [500, 502, 503, 429]) {
  const { client } = idpClient(() => new Response('<html>busy</html>', { status }));
  assert.deepEqual(await client.grant('rt-0'), { ok: false, kind: 'transient' }, `HTTP ${status}`);
  assert.match(logs.at(-1).line, new RegExp(`status=${status} error=other`));
}
{
  const { client } = idpClient(() => json(401, { error: 'invalid_client' }));
  assert.deepEqual(await client.grant('rt-0'), { ok: false, kind: 'transient' });
  assert.equal(logs.at(-1).level, 'error', 'a client the IdP does not know is logged at error');
}
{
  const { client } = idpClient(() => json(400, { error: 'Bearer rt-0 at-9 <script>' }));
  await client.grant('rt-0');
  assert.match(logs.at(-1).line, /error=other/, 'an error code that is not a plain word is not logged');
}
{
  const { client } = idpClient((form) => json(200, { access_token: 'at-1', refresh_token: 'rt-1', expires_in: 300, presented: form.get('refresh_token') }));
  assert.deepEqual(await client.grant('rt-0'), { ok: true, tokens: { accessToken: 'at-1', refreshToken: 'rt-1', expiresIn: 300 } });
  const { client: steady } = idpClient(() => json(200, { access_token: 'at-1', expires_in: 300 }));
  assert.equal((await steady.grant('rt-0')).tokens.refreshToken, 'rt-0', 'an IdP that does not rotate keeps the token');
}

// ---------------------------------------------------------------- auth.ts against a fake IdP
const realNow = Date.now.bind(Date);
let offsetMs = 0;
Date.now = () => realNow() + offsetMs;
const nowS = () => Math.floor(Date.now() / 1000);
let idp;
function resetIdp(mode = 'up') {
  idp = { mode, valid: new Set(['rt-0']), n: 0, grants: 0, latencyMs: 0 };
}
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
globalThis.fetch = async (url, init = {}) => {
  if (String(url).endsWith('openid-configuration')) {
    return new Response(JSON.stringify({ token_endpoint: 'https://idp.invalid/token/' }), { status: 200 });
  }
  idp.grants += 1;
  const presented = new URLSearchParams(String(init.body)).get('refresh_token');
  if (idp.latencyMs) await new Promise((r) => setTimeout(r, idp.latencyMs));
  if (idp.mode === 'down') throw new TypeError('fetch failed');
  if (idp.mode === '503') return new Response('busy', { status: 503 });
  if (!idp.valid.has(presented)) return json(400, { error: 'invalid_grant' });
  idp.valid.delete(presented);
  const next = `rt-${++idp.n}`;
  idp.valid.add(next);
  return json(200, { access_token: `${b64({ alg: 'none' })}.${b64({ groups: ['role_tenant'], n: idp.n })}.sig`, refresh_token: next, expires_in: 300 });
};
process.env.AUTHENTIK_ISSUER = 'https://idp.invalid/application/o/orcastra/';
process.env.AUTHENTIK_CLIENT_SECRET = 'client-secret-value';
console.info = capture('info');
console.warn = capture('warn');
console.error = capture('error');

const redirect = load('frontend/src/lib/auth-redirect.ts');
function world() {
  const auth = load('frontend/src/auth.ts', {
    'next-auth': { __esModule: true, default: (config) => ({ handlers: {}, config }) },
    '@/lib/auth-redirect': redirect,
    '@/lib/token-claims': load('frontend/src/lib/token-claims.ts'),
    '@/lib/refresh-grants': load('frontend/src/lib/refresh-grants.ts'),
    '@/lib/auth-refresh': refreshLib,
    '@/types/rbac': load('frontend/src/types/rbac.ts'),
  });
  return { jwt: auth.authConfig.callbacks.jwt, authorized: auth.authConfig.callbacks.authorized };
}
const expired = (over = {}) => ({
  sub: 'buyer', authentikId: 'buyer@example.invalid', accessToken: 'at-0', refreshToken: 'rt-0',
  expiresAt: nowS() - 5, groups: ['role_tenant'], roles: ['role_tenant'], groupsReadAt: nowS() - 600, ...over,
});
async function grantsDuring(fn) {
  const before = idp.grants;
  const out = await fn();
  return { out, granted: idp.grants - before };
}

try {
  {
    // The IdP ended the session: the refresh token goes, the error says so, nothing more is asked.
    resetIdp();
    idp.valid = new Set();
    const { jwt } = world();
    const ended = await jwt({ token: expired() });
    assert.equal(ended.error, 'RefreshTokenRevoked');
    assert.equal(ended.refreshToken, undefined);
    assert.equal(ended.accessToken, 'at-0', 'the stale access token stays so an open page keeps its identity');
    assert.equal(ended.retryAfter, undefined);
    const again = await grantsDuring(() => jwt({ token: { ...ended } }));
    assert.equal(again.granted, 0);
    assert.equal(again.out.error, 'RefreshTokenRevoked');
    const forced = await grantsDuring(() => jwt({ token: { ...ended }, trigger: 'update', session: { forceRefresh: true } }));
    assert.equal(forced.granted, 0, 'not even a forced refresh asks again');
  }
  for (const mode of ['down', '503']) {
    // A failure that says nothing about the token cools down and keeps the refresh token.
    resetIdp(mode);
    const { jwt } = world();
    const cooled = await jwt({ token: expired() });
    assert.equal(cooled.error, 'RefreshAccessTokenError', mode);
    assert.ok(cooled.retryAfter > nowS(), mode);
    assert.equal(cooled.refreshToken, 'rt-0', mode);
    idp.mode = 'up';
    offsetMs += 61_000;
    const back = await jwt({ token: { ...cooled } });
    assert.equal(back.error, undefined, `${mode}: recovers once the IdP answers`);
    offsetMs = 0;
  }
  {
    // Concurrent reads of one expired cookie: one grant, everyone gets its answer.
    resetIdp();
    idp.latencyMs = 30;
    const { jwt } = world();
    const burst = await grantsDuring(() => Promise.all(Array.from({ length: 8 }, () => jwt({ token: expired() }))));
    assert.equal(burst.granted, 1);
    assert.ok(burst.out.every((t) => t.refreshToken === 'rt-1' && !t.error));
    // A read still holding the spent token, inside the replay window, takes the result.
    const late = await grantsDuring(() => jwt({ token: expired() }));
    assert.equal(late.granted, 0);
    assert.equal(late.out.refreshToken, 'rt-1');
  }
  {
    // A forced refresh runs at most once per 30 s per session, and not during a cooldown.
    resetIdp();
    const { jwt } = world();
    const fresh = await jwt({ token: expired() });
    const update = (token) => jwt({ token: { ...token }, trigger: 'update', session: { forceRefresh: true } });
    assert.equal((await grantsDuring(() => update(fresh))).granted, 0, 'inside the floor');
    offsetMs += 31_000;
    const forced = await grantsDuring(() => update(fresh));
    assert.equal(forced.granted, 1, 'past the floor');
    assert.equal(forced.out.refreshToken, 'rt-2');
    offsetMs += 31_000;
    const cooling = { ...forced.out, retryAfter: nowS() + 30 };
    assert.equal((await grantsDuring(() => update(cooling))).granted, 0, 'a cooldown holds a forced refresh too');
    offsetMs = 0;
  }
  {
    // Middleware's edge runtime never refreshes, even through this callback.
    resetIdp();
    const { jwt } = world();
    process.env.NEXT_RUNTIME = 'edge';
    const edge = await grantsDuring(() => jwt({ token: expired() }));
    delete process.env.NEXT_RUNTIME;
    assert.equal(edge.granted, 0);
    assert.equal(edge.out.refreshToken, 'rt-0');
  }
  {
    // A sign-in starts clean, whatever the cookie said before.
    resetIdp();
    const { jwt } = world();
    const signedIn = await jwt({
      token: { error: 'RefreshTokenRevoked', retryAfter: nowS() + 60 },
      user: { id: 'buyer@example.invalid', email: 'buyer@example.invalid' },
      account: { access_token: 'at-new', refresh_token: 'rt-new', expires_at: nowS() + 300 },
    });
    assert.equal(signedIn.error, undefined);
    assert.equal(signedIn.retryAfter, undefined);
    assert.equal(signedIn.refreshToken, 'rt-new');
  }
  {
    // Middleware: an ended session is signed out, so a deep link goes through sign-in and back.
    const { authorized } = world();
    const request = (path) => ({ nextUrl: new URL(`https://dash.invalid${path}`), headers: new Headers({ host: 'dash.invalid' }) });
    const ended = { user: { id: 'buyer', groups: ['role_tenant'] }, error: 'RefreshTokenRevoked' };
    const live = { user: { id: 'buyer', groups: ['role_tenant'] } };
    const deepLink = '/terminal/term_web-1?instance=web-1&host=c1&project=shop';

    const sent = authorized({ auth: ended, request: request(deepLink) });
    assert.ok(sent instanceof Response, 'an ended session is not let through');
    const location = new URL(sent.headers.get('location'));
    assert.equal(location.pathname, '/auth/signin');
    assert.equal(location.searchParams.get('callbackUrl'), deepLink);

    assert.equal(authorized({ auth: ended, request: request(`/auth/signin?callbackUrl=${encodeURIComponent(deepLink)}`) }), true,
      'the sign-in page is not bounced home for an ended session');
    assert.equal(authorized({ auth: live, request: request(deepLink) }), true);
    const bounced = authorized({ auth: live, request: request(`/auth/signin?callbackUrl=${encodeURIComponent(deepLink)}`) });
    assert.equal(new URL(bounced.headers.get('location')).pathname, '/terminal/term_web-1', 'a live session still skips sign-in');
    assert.equal(authorized({ auth: { ...live, error: 'RefreshAccessTokenError' }, request: request(deepLink) }), true,
      'a transient failure keeps the page');
  }

  // Nothing above logged a token, a secret or a subject.
  for (const { line } of logs) {
    for (const marker of SECRET_MARKERS) assert.ok(!line.includes(marker), `a log line carries "${marker}": ${line}`);
  }
  assert.ok(logs.some(({ line }) => /^\[AUTH\] refresh failed runtime=nodejs kind=revoked /.test(line)));
} finally {
  console.info = quiet.info;
  console.warn = quiet.warn;
  console.error = quiet.error;
  Date.now = realNow;
}

console.log('auth refresh contract: invalid_grant ends the session, other failures cool down, one grant per token, no token in a log line');
