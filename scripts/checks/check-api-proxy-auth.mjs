#!/usr/bin/env node
// The /api/v1 proxy speaks for the browser with the session in its cookie. It attaches that
// session's access token itself, so the token never has to reach page JavaScript, and it refuses
// what a cookie-authenticated proxy must refuse: a cross-site write, a session the identity
// provider ended, and a tab acting for an account that is no longer the signed-in one. A caller
// without a session cookie (an integration's API key, an MCP client) passes through untouched.
// This runs the real decision module with real next-auth/jwt cookies.
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

const sessionCookie = load("frontend/src/lib/session-cookie.ts", { "next-auth/jwt": jwt });
const proxyAuth = load("frontend/src/lib/api-proxy-auth.ts", {
  "@/lib/session-cookie": sessionCookie,
  "@/lib/auth-headers": load("frontend/src/lib/auth-headers.ts"),
  "@/lib/auth-refresh": load("frontend/src/lib/auth-refresh.ts"),
});
const { decideProxyAuth, resolveProxyAuth, applyProxyAuth, ACTOR_HEADER } = proxyAuth;
const { sessionCookieName, carriesSessionCookie, pinnedSessionCookies } = sessionCookie;

const SECRET = "qa-secret-".repeat(4);
const OTHER_SECRET = "other-deployment-".repeat(3);
const CONFIGURED = "http://100.64.0.10:2025";
const session = {
  sub: "sub-1", authentikId: "qa-partner@example.invalid", accessToken: "at-session",
  refreshToken: "rt-1", expiresAt: 2_000_000_000, groups: ["role_partner"],
};
const encode = (token, name = "authjs.session-token", secret = SECRET) =>
  jwt.encode({ token, secret, salt: name }).then((value) => `${name}=${value}`);

// The page names its account on every request; `null` in `extra` drops a header.
const ACTOR = encodeURIComponent("qa-partner@example.invalid");
function headers(extra = {}) {
  const all = { host: "100.64.0.10:2025", [ACTOR_HEADER]: ACTOR, ...extra };
  for (const [name, value] of Object.entries(all)) if (value === null) delete all[name];
  return new Headers(all);
}
// An explicit `undefined` session means "no cookie", so defaults apply only to omitted arguments.
async function decide(method, extra, ...rest) {
  const token = rest.length > 0 ? rest[0] : session;
  const configured = rest.length > 1 ? rest[1] : CONFIGURED;
  return decideProxyAuth(method, headers(extra), token, configured);
}

// 1. Cookie name: the one NextAuth itself uses for the request, never whichever is present.
{
  const H = (extra = {}) => new Headers(extra);
  const PLAIN = "authjs.session-token";
  const SECURE = "__Secure-authjs.session-token";
  const HOST = "__Host-authjs.session-token";
  assert.equal(sessionCookieName(H(), "http://x.invalid/api/v1/a", "http://x.invalid"), PLAIN);
  assert.equal(sessionCookieName(H(), "https://x.invalid/api/v1/a", "http://x.invalid"), PLAIN, "the configured URL decides, as in NextAuth");
  assert.equal(sessionCookieName(H(), "http://x.invalid/api/v1/a", "https://x.invalid"), HOST, "an https deployment pins __Host-");
  assert.equal(sessionCookieName(H(), "https://x.invalid/api/v1/a", undefined), SECURE);
  assert.equal(sessionCookieName(H({ "x-forwarded-proto": "https" }), "http://x.invalid/api/v1/a", undefined), SECURE);
  assert.equal(sessionCookieName(H({ "x-forwarded-proto": "http" }), "https://x.invalid/api/v1/a", undefined), PLAIN);
  assert.equal(sessionCookieName(H(), "http://x.invalid/api/v1/a", "::not a url::"), PLAIN);
  assert.equal(carriesSessionCookie(`a=1; ${PLAIN}=x`, PLAIN), true);
  assert.equal(carriesSessionCookie(`${PLAIN}.0=x; ${PLAIN}.1=y`, PLAIN), true, "a chunked session");
  assert.equal(carriesSessionCookie(`${SECURE}=y`, PLAIN), false);
  // The pinned cookie: only for a configured https URL, and with what the __Host- prefix demands
  // (Secure, Path=/, no Domain), or the browser drops it.
  assert.equal(pinnedSessionCookies("http://x.invalid"), undefined);
  assert.equal(pinnedSessionCookies(undefined), undefined);
  assert.equal(pinnedSessionCookies("::not a url::"), undefined);
  const pinned = pinnedSessionCookies("https://app.example.invalid");
  assert.equal(pinned.sessionToken.name, HOST);
  assert.deepEqual(pinned.sessionToken.options, { httpOnly: true, sameSite: "lax", path: "/", secure: true });
  assert.equal("domain" in pinned.sessionToken.options, false);
  const authSource = readFileSync(join(root, "frontend/src/auth.ts"), "utf8");
  assert.match(authSource, /cookies: pinnedSessionCookies\(process\.env\.AUTH_URL \?\? process\.env\.NEXTAUTH_URL\),/,
    "NextAuth writes the session cookie under the name the proxy reads");
  assert.equal(carriesSessionCookie("authjs.csrf-token=x; theme=dark", PLAIN), false);
  assert.equal(carriesSessionCookie("", PLAIN), false);
  assert.equal(carriesSessionCookie(null, PLAIN), false);
}

