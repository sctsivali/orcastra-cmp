#!/usr/bin/env node
// Every page response carries its own nonce, and only scripts that carry it run: the enforced
// script-src is 'nonce-<n>' 'strict-dynamic', the report-only policy adds what a stricter policy
// would block, and violations reach /api/csp-report, which must take hostile input safely. This
// runs the real policy builder and report reader, and checks the wiring that makes the nonce reach
// Next's scripts and the root layout's own.
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

globalThis.crypto ??= webcrypto;

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const frontendRequire = createRequire(join(root, "frontend/package.json"));
const ts = frontendRequire("typescript");

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
const read = (path) => readFileSync(join(root, path), "utf8");

const csp = load("frontend/src/lib/csp.ts");
const report = load("frontend/src/lib/csp-report.ts");

// 1. A nonce is 128 random bits, new every time.
{
  const seen = new Set(Array.from({ length: 200 }, () => csp.newNonce()));
  assert.equal(seen.size, 200, "a nonce repeated");
  for (const nonce of seen) assert.match(nonce, /^[A-Za-z0-9+/]{22}==$/);
}

// 2. The policies.
{
  const nonce = "AAAAAAAAAAAAAAAAAAAAAA==";
  const parse = (policy) => Object.fromEntries(policy.split("; ").map((d) => [d.split(" ")[0], d]));
  const enforced = parse(csp.contentSecurityPolicies({ nonce, dev: false, mode: "enforce" }).enforced);
  assert.equal(enforced["script-src"], `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`);
  assert.equal(enforced["default-src"], undefined, "a default-src would also become style-src and break inline styles");
  assert.equal(enforced["style-src"], undefined);
  for (const d of ["object-src 'none'", "base-uri 'self'", "form-action 'self'", "frame-ancestors 'self'", "report-uri /api/csp-report"]) {
    assert.equal(enforced[d.split(" ")[0]], d);
  }
  assert.doesNotMatch(JSON.stringify(enforced), /unsafe-eval|unsafe-inline/, "production enforces no unsafe source for scripts");
  assert.match(csp.contentSecurityPolicies({ nonce, dev: true, mode: "enforce" }).enforced, /'strict-dynamic' 'unsafe-eval'/, "next dev needs eval");

  const fallback = parse(csp.contentSecurityPolicies({ nonce, dev: false, mode: "report-only" }).enforced);
  assert.equal(fallback["script-src"], undefined, "the switch back moves script-src out of the enforced policy");
  assert.equal(fallback["object-src"], "object-src 'none'", "and keeps the rest enforced");

  const wide = parse(csp.contentSecurityPolicies({ nonce, dev: false, mode: "enforce", imageSources: ["https://*.tiles.example"] }).reportOnly);
  assert.equal(wide["default-src"], "default-src 'self'");
  assert.equal(wide["script-src"], enforced["script-src"], "both policies trust the same nonce");
  assert.match(wide["img-src"], /https:\/\/\*\.cloudinary\.com https:\/\/\*\.tiles\.example$/);
  assert.equal(wide["media-src"], "media-src 'self' blob:", "SPICE plays from blob: URLs");
  assert.equal(wide["report-uri"], "report-uri /api/csp-report");

  assert.equal(csp.scriptPolicyMode(undefined), "enforce");
  assert.equal(csp.scriptPolicyMode(""), "enforce");
  assert.equal(csp.scriptPolicyMode(" Report-Only "), "report-only");
  assert.equal(csp.scriptPolicyMode("off"), "enforce", "an unknown value never turns enforcement off");

  assert.deepEqual(csp.tileImageSources({ MAP_TILE_URL: "https://{s}.tile.example/{z}/{x}/{y}.png" }), ["https://*.tile.example"]);
  assert.deepEqual(csp.tileImageSources({ MAP_TILE_URL: "https://tiles.example/{z}/{x}/{y}.png", MAP_TILE_URL_DARK: "https://dark.example/{z}/{x}/{y}.png" }),
    ["https://tiles.example", "https://dark.example"]);
  assert.deepEqual(csp.tileImageSources({ MAP_TILE_URL: "http://plain.example/{z}/{x}/{y}.png" }), [], "only https tiles");
  assert.deepEqual(csp.tileImageSources({ MAP_TILE_URL: "not a url" }), []);
  assert.deepEqual(csp.tileImageSources({}), []);
}

