#!/usr/bin/env node
// Middleware must not echo an unchanged session cookie. NextAuth appends the re-encoded session
// to every middleware response; one that lands after a /api/auth/session refresh restored the
// older session (a rotated refresh token, or a background cooldown). This runs the real helper
// with the real next-auth/jwt encoder and reader against cookies shaped like the live ones.
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

globalThis.crypto ??= webcrypto; // jose's web build; Node 18 has no global crypto

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const frontendRequire = createRequire(join(root, "frontend/package.json"));
const ts = frontendRequire("typescript");
const jwt = await import(pathToFileURL(frontendRequire.resolve("next-auth/jwt")).href);
const core = await import(pathToFileURL(createRequire(frontendRequire.resolve("next-auth/jwt")).resolve("@auth/core")).href);

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

const { dropUnchangedSessionCookie, sameSession, withoutSessionEcho } = load("frontend/src/lib/session-cookie-echo.ts", {
  "next-auth/jwt": jwt,
});

const SECRET = "qa-secret-".repeat(4);
const NAME = "authjs.session-token";
const session = {
  sub: "qa", authentikId: "qa@example.invalid", accessToken: "at-1", refreshToken: "rt-1",
  expiresAt: 2_000_000_000, groups: ["role_partner"], roles: ["role_partner"], groupsReadAt: 1_900_000_000,
};
const cookieOf = async (token, name = NAME) =>
  `${name}=${await jwt.encode({ token, secret: SECRET, salt: name })}`;
const setCookieOf = async (token, name = NAME) => `${await cookieOf(token, name)}; Path=/; HttpOnly; SameSite=Lax`;

function middlewareResponse(setCookies, extra = {}) {
  const headers = new Headers({ "x-middleware-next": "1", ...extra });
  for (const c of setCookies) headers.append("set-cookie", c);
  return new Response(null, { status: 200, headers });
}
const requestWith = (cookie) => new Request("http://qa.invalid/inventory", { headers: { cookie } });
const sessionSetCookies = (response) => response.headers.getSetCookie().filter((c) => c.startsWith(`${NAME}=`));

// The comparison ignores what every encode stamps and nothing else.
assert.equal(sameSession({ ...session, iat: 1, exp: 2, jti: "a" }, { ...session, iat: 3, exp: 4, jti: "b" }), true);
assert.equal(sameSession(session, { ...session, refreshToken: "rt-2" }), false);
assert.equal(sameSession(session, { ...session, retryAfter: 5 }), false);
assert.equal(sameSession(session, { ...session, retryAfter: undefined }), true);
assert.equal(sameSession(session, { ...session, groups: ["role_tenant"] }), false);

const incoming = await cookieOf(session);

// 1. Middleware did not refresh: the echo of the request's own session is dropped, and nothing
//    else about the response changes.
{
  const other = "theme=dark; Path=/";
  const out = await dropUnchangedSessionCookie(
    requestWith(`${incoming}; theme=light`),
    middlewareResponse([await setCookieOf(session), other], { "x-orcastra-probe": "kept" }),
    SECRET,
  );
  assert.deepEqual(sessionSetCookies(out), [], "an unchanged session cookie is not echoed");
  assert.deepEqual(out.headers.getSetCookie(), [other], "other cookies still go out");
  assert.equal(out.headers.get("x-middleware-next"), "1");
  assert.equal(out.headers.get("x-orcastra-probe"), "kept");
  assert.equal(out.status, 200);
}

// 2. A refresh, a new request-path cooldown, or a role change does go out.
for (const changed of [
  { ...session, accessToken: "at-2", refreshToken: "rt-2", expiresAt: 2_000_000_300 },
  { ...session, error: "RefreshAccessTokenError", retryAfter: 1_900_000_060 },
  { ...session, groups: ["role_tenant"], roles: ["role_tenant"] },
]) {
  const out = await dropUnchangedSessionCookie(requestWith(incoming), middlewareResponse([await setCookieOf(changed)]), SECRET);
  assert.equal(sessionSetCookies(out).length, 1, `a changed session is written (${JSON.stringify(changed).slice(0, 60)})`);
}