// 2. The session in a real cookie is read from the Cookie header only.
{
  const cookie = await encode(session);
  const read = await resolveProxyAuth(new Request("http://100.64.0.10:2025/api/v1/clusters", {
    headers: { host: "100.64.0.10:2025", cookie, authorization: "Bearer caller-chosen", [ACTOR_HEADER]: ACTOR },
  }), SECRET, CONFIGURED);
  assert.deepEqual(read, { kind: "session", bearer: "at-session" }, "the cookie's token replaces the caller's header");

  const hosted = await encode(session, "__Host-authjs.session-token");
  const readHosted = await resolveProxyAuth(new Request("https://dash.example.invalid/api/v1/clusters", {
    headers: { host: "dash.example.invalid", cookie: hosted, [ACTOR_HEADER]: ACTOR },
  }), SECRET, "https://dash.example.invalid");
  assert.deepEqual(readHosted, { kind: "session", bearer: "at-session" }, "an https deployment reads the __Host- name");
  // On that deployment a __Secure- cookie, which a sibling host could set, is not the session.
  const siblingSet = await encode({ ...session, accessToken: "at-sibling" }, "__Secure-authjs.session-token");
  const readSibling = await resolveProxyAuth(new Request("https://dash.example.invalid/api/v1/clusters", {
    headers: { host: "dash.example.invalid", cookie: `${siblingSet}; ${hosted}`, [ACTOR_HEADER]: ACTOR },
  }), SECRET, "https://dash.example.invalid");
  assert.deepEqual(readSibling, { kind: "session", bearer: "at-session" }, "a sibling's __Secure- cookie is ignored");
  //    With no URL configured NextAuth names it by the request, and so does the proxy.
  const secure = await encode(session, "__Secure-authjs.session-token");
  const readSecure = await resolveProxyAuth(new Request("https://dash.example.invalid/api/v1/clusters", {
    headers: { host: "dash.example.invalid", cookie: secure, [ACTOR_HEADER]: ACTOR },
  }), SECRET, undefined);
  assert.deepEqual(readSecure, { kind: "session", bearer: "at-session" }, "an unconfigured https request reads __Secure-");

  // A cookie of the other name, planted by a host on the same site, is not what NextAuth reads, so
  // neither does the proxy: the real session stays the one used.
  const planted = await encode({ ...session, accessToken: "at-planted" }, "__Secure-authjs.session-token");
  const readPlanted = await resolveProxyAuth(new Request("http://100.64.0.10:2025/api/v1/clusters", {
    headers: { host: "100.64.0.10:2025", cookie: `${planted}; ${cookie}`, [ACTOR_HEADER]: ACTOR },
  }), SECRET, CONFIGURED);
  assert.deepEqual(readPlanted, { kind: "session", bearer: "at-session" }, "a planted cookie of the other name is ignored");

  // Without the actor header no session is attached: a navigation, or a script that does not name
  // the account it acts for.
  const noActor = await resolveProxyAuth(new Request("http://100.64.0.10:2025/api/v1/my-keys/k1/download", {
    headers: { host: "100.64.0.10:2025", cookie, accept: "text/html,application/xhtml+xml" },
  }), SECRET, CONFIGURED);
  assert.deepEqual(noActor, { kind: "anonymous" }, "a link from any site on plain http reaches the API without the session");

  // A bearer alone is never read as the session (getToken's fallback must not apply).
  const bearerOnly = await resolveProxyAuth(new Request("http://100.64.0.10:2025/api/v1/clusters", {
    headers: { host: "100.64.0.10:2025", authorization: `Bearer ${cookie.split("=")[1]}` },
  }), SECRET, CONFIGURED);
  assert.deepEqual(bearerOnly, { kind: "anonymous" }, "a session JWE sent as a bearer is not a session");
  // An empty session cookie is where getToken would fall back to the bearer.
  const emptyCookie = await resolveProxyAuth(new Request("http://100.64.0.10:2025/api/v1/clusters", {
    headers: { host: "100.64.0.10:2025", cookie: "authjs.session-token=", authorization: `Bearer ${cookie.split("=")[1]}` },
  }), SECRET, CONFIGURED);
  assert.deepEqual(emptyCookie, { kind: "anonymous" }, "an empty session cookie does not turn a bearer into the session");

  // Another deployment's cookie on the same host (cookies ignore the port) is no session here.
  const foreign = await encode(session, "authjs.session-token", OTHER_SECRET);
  const readForeign = await resolveProxyAuth(new Request("http://100.64.0.10:2025/api/v1/clusters", {
    headers: { host: "100.64.0.10:2025", cookie: foreign, authorization: "Bearer id:secret" },
  }), SECRET, CONFIGURED);
  assert.deepEqual(readForeign, { kind: "anonymous" }, "an unreadable cookie grants nothing and blocks nothing");

  const noSecret = await resolveProxyAuth(new Request("http://100.64.0.10:2025/api/v1/clusters", {
    headers: { host: "100.64.0.10:2025", cookie },
  }), undefined, CONFIGURED);
  assert.deepEqual(noSecret, { kind: "anonymous" }, "without a secret nothing is attached");
}