// 3. Reports: both formats, hostile input made harmless, the policy (with its nonce) never kept.
{
  const legacy = JSON.stringify({
    "csp-report": {
      "document-uri": "https://dash.example/inventory/instances?token=abc#frag",
      "blocked-uri": "inline",
      "effective-directive": "script-src-elem",
      "original-policy": "script-src 'nonce-SECRETNONCE'",
      disposition: "enforce",
      "source-file": "https://dash.example/_next/static/chunks/app.js?v=1",
      "line-number": 12,
      "column-number": "7",
      "script-sample": "alert(1)\nFORGED LOG LINE\u2028x",
    },
  });
  const [v] = report.parseCspReport("application/csp-report; charset=utf-8", legacy);
  assert.equal(v.document, "https://dash.example/inventory/instances", "query and fragment dropped");
  assert.equal(v.blocked, "inline");
  assert.equal(v.directive, "script-src-elem");
  assert.equal(v.source, "https://dash.example/_next/static/chunks/app.js");
  assert.equal(v.line, 12);
  assert.equal(v.column, 7);
  assert.doesNotMatch(v.sample, /[\n\r\u2028]/, "no control character reaches a log line");
  assert.ok(v.sample.length <= 40);
  assert.doesNotMatch(JSON.stringify(v), /SECRETNONCE|token=abc/, "neither the nonce nor a query value is kept");

  const modern = JSON.stringify([
    { type: "csp-violation", body: { documentURL: "https://dash.example/x?q=1", blockedURL: "https://evil.example/a.js?k=v", effectiveDirective: "script-src-elem", disposition: "report", lineNumber: 3 } },
    { type: "deprecation", body: { message: "ignored" } },
    { type: "csp-violation", body: null },
  ]);
  const parsed = report.parseCspReport("application/reports+json", modern);
  assert.equal(parsed.length, 1);
  assert.equal(parsed[0].blocked, "https://evil.example/a.js");
  assert.equal(parsed[0].document, "https://dash.example/x");

  assert.deepEqual(report.parseCspReport("application/csp-report", "{not json"), []);
  assert.deepEqual(report.parseCspReport("application/json", legacy), [], "only the two report types");
  assert.deepEqual(report.parseCspReport("application/csp-report", "null"), []);
  const flood = JSON.stringify(Array.from({ length: 50 }, () => ({ type: "csp-violation", body: { effectiveDirective: "img-src" } })));
  assert.equal(report.parseCspReport("application/reports+json", flood).length, 10, "one body yields at most ten");
  const huge = report.parseCspReport("application/csp-report", JSON.stringify({ "csp-report": { "effective-directive": "x".repeat(5000), "document-uri": `https://a.example/${"p".repeat(5000)}` } }))[0];
  assert.ok(huge.directive.length <= 64 && huge.document.length <= 512, "every field is capped");
  assert.equal(report.parseCspReport("application/csp-report", JSON.stringify({ "csp-report": { "line-number": -1, "column-number": 1e12 } }))[0].line, null);

  let clock = 0;
  const bucket = report.tokenBucket(3, 1, () => clock);
  assert.deepEqual([bucket.take(), bucket.take(), bucket.take(), bucket.take()], [true, true, true, false]);
  clock += 1000;
  assert.deepEqual([bucket.take(), bucket.take()], [true, false], "refills at its rate, never past its capacity");

  const stream = (bytes) => new Response(new Uint8Array(bytes)).body;
  assert.equal(await report.readCapped(stream(8 * 1024), 8 * 1024), "\u0000".repeat(8 * 1024));
  assert.equal(await report.readCapped(stream(8 * 1024 + 1), 8 * 1024), null, "one byte past the cap is refused");
  assert.equal(await report.readCapped(null), "");

  const ours = (document, headers = {}, configured = "https://dash.example") =>
    report.fromThisDeployment({ ...v, document }, new Headers(headers), configured);
  assert.equal(ours("https://dash.example/inventory"), true, "the configured host");
  assert.equal(ours("http://100.64.0.10:2025/x", { host: "100.64.0.10:2025" }, "http://other.example"), true, "the host it was sent to");
  assert.equal(ours("https://evil.example/x", { host: "dash.example" }), false, "another site's report-uri pointed here");
  assert.equal(ours("", { host: "dash.example" }), false);
  assert.equal(ours("inline", { host: "dash.example" }), false);
}

