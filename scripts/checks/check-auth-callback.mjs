#!/usr/bin/env node
// The page a sign-in returns to. A signed-in visit to /auth/signin and the retry after
// a failed sign-in both follow a callbackUrl, so the parser that admits it is the open-redirect
// boundary. This runs the real modules against the shapes browsers fold into another origin.
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

const redirect = load("frontend/src/lib/auth-redirect.ts");
const { safeCallbackPath } = redirect;
const ORIGIN = "https://qa.invalid";

for (const [raw, expected] of [
  ["/inventory/instances?x=1", "/inventory/instances?x=1"],
  ["/inventory/instances?x=1#row", "/inventory/instances?x=1#row"],
  ["/", "/"],
  ["/authentic", "/authentic"],
  ["/apiary", "/apiary"],
  ["/inventory/../settings", "/settings"],
]) {
  assert.equal(safeCallbackPath(raw), expected, `accepts ${raw}`);
  assert.equal(safeCallbackPath(raw, ORIGIN), expected, `accepts ${raw} with an origin`);
}

for (const raw of [
  undefined, null, "", "x", "inventory", "//evil.example", "//evil.example/x", "/\\evil.example",
  "/\t/evil.example", "/\n/evil.example", "\\/evil.example", "https://evil.example/x",
  "http://qa.invalid/x", "javascript:alert(1)", "/auth", "/auth/signin", "/auth/error?x=1",
  "/AUTH/signin", "/api", "/api/auth/signout", "/api/v1/nodes",
  // Dot segments that fold into another origin after the origin check.
  "/.//evil.com", "/..//evil.com", "/a/..//evil.com", "/%2e//evil.com", "/%2e%2e//evil.com",
  "/x/../\\evil.com", "/.\\/evil.com", "/.//\\evil.com",
]) {
  assert.equal(safeCallbackPath(raw), null, `rejects ${JSON.stringify(raw)}`);
  assert.equal(safeCallbackPath(raw, ORIGIN), null, `rejects ${JSON.stringify(raw)} with an origin`);
}

// next-auth's own /api/auth/signin hands over an absolute URL on the app origin.
assert.equal(safeCallbackPath(`${ORIGIN}/inventory?x=1`, ORIGIN), "/inventory?x=1");
assert.equal(safeCallbackPath(ORIGIN, ORIGIN), "/");
assert.equal(safeCallbackPath(`${ORIGIN}.evil.example/x`, ORIGIN), null);
assert.equal(safeCallbackPath(`${ORIGIN}@evil.example/x`, ORIGIN), null);
assert.equal(safeCallbackPath(`${ORIGIN}/auth/signin`, ORIGIN), null);
assert.equal(safeCallbackPath(`${ORIGIN}/x`), null, "an absolute URL needs the origin to compare");
assert.equal(safeCallbackPath(`${ORIGIN}//evil.com`, ORIGIN), null);
assert.equal(safeCallbackPath(`${ORIGIN}/.//evil.com`, ORIGIN), null);

// redirectTo never leaves the origin, whatever path it is handed.
const nextUrl = new URL(`${ORIGIN}/auth/signin`);
for (const path of ["//evil.com", "/\\evil.com", "/inventory?x=1"]) {
  assert.equal(redirect.redirectTo(nextUrl, null, path).origin, ORIGIN, `redirectTo(${path})`);
}
assert.equal(redirect.redirectTo(nextUrl, null, "/inventory?x=1").href, `${ORIGIN}/inventory?x=1`);

// The tab stash re-validates on every read and survives storage that throws.
const store = new Map();
globalThis.window = {
  sessionStorage: {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
  },
};
const stash = load("frontend/src/lib/auth-return.ts", { "@/lib/auth-redirect": redirect });
stash.rememberReturnPath("/inventory/clusters?view=grid");
assert.equal(stash.recallReturnPath(), "/inventory/clusters?view=grid");
stash.rememberReturnPath("//evil.example");
assert.equal(stash.recallReturnPath(), "/inventory/clusters?view=grid", "an unsafe value is never stored");
store.set("orcastra:auth-return", "https://evil.example");
assert.equal(stash.recallReturnPath(), null, "a tampered entry is refused on read");
stash.forgetReturnPath();
assert.equal(stash.recallReturnPath(), null);
globalThis.window = {
  get sessionStorage() {
    throw new Error("SecurityError");
  },
};
stash.rememberReturnPath("/x");
assert.equal(stash.recallReturnPath(), null);
stash.forgetReturnPath();
delete globalThis.window;

// The middleware branch for a signed-in visit to an auth page must go through the parser.
const auth = readFileSync(join(root, "frontend/src/auth.ts"), "utf8");
assert.match(auth, /safeCallbackPath\(nextUrl\.searchParams\.get\("callbackUrl"\), nextUrl\.origin\) \?\? "\/"/);
const signin = readFileSync(join(root, "frontend/src/app/auth/signin/page.tsx"), "utf8");
assert.doesNotMatch(signin, /signIn\("authentik", \{ callbackUrl \}\)/, "the raw parameter is never followed");
assert.match(signin, /safeCallbackPath\(requested, window\.location\.origin\)/);

console.log("auth callback: ok");