// 3. Deletions always go through: sign-out, an unreadable session, a retired chunk.
for (const deletion of [`${NAME}=; Path=/; Max-Age=0`, `${NAME}.1=; Path=/; Max-Age=0`]) {
  const out = await dropUnchangedSessionCookie(
    requestWith(incoming),
    middlewareResponse([await setCookieOf(session), deletion]),
    SECRET,
  );
  assert.equal(out.headers.getSetCookie().length, 2, `a deletion keeps the whole session write (${deletion})`);
}

// 4. Anything that cannot be read is left exactly as NextAuth wrote it.
for (const [label, request, secret] of [
  ["no session on the request", requestWith("theme=light"), SECRET],
  ["a session sealed with another secret", requestWith(incoming), "another-secret-".repeat(3)],
  ["no secret configured", requestWith(incoming), undefined],
]) {
  const response = middlewareResponse([await setCookieOf(session)]);
  assert.equal(await dropUnchangedSessionCookie(request, response, secret), response, label);
}

// 5. A request carrying only a bearer is not read as the session (getToken would fall back to it).
{
  const bearer = (await cookieOf(session)).split("=").slice(1).join("=");
  const request = new Request("http://qa.invalid/inventory", { headers: { authorization: `Bearer ${bearer}` } });
  const response = middlewareResponse([await setCookieOf(session)]);
  assert.equal(await dropUnchangedSessionCookie(request, response, SECRET), response, "a bearer is not a session");
}

// 6. The secure-prefixed cookie name is handled the same way.
{
  const secure = "__Secure-authjs.session-token";
  const out = await dropUnchangedSessionCookie(
    requestWith(await cookieOf(session, secure)),
    middlewareResponse([await setCookieOf(session, secure)]),
    SECRET,
  );
  assert.deepEqual(out.headers.getSetCookie(), [], "the secure session cookie is not echoed either");
}

// 7. NextAuth's own session route re-sets an unchanged session on a read; through the wrapper it
//    does not, and a changed session still goes out.
{
  process.env.AUTH_SECRET = SECRET;
  const route = (jwtCallback) => (request) => core.Auth(request, {
    providers: [], secret: SECRET, trustHost: true, basePath: "/api/auth",
    session: { strategy: "jwt" }, callbacks: { jwt: jwtCallback },
  });
  const read = () => requestWith(incoming);
  const sessionRead = (r) => new Request("http://qa.invalid/api/auth/session", { headers: r.headers });
  const raw = await route(({ token }) => token)(sessionRead(read()));
  assert.equal(sessionSetCookies(raw).length, 1, "precondition: NextAuth echoes the session on a read");
  const wrapped = await withoutSessionEcho(route(({ token }) => token))(sessionRead(read()));
  assert.equal(wrapped.status, 200);
  assert.deepEqual(sessionSetCookies(wrapped), [], "the session route echoed an unchanged session");
  const rotated = await withoutSessionEcho(route(({ token }) => ({ ...token, refreshToken: "rt-2" })))(sessionRead(read()));
  assert.equal(sessionSetCookies(rotated).length, 1, "a rotated session must be written");
}

// 8. Middleware builds its own non-refreshing instance and goes through the helper; the session
//    route goes through the wrapper for both methods.
const middleware = readFileSync(join(root, "frontend/src/middleware.ts"), "utf8");
assert.match(middleware, /NextAuth\(\{ \.\.\.authConfig, callbacks: \{ \.\.\.authConfig\.callbacks, jwt: \(\{ token \}\) => token \} \}\)/);
assert.match(middleware, /dropUnchangedSessionCookie\(request, response, sessionSecret\(\)\)/);
assert.doesNotMatch(middleware, /import \{[^}]*\bauth\b[^}]*\} from "@\/auth"/, "middleware must not use the refreshing instance");
const routeSource = readFileSync(join(root, "frontend/src/app/api/auth/[...nextauth]/route.ts"), "utf8");
assert.match(routeSource, /export const GET = withoutSessionEcho\(handlers\.GET\);/);
assert.match(routeSource, /export const POST = withoutSessionEcho\(handlers\.POST\);/);

console.log("session cookie echo: ok");