// 3. No session cookie: an API key or MCP caller passes through as it came.
assert.deepEqual(await decide("POST", { authorization: "Bearer id:secret" }, undefined), { kind: "anonymous" });
assert.deepEqual(await decide("POST", { origin: "https://evil.example" }, undefined), { kind: "anonymous" },
  "without a session there is nothing to forge, so the backend decides");

// 4. Reads that name their account: the page's own, and a script's that names it.
assert.deepEqual(await decide("GET", {}), { kind: "session", bearer: "at-session" }, "no fetch metadata (http, curl, Playwright)");
assert.deepEqual(await decide("GET", { "sec-fetch-site": "same-origin" }), { kind: "session", bearer: "at-session" });
//    A navigation never names its account, so it never gets the session: a typed address, a link
//    from mail or chat, a link on another site over plain http (no Origin, no fetch metadata).
assert.deepEqual(await decide("GET", { "sec-fetch-site": "none", [ACTOR_HEADER]: null }), { kind: "anonymous" }, "typed in the address bar");
assert.deepEqual(await decide("GET", { [ACTOR_HEADER]: null, accept: "text/html" }), { kind: "anonymous" }, "a cross-site link over plain http");
assert.deepEqual(await decide("GET", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", [ACTOR_HEADER]: null }), { kind: "anonymous" });
assert.deepEqual(await decide("POST", { [ACTOR_HEADER]: null }), { kind: "anonymous" }, "a write that names no account gets no session");
assert.deepEqual(await decide("HEAD", { "sec-fetch-site": "same-origin" }), { kind: "session", bearer: "at-session" });
//    A read another site starts goes out without the session.
assert.deepEqual(await decide("GET", { "sec-fetch-site": "cross-site" }), { kind: "anonymous" });
assert.deepEqual(await decide("GET", { "sec-fetch-site": "same-site" }), { kind: "anonymous" }, "a sibling subdomain is another origin");
assert.deepEqual(await decide("GET", { origin: "https://evil.example" }), { kind: "anonymous" }, "plain http sends Origin but no fetch metadata");

// 5. Writes need the request to come from this origin.
for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
  assert.deepEqual(await decide(method, { "sec-fetch-site": "same-origin", origin: "http://100.64.0.10:2025" }),
    { kind: "session", bearer: "at-session" }, `${method} from the app`);
  const crossSite = await decide(method, { "sec-fetch-site": "cross-site", origin: "https://evil.example" });
  assert.equal(crossSite.kind, "refuse", `${method} from another site is refused`);
  assert.equal(crossSite.status, 403);
  assert.equal(crossSite.signal, "cross-site");
}
//    Plain http: no fetch metadata, so Origin decides, against the configured URL or the Host.
assert.deepEqual(await decide("POST", { origin: "http://100.64.0.10:2025" }), { kind: "session", bearer: "at-session" });
assert.deepEqual(await decide("POST", { origin: "https://dash.example.invalid", host: "dash.example.invalid" }),
  { kind: "session", bearer: "at-session" }, "a second hostname (a tunnel) is its own origin by Host");
assert.deepEqual(await decide("POST", { origin: "https://dash.example.invalid", host: "127.0.0.1:2025", "x-forwarded-host": "dash.example.invalid" }),
  { kind: "session", bearer: "at-session" }, "behind a proxy that forwards the host");
assert.deepEqual(await decide("POST", { origin: "http://100.64.0.10:2025", host: "127.0.0.1:2025" }),
  { kind: "session", bearer: "at-session" }, "the configured URL is always this origin");
assert.equal((await decide("POST", { origin: "http://100.64.0.10:2026" })).kind, "refuse", "another port is another origin");
assert.equal((await decide("POST", { origin: "http://evil.example" })).kind, "refuse");
assert.equal((await decide("POST", { origin: "null" })).kind, "refuse", "an opaque origin (sandboxed frame, data: URL)");
assert.equal((await decide("POST", { origin: "not a url" })).kind, "refuse");
assert.equal((await decide("POST", { "sec-fetch-site": "same-site" })).kind, "refuse", "metadata without Origin is a browser, not a script");
assert.equal((await decide("POST", { "sec-fetch-site": "cross-site", origin: "http://100.64.0.10:2025" })).kind, "refuse",
  "the browser's own verdict wins over a matching Origin");
//    A script or the QA harness with the cookie sends neither header.
assert.deepEqual(await decide("POST", {}), { kind: "session", bearer: "at-session" });
//    A configured URL that does not parse is ignored rather than trusted.
assert.equal((await decide("POST", { origin: "http://evil.example" }, session, "::not a url::")).kind, "refuse");
assert.deepEqual(await decide("POST", { origin: "http://100.64.0.10:2025" }, session, "::not a url::"),
  { kind: "session", bearer: "at-session" });

// 6. A session the identity provider ended, or one without a token, is over here too.
for (const ended of [{ ...session, error: "RefreshTokenRevoked" }, { ...session, accessToken: undefined }]) {
  const out = await decide("GET", {}, ended);
  assert.equal(out.kind, "refuse");
  assert.equal(out.status, 401);
  assert.equal(out.signal, "session-ended");
}
//    A transient refresh failure keeps the token it has; the backend judges its expiry.
assert.deepEqual(await decide("GET", {}, { ...session, error: "RefreshAccessTokenError" }), { kind: "session", bearer: "at-session" });
//    A cross-site request, or one that names no account, learns nothing about the session's state.
assert.deepEqual(await decide("GET", { "sec-fetch-site": "cross-site" }, { ...session, error: "RefreshTokenRevoked" }), { kind: "anonymous" });
assert.deepEqual(await decide("GET", { [ACTOR_HEADER]: null }, { ...session, error: "RefreshTokenRevoked" }), { kind: "anonymous" });

// 7. A tab that loaded for one account never acts for another.
assert.deepEqual(await decide("POST", { [ACTOR_HEADER]: encodeURIComponent("qa-partner@example.invalid") }),
  { kind: "session", bearer: "at-session" });
{
  const out = await decide("POST", { [ACTOR_HEADER]: encodeURIComponent("qa-admin@example.invalid") });
  assert.equal(out.kind, "refuse");
  assert.equal(out.status, 401);
  assert.equal(out.signal, "actor-changed");
}
assert.equal((await decide("GET", { [ACTOR_HEADER]: "%E0%A4%A" })).signal, "actor-changed", "a malformed actor is not this actor");
assert.deepEqual(await decide("GET", { [ACTOR_HEADER]: "sub-only" }, { ...session, authentikId: undefined, sub: "sub-only" }),
  { kind: "session", bearer: "at-session" }, "the subject falls back to sub, as the session's user id does");
assert.deepEqual(await decide("GET", { [ACTOR_HEADER]: "anyone" }, undefined), { kind: "anonymous" },
  "without a session the actor header means nothing");

// 8. What reaches the backend: the session's bearer or the caller's own header, never the
//    cookie, the browser's Origin, or the actor hint.
{
  const upstream = new Headers({
    cookie: "authjs.session-token=x", origin: "http://100.64.0.10:2025", authorization: "Bearer stale",
    [ACTOR_HEADER]: "qa", "content-type": "application/json",
  });
  applyProxyAuth(upstream, { kind: "session", bearer: "at-session" });
  assert.equal(upstream.get("authorization"), "Bearer at-session");
  assert.equal(upstream.get("cookie"), null);
  assert.equal(upstream.get("origin"), null);
  assert.equal(upstream.get(ACTOR_HEADER), null);
  assert.equal(upstream.get("content-type"), "application/json");

  const keyed = new Headers({ cookie: "authjs.session-token=foreign", authorization: "Bearer id:secret", origin: "https://tool.example" });
  applyProxyAuth(keyed, { kind: "anonymous" });
  assert.equal(keyed.get("authorization"), "Bearer id:secret", "an API key caller keeps its key");
  assert.equal(keyed.get("cookie"), null);
  assert.equal(keyed.get("origin"), null);

  // A read another site started never carries a token the page put there either.
  const crossRead = new Headers({ cookie: "authjs.session-token=x", "sec-fetch-site": "cross-site" });
  applyProxyAuth(crossRead, { kind: "anonymous" });
  assert.equal(crossRead.get("authorization"), null);
}

// 9. The browser leg: no upstream CORS, and an answer made with the session stays private.
{
  const { finishProxyResponse } = proxyAuth;
  const SESSION = { kind: "session", bearer: "at" };
  const finish = (init, auth = SESSION) => {
    const h = new Headers(init);
    finishProxyResponse(h, auth);
    return h;
  };
  const cors = finish({ "access-control-allow-origin": "https://evil.example", "access-control-allow-credentials": "true", "content-type": "application/json" });
  assert.equal(cors.get("access-control-allow-origin"), null);
  assert.equal(cors.get("access-control-allow-credentials"), null);
  assert.equal(cors.get("content-type"), "application/json");
  assert.equal(finish({}).get("cache-control"), "private");
  assert.equal(finish({}).get("vary"), "Cookie");
  assert.equal(finish({ "cache-control": "public, max-age=60" }).get("cache-control"), "private, max-age=60");
  assert.equal(finish({ "cache-control": "max-age=30, stale-while-revalidate=60" }).get("cache-control"), "private, max-age=30, stale-while-revalidate=60");
  assert.equal(finish({ "cache-control": "no-store" }).get("cache-control"), "no-store");
  assert.equal(finish({ "cache-control": "private, max-age=5" }).get("cache-control"), "private, max-age=5");
  assert.equal(finish({ vary: "Accept-Encoding" }).get("vary"), "Accept-Encoding, Cookie");
  assert.equal(finish({ vary: "Cookie, Accept-Encoding" }).get("vary"), "Cookie, Accept-Encoding");
  //    A caller with its own key is not answered with the session, so nothing changes but CORS.
  const keyed = finish({ "cache-control": "public, max-age=60", "access-control-allow-origin": "*" }, { kind: "anonymous" });
  assert.equal(keyed.get("cache-control"), "public, max-age=60");
  assert.equal(keyed.get("vary"), null);
  assert.equal(keyed.get("access-control-allow-origin"), null);
}

// 10. No page code holds the access token or sends a bearer of its own: the session reaches the
//     backend only through the proxy. Only the server-side session code may name the token.
{
  const { readdirSync, statSync } = await import("node:fs");
  const SERVER_SIDE = new Set([
    "auth.ts", "auth-session.ts", "middleware.ts",
    "lib/api-proxy-auth.ts", "lib/session-cookie.ts", "lib/session-cookie-echo.ts",
    "lib/auth-refresh.ts", "lib/refresh-grants.ts", "lib/token-stamp.ts",
  ]);
  const src = join(root, "frontend/src");
  const files = (function walk(dir) {
    return readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) return walk(path);
      return /\.(?:ts|tsx)$/.test(name) ? [path.slice(src.length + 1)] : [];
    });
  })(src);
  const offenders = files
    .filter((file) => !SERVER_SIDE.has(file) && !file.startsWith("app/api/"))
    .filter((file) => /accessToken|["']?Authorization["']?\s*[:\]]|Bearer \$\{/.test(readFileSync(join(src, file), "utf8")));
  assert.deepEqual(offenders, [], "page code names the access token or sets a bearer");
  const apiSource = readFileSync(join(src, "lib/api.ts"), "utf8");
  assert.match(apiSource, /Object\.assign\(headers, authHeaders\(\)\);/, "fetchAPI names the installed actor on every request");
  for (const file of files) {
    const text = readFileSync(join(src, file), "utf8");
    if (/^["']use client["']/m.test(text)) {
      assert.doesNotMatch(text, /@\/lib\/(?:api-proxy-auth|session-cookie)["']/, `${file}: a client module imports the server session reader`);
    }
  }
}

console.log("check-api-proxy-auth: ok");