// 4. Wiring.
{
  const middlewareSource = read("frontend/src/middleware.ts");
  assert.match(middlewareSource, /return withContentSecurityPolicy\(request\);\n\}\);/, "every page response the auth callback passes gets the policy");

  // The middleware itself, in both modes. Next copies the middleware response's headers onto the
  // request it renders and reads the nonce from that request's Content-Security-Policy, so what
  // reaches Next is the response policy when there is one, else the request override.
  class TestNextResponse extends Response {
    static next({ request } = {}) {
      const response = new TestNextResponse(null, { status: 200 });
      response.forwarded = request?.headers;
      return response;
    }
  }
  const middleware = load("frontend/src/middleware.ts", {
    "next-auth": { __esModule: true, default: () => ({ auth: (callback) => callback }) },
    "@/auth": { authConfig: { callbacks: {} } },
    "@/lib/request-security": { inspectRequestTarget: () => null },
    "@/lib/csp": csp,
    "@/lib/session-cookie-echo": { dropUnchangedSessionCookie: async (_q, r) => r, sessionSecret: () => undefined },
    "@/lib/session-cookie": { legacySessionCookieExpiries: () => [] },
    "next/server": { NextResponse: TestNextResponse },
    "@/app/settings/settings-tabs": { isSettingsTab: () => true },
  }).default;
  const nextReads = (response) => response.headers.get("content-security-policy") ?? response.forwarded.get("content-security-policy");
  const page = () => ({ method: "GET", nextUrl: new URL("https://qa.invalid/inventory"), headers: new Headers({ "x-nonce": "client-chosen", "content-security-policy": "script-src *" }) });
  const saved = process.env.CSP_SCRIPT_POLICY;
  try {
    delete process.env.CSP_SCRIPT_POLICY;
    const enforced = await middleware(page());
    const nonce = enforced.forwarded.get("x-nonce");
    assert.notEqual(nonce, "client-chosen", "a client cannot choose the nonce");
    assert.match(enforced.headers.get("content-security-policy"), new RegExp(`'nonce-${nonce.replace(/[+/=]/g, "\\$&")}'`), "enforce: the response policy names the nonce");
    assert.match(nextReads(enforced), /'nonce-/, "enforce: Next finds the nonce");

    process.env.CSP_SCRIPT_POLICY = "report-only";
    const reporting = await middleware(page());
    assert.equal(reporting.headers.get("content-security-policy"), null,
      "report-only: no response policy, or Next copies its nonce-less value onto the request and drops its nonces");
    assert.match(nextReads(reporting), new RegExp(`'nonce-${reporting.forwarded.get("x-nonce").replace(/[+/=]/g, "\\$&")}'`), "report-only: Next still finds the nonce");
    assert.match(reporting.headers.get("content-security-policy-report-only"), /'strict-dynamic'/);
  } finally {
    if (saved === undefined) delete process.env.CSP_SCRIPT_POLICY;
    else process.env.CSP_SCRIPT_POLICY = saved;
  }

  const layout = read("frontend/src/app/layout.tsx");
  assert.match(layout, /const nonce = requestHeaders\.get\("x-nonce"\) \?\? undefined;/);
  const scripts = layout.match(/<script\b[^>]*>/g) ?? [];
  assert.ok(scripts.length >= 2, "the layout's inline scripts");
  for (const tag of scripts) assert.match(tag, /\bnonce=\{nonce\}/, `a layout script without the nonce: ${tag}`);
  const anyScript = /<script\b|from ["']next\/script["']/;
  const src = join(root, "frontend/src");
  const { readdirSync, statSync } = await import("node:fs");
  const files = (function walk(dir) {
    return readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      return statSync(path).isDirectory() ? walk(path) : /\.tsx$/.test(name) ? [path.slice(src.length + 1)] : [];
    });
  })(src);
  const others = files.filter((file) => file !== "app/layout.tsx" && anyScript.test(readFileSync(join(src, file), "utf8")));
  assert.deepEqual(others, [], "a server-rendered script outside the root layout would need the nonce too");

  const config = read("frontend/next.config.js");
  assert.doesNotMatch(config, /Content-Security-Policy-Report-Only/, "middleware owns the report-only policy for pages");
  assert.doesNotMatch(config, /script-src 'self'[^;]*'unsafe/, "no unsafe script source in the static baseline");

  // What middleware does not match gets script-src 'none', page paths keep a baseline without
  // script-src (report-only mode relies on it), and the guest download keeps its sandbox. For one
  // header key the last matching rule wins, as in Next.
  const configModule = frontendRequire(join(root, "frontend/next.config.js"));
  const rules = await (typeof configModule === "function" ? configModule("phase-production-build", {}) : configModule).headers();
  const { getPathMatch } = frontendRequire("next/dist/shared/lib/router/utils/path-match");
  const policyFor = (path) => {
    let value;
    for (const rule of rules) {
      if (!getPathMatch(rule.source, { removeUnnamedParams: true, strict: true })(path)) continue;
      const header = rule.headers.find((h) => h.key.toLowerCase() === "content-security-policy");
      if (header) value = header.value;
    }
    return value ?? "";
  };
  for (const path of ["/api", "/api/x", "/api/v1/unknown", "/api/auth/signout", "/health/x", "/healthz", "/_next/static/chunks/a.js",
    "/_next/image", "/geo/x.json", "/spice-html5/x", "/x.json", "/favicon.ico"]) {
    assert.match(policyFor(path), /script-src 'none'/, `${path}: middleware skips it, so its HTML must run no script`);
  }
  for (const path of ["/", "/inventory", "/settings/integrations", "/auth/signin", "/console/x"]) {
    assert.doesNotMatch(policyFor(path), /script-src/, `${path}: the static baseline must not decide script-src for a page`);
  }
  assert.match(policyFor("/api/v1/instance-files/download"), /^sandbox; default-src 'none'$/, "guest file bytes keep their sandbox");
  // The matcher's exclusions and the config's list name the same paths.
  const matcher = /matcher:\s*\[\s*(?:\/\*[\s\S]*?\*\/\s*)?"([^"]+)"/.exec(middlewareSource)[1];
  for (const excluded of ["api", "health", "_next/static", "_next/image", "geo/", "spice-html5/", "ico|png|jpg|svg|webp|json"]) {
    assert.ok(matcher.includes(excluded), `the middleware matcher no longer excludes ${excluded}; update UNMATCHED_BY_MIDDLEWARE`);
  }

  const route = read("frontend/src/app/api/csp-report/route.ts");
  assert.match(route, /export async function POST/);
  assert.doesNotMatch(route, /export async function (GET|PUT|DELETE|PATCH)/, "reports are POSTed only");
  assert.match(route, /if \(!fromThisDeployment\(violation, request\.headers, configuredUrl\)\) continue;/, "another site's reports cost nothing");
  assert.match(route, /violation\.disposition === "enforce" \? budgets\.enforce : budgets\.report/, "report-only noise cannot spend the enforced budget");
}

console.log("csp nonce: per-response nonce, enforced script-src, report-only wider policy, hostile reports handled");
