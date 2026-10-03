#!/usr/bin/env node
"use strict";

/**
 * Isolated real-role browser QA for the B2 lab.
 *
 * This file has no default side effect. It does not inspect legacy auth state,
 * discover credentials, or write artifacts until a caller supplies both
 * --execute and an exact private credentials file. Credentials, JWTs, response
 * bodies, browser storage, and the private resource ledger are never printed.
 */

const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = "http://127.0.0.1:32025";
const BACKEND = "http://127.0.0.1:32405";
const AUTH_ORIGIN = "http://auth.orcastra-qa.invalid:32090";
const ISSUER = `${AUTH_ORIGIN}/application/o/orcastra-qa-hardening/`;
const AUDIENCE = "orcastra-qa-hardening";
const MAX_CREDENTIAL_BYTES = 16 * 1024;
const ACTORS = Object.freeze({
  admin: { email: "admin@orcastra-qa.invalid", group: "role_admin" },
  "partner-a": { email: "partner-a@orcastra-qa.invalid", group: "role_partner" },
  "partner-b": { email: "partner-b@orcastra-qa.invalid", group: "role_partner" },
  "tenant-a1": { email: "tenant-a1@orcastra-qa.invalid", group: "role_tenant" },
  "tenant-a2": { email: "tenant-a2@orcastra-qa.invalid", group: "role_tenant" },
  "tenant-b1": { email: "tenant-b1@orcastra-qa.invalid", group: "role_tenant" },
});
const CASES = Object.freeze([
  ["auth.admin", "admin", "fresh UI login yields the exact Admin JWT and backend accepts it"],
  ["auth.partner-a", "partner-a", "fresh UI login yields the exact Partner JWT and backend accepts it"],
  ["auth.partner-b", "partner-b", "fresh UI login yields the exact Partner JWT and backend accepts it"],
  ["auth.tenant-a1", "tenant-a1", "fresh UI login yields the exact Tenant JWT and backend accepts it"],
  ["auth.tenant-a2", "tenant-a2", "fresh UI login yields the exact Tenant JWT and backend accepts it"],
  ["auth.tenant-b1", "tenant-b1", "fresh UI login yields the exact Tenant JWT and backend accepts it"],
  ["auth.invalid", "admin", "missing and invalid bearer requests return 401"],
  ["organization.setup", "partner-a", "two isolated organizations and membership rows persist"],
  ["organization.neighbor-denial", "tenant-a1", "neighbor organization preference read/write is denied"],
  ["organization.branding", "partner-a", "Partner's first branding save persists and appears in Tenant A without a reload"],
  ["organization.revoke", "partner-a", "removing Tenant A2 revokes direct organization access"],
  ["settings.urls", "partner-a", "tab navigation, reload, history, General alias, and unknown 404 are correct"],
  ["settings.tenant-denial", "tenant-a1", "privileged settings direct URL returns to General before privileged fetches"],
  ["settings.personal-theme-cache", "tenant-a1", "personal theme and auth persist while browser reads refresh"],
  ["auth.logout", "tenant-b1", "real browser logout removes the session and backend rejects requests without a bearer"],
]);
const SANITIZED_PATHS = new Set([
  "/api/v1/system/settings", "/api/v1/organizations/", "/api/v1/organizations/:neighbor/preferences",
  "/api/v1/organizations/:owned/preferences", "/api/v1/organizations/:owned/members/:synthetic",
  "/api/v1/user-preferences",
  "/settings", "/settings/integrations",
]);

function fail(message) { throw new Error(message); }
function exactObject(value, keys, name) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join("|") !== [...keys].sort().join("|")) fail(`${name} has an unexpected shape`);
  return value;
}
function assertPrivateFileStat(stat, limit = MAX_CREDENTIAL_BYTES) {
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size < 2 || stat.size > limit) fail("credentials must be a bounded regular single-link file");
  if ((stat.mode & 0o777) !== 0o600 || stat.uid !== process.getuid() || stat.gid !== process.getgid()) fail("credentials must be owned by this process and mode 0600");
  return stat;
}
function noSymlinkAncestors(file) {
  let current = path.dirname(path.resolve(file));
  while (true) {
    const stat = fs.lstatSync(current);
    if (!stat.isDirectory() || stat.isSymbolicLink()) fail("credentials path has an unsafe ancestor");
    const parent = path.dirname(current);
    if (parent === current) return;
    current = parent;
  }
}
function sameFileStat(left, right) { return left.dev === right.dev && left.ino === right.ino && left.size === right.size && left.mtimeMs === right.mtimeMs && left.nlink === right.nlink && left.mode === right.mode && left.uid === right.uid && left.gid === right.gid; }
function privateRegular(file, limit = MAX_CREDENTIAL_BYTES) {
  noSymlinkAncestors(file);
  const stat = fs.lstatSync(file);
  assertPrivateFileStat(stat, limit);
  return stat;
}
function privateDirectory(directory) {
  const stat = fs.lstatSync(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o777) !== 0o700 || stat.uid !== process.getuid() || stat.gid !== process.getgid()) fail("artifacts must be an owned mode 0700 directory");
  return stat;
}
function parseCredentialsBytes(bytes) {
  if (!Buffer.isBuffer(bytes) || bytes.length < 2 || bytes.length > MAX_CREDENTIAL_BYTES) fail("credential input is outside its byte bound");
  let document;
  try { document = JSON.parse(bytes.toString("utf8")); } catch { fail("credentials are not valid JSON"); }
  exactObject(document, ["schema_version", "actors"], "credentials");
  if (document.schema_version !== 1) fail("unsupported credential schema");
  exactObject(document.actors, Object.keys(ACTORS), "credential actors");
  const passwords = {};
  for (const alias of Object.keys(ACTORS)) {
    const row = exactObject(document.actors[alias], ["password"], `credential actor ${alias}`);
    if (typeof row.password !== "string" || row.password.length < 12 || row.password.length > 512 || /[\r\n\0]/.test(row.password)) fail(`invalid password for ${alias}`);
    passwords[alias] = row.password;
  }
  return passwords;
}
function readCredentials(file) {
  const absolute = path.resolve(file);
  noSymlinkAncestors(absolute);
  const fd = fs.openSync(absolute, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const before = fs.fstatSync(fd); assertPrivateFileStat(before);
    const bytes = fs.readFileSync(fd); const after = fs.fstatSync(fd);
    if (!sameFileStat(before, after)) fail("credentials changed while being read");
    return parseCredentialsBytes(bytes);
  } finally { fs.closeSync(fd); }
}
function assertClaims(token, actor, now = Date.now() / 1000) {
  if (typeof token !== "string" || token.split(".").length !== 3) fail("session does not contain a JWT");
  let claims;
  try { claims = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString("utf8")); } catch { fail("JWT payload is malformed"); }
  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud], expected = ACTORS[actor];
  const roleGroups = Array.isArray(claims.groups) ? claims.groups.filter((group) => /^role_/.test(group)) : [];
  if (claims.iss !== ISSUER || !audiences.includes(AUDIENCE) || claims.email !== expected.email || roleGroups.length !== 1 || roleGroups[0] !== expected.group || !Number.isFinite(claims.exp) || claims.exp <= now + 15) fail(`unexpected private JWT claims for ${actor}`);
  return true;
}
function sanitizeRecord(row) {
  exactObject(row, ["case_id", "method", "path", "status", "assertion"], "evidence row");
  if (!CASES.some(([id]) => id === row.case_id) || !/^(GET|POST|PUT|PATCH|DELETE|UI)$/.test(row.method) || !SANITIZED_PATHS.has(row.path) || !Number.isInteger(row.status) || row.status < 0 || row.status > 599 || typeof row.assertion !== "boolean") fail("unsafe evidence row");
  return { case_id: row.case_id, method: row.method, path: row.path, status: row.status, assertion: row.assertion };
}
function plan() { return CASES.map(([case_id, role, expected_assertion]) => ({ case_id, role, fixture: "fresh B2 synthetic lab", expected_assertion, result: "pending" })); }
function writeJsonNew(file, value) {
  const fd = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
  try { fs.writeFileSync(fd, `${JSON.stringify(value)}\n`); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}
function openPrivateLedger(file) {
  const fd = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
  fs.writeFileSync(fd, '{"event":"ledger_started"}\n'); fs.fsyncSync(fd);
  return fd;
}
function appendPrivateLedger(fd, value) { fs.writeFileSync(fd, `${JSON.stringify(value)}\n`); fs.fsyncSync(fd); }
function requireExecution(args) {
  if (!args.execute || !args.credentials || !args.artifacts || args.frontend !== FRONTEND || args.backend !== BACKEND || args.auth !== AUTH_ORIGIN) fail("refusing execution without exact lab URLs, --execute, --credentials, and --artifacts");
}
function failureNotice(caseId) {
  const safeCase = caseId === "startup" || CASES.some(([id]) => id === caseId) ? caseId : "startup";
  return `real role harness stopped: execution_failed case=${safeCase}\n`;
}
function parseArgs(argv) {
  const result = { frontend: FRONTEND, backend: BACKEND, auth: AUTH_ORIGIN, execute: false };
  for (let i = 2; i < argv.length; i += 1) {
    const item = argv[i];
    if (item === "--execute") { result.execute = true; continue; }
    if (!["--credentials", "--artifacts", "--frontend", "--backend", "--auth"].includes(item) || !argv[i + 1]) fail("invalid harness arguments");
    result[item.slice(2)] = argv[++i];
  }
  return result;
}
async function api(page, token, method, endpoint, body) {
  return page.evaluate(async ({ token: bearer, method: verb, endpoint: target, body: requestBody }) => {
    const response = await fetch(target, { method: verb, credentials: "include", headers: bearer ? { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json" } : {}, body: requestBody ? JSON.stringify(requestBody) : undefined, cache: "no-store" });
    const text = await response.text(); let json = null;
    try { json = text ? JSON.parse(text) : null; } catch { /* response bodies remain private */ }
    return { status: response.status, json };
  }, { token, method, endpoint, body });
}
async function login(browser, actor, passwords) {
  const context = await browser.newContext({ baseURL: FRONTEND, viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  await page.goto(`/auth/signin?callbackUrl=${encodeURIComponent("/settings")}`, { waitUntil: "domcontentloaded" });
  await page.waitForURL((url) => url.origin === AUTH_ORIGIN, { timeout: 12_000 }).catch(async () => {
    await page.getByRole("button", { name: /sign in with sso/i }).click({ timeout: 6_000 }).catch(() => {});
    await page.waitForURL((url) => url.origin === AUTH_ORIGIN, { timeout: 30_000 });
  });
  const deadline = Date.now() + 150_000;
  while (Date.now() < deadline) {
    const url = new URL(page.url());
    if (url.origin === FRONTEND && !url.pathname.startsWith("/auth/signin")) break;
    await page.waitForLoadState("domcontentloaded").catch(() => {});
    const uid = page.locator('input[name="uidField"]').first();
    const password = page.locator('input[name="password"]').first();
    if (await uid.isVisible().catch(() => false)) {
      await uid.fill(ACTORS[actor].email).catch(() => {});
      await page.keyboard.press("Enter").catch(() => {});
      await page.waitForTimeout(1500);
      continue;
    }
    if (await password.isVisible().catch(() => false)) {
      await password.fill(passwords[actor]).catch(() => {});
      await page.keyboard.press("Enter").catch(() => {});
      await page.waitForTimeout(1800);
      continue;
    }
    const consent = page.locator('button[type="submit"], button:has-text("Continue"), button:has-text("Authorize")').first();
    if (await consent.isVisible().catch(() => false)) {
      await consent.click().catch(() => {});
      await page.waitForTimeout(1500);
      continue;
    }
    await page.waitForTimeout(1000);
  }
  await page.waitForURL((url) => url.origin === FRONTEND && !url.pathname.startsWith("/auth/signin"), { timeout: 30_000 });
  await page.waitForFunction(async () => {
    try { const response = await fetch("/api/auth/session", { credentials: "include", cache: "no-store" }); return Boolean((await response.json())?.user); } catch { return false; }
  }, { timeout: 25_000 });
  const session = await page.evaluate(async () => (await fetch("/api/auth/session", { credentials: "include", cache: "no-store" })).json());
  if (!session?.accessToken || session?.user?.email !== ACTORS[actor].email) fail(`UI session does not match ${actor}`);
  assertClaims(session.accessToken, actor);
  return { context, page, token: session.accessToken };
}
async function requireStatus(page, token, method, endpoint, expected, body) {
  const response = await api(page, token, method, endpoint, body);
  if (response.status !== expected) fail(`${method} ${endpoint} returned ${response.status}, expected ${expected}`);
  return response.json;
}
function orgPayload(label) { return { name: `orcastra-qa-hardening-${label}`, description: "Synthetic isolated role harness" }; }
function organizationId(body) { if (!body || !Number.isInteger(body.id)) fail("organization creation did not return a numeric private id"); return body.id; }
async function settingsChecks(page, actor, evidence) {
  await page.goto("/settings", { waitUntil: "networkidle" });
  if (actor === "tenant-a1") {
    const requests = []; page.on("request", (request) => { if (request.url().includes("/api/v1/")) requests.push(new URL(request.url()).pathname); });
    await page.goto("/settings/integrations", { waitUntil: "networkidle" });
    if (!page.url().endsWith("/settings") || requests.some((item) => /\/(integrations\/api-keys|user-groups|system\/resource-usage)$/.test(item))) fail("tenant privileged settings fetched data before denial");
    evidence.push({ case_id: "settings.tenant-denial", method: "UI", path: "/settings/integrations", status: 200, assertion: true });
  } else {
    for (const [title, slug] of [["Integrations", "integrations"], ["User Groups", "user-groups"]]) {
      await page.getByRole("tab", { name: title, exact: true }).click();
      if (!page.url().endsWith(`/settings/${slug}`)) fail(`settings tab ${slug} did not own its URL`);
      await page.reload({ waitUntil: "networkidle" });
      if (await page.getByRole("tab", { name: title, exact: true }).getAttribute("data-state") !== "active") fail(`settings reload did not retain ${slug}`);
    }
    await page.goBack({ waitUntil: "networkidle" });
    if (!page.url().endsWith("/settings/integrations") || await page.getByRole("tab", { name: "Integrations", exact: true }).getAttribute("data-state") !== "active") fail("settings Back did not restore Integrations");
    await page.goForward({ waitUntil: "networkidle" });
    if (!page.url().endsWith("/settings/user-groups") || await page.getByRole("tab", { name: "User Groups", exact: true }).getAttribute("data-state") !== "active") fail("settings Forward did not restore User Groups");
    for (const [title, slug] of [["Personalization", "personalization"], ["Dashboard Host", "dashboard-host"]]) {
      await page.getByRole("tab", { name: title, exact: true }).click();
      if (!page.url().endsWith(`/settings/${slug}`)) fail(`settings tab ${slug} did not own its URL`);
      await page.reload({ waitUntil: "networkidle" });
      if (await page.getByRole("tab", { name: title, exact: true }).getAttribute("data-state") !== "active") fail(`settings reload did not retain ${slug}`);
    }
    await page.goto("/settings/general", { waitUntil: "networkidle" });
    if (!page.url().endsWith("/settings")) fail("general settings alias did not canonicalize");
    const response = await page.goto("/settings/not-a-real-tab", { waitUntil: "domcontentloaded" });
    if (!response || response.status() !== 404) fail("unknown settings tab did not return 404");
    evidence.push({ case_id: "settings.urls", method: "UI", path: "/settings", status: 200, assertion: true });
  }
}
async function cacheThemeChecks(page, actor, evidence) {
  await page.goto("/settings", { waitUntil: "networkidle" });
  const session = await page.evaluate(async () => (await fetch("/api/auth/session", { credentials: "include", cache: "no-store" })).json());
  if (!session?.accessToken) fail("personal preferences require the real session token");
  const personal = await requireStatus(page, session.accessToken, "GET", "/api/v1/user-preferences", 200);
  const nextTheme = personal?.theme === "dark" ? "light" : "dark";
  const updated = await requireStatus(page, session.accessToken, "PUT", "/api/v1/user-preferences", 200, { ...personal, theme: nextTheme });
  if (updated?.theme !== nextTheme || !updated?.lastModified) fail("personal theme did not persist before cache clear");
  const readBack = await requireStatus(page, session.accessToken, "GET", "/api/v1/user-preferences", 200);
  if (readBack?.theme !== nextTheme) fail("personal theme readback differs from saved value");
  await page.reload({ waitUntil: "networkidle" });
  const htmlClasses = await page.locator("html").getAttribute("class");
  if (!htmlClasses?.split(/\s+/).includes(nextTheme)) fail("theme was not applied after reload");
  const before = await page.evaluate(async () => (await fetch("/api/auth/session", { credentials: "include", cache: "no-store" })).json());
  let scopedReads = 0;
  const scopedResponse = (response) => { if (response.url().includes("/api/v1/system/settings")) scopedReads += 1; };
  page.on("response", scopedResponse);
  const readsBefore = scopedReads;
  await page.getByRole("button", { name: "Clear Browser Cache", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "Browser cache cleared." }).waitFor();
  const after = await page.evaluate(async () => (await fetch("/api/auth/session", { credentials: "include", cache: "no-store" })).json());
  page.off("response", scopedResponse);
  if (before.user?.email !== ACTORS[actor].email || after.user?.email !== before.user?.email || scopedReads <= readsBefore) fail("browser cache clear did not preserve auth or refresh scoped reads");
  if (!await page.getByText("Orcastra QA A footer", { exact: true }).isVisible()) fail("personal theme or cache clear removed organization branding");
  evidence.push({ case_id: "settings.personal-theme-cache", method: "PUT", path: "/api/v1/user-preferences", status: 200, assertion: true });
}
async function logoutChecks(page, evidence) {
  await page.goto("/settings", { waitUntil: "networkidle" });
  const accountButton = page.locator("button:has(.rounded-full)").last();
  if (!await accountButton.isVisible().catch(() => false)) fail("account menu trigger is unavailable");
  await accountButton.click();
  await page.getByText("Sign out", { exact: true }).click();
  await page.waitForFunction(async () => {
    try { const response = await fetch("/api/auth/session", { credentials: "include", cache: "no-store" }); return !(await response.json())?.user; } catch { return false; }
  }, { timeout: 30_000 });
  await requireStatus(page, null, "GET", "/api/v1/system/settings", 401);
  evidence.push({ case_id: "auth.logout", method: "GET", path: "/api/v1/system/settings", status: 401, assertion: true });
}
async function execute(args, state) {
  requireExecution(args);
  const passwords = readCredentials(path.resolve(args.credentials));
  const artifactRoot = path.resolve(args.artifacts);
  if (fs.existsSync(artifactRoot)) fail("artifact directory must be absent; no adoption or overwrite");
  fs.mkdirSync(artifactRoot, { mode: 0o700 });
  fs.chmodSync(artifactRoot, 0o700);
  privateDirectory(artifactRoot);
  const privateLedger = path.join(artifactRoot, "private-ledger.jsonl"), ledgerFd = openPrivateLedger(privateLedger), sanitized = [];
  const sessions = {};
  let browser;
  try {
    const { chromium } = require("@playwright/test");
    browser = await chromium.launch({ headless: true });
    for (const actor of Object.keys(ACTORS)) {
      state.caseId = `auth.${actor}`;
      sessions[actor] = await login(browser, actor, passwords);
      await requireStatus(sessions[actor].page, sessions[actor].token, "GET", "/api/v1/system/settings", 200);
      sanitized.push({ case_id: `auth.${actor}`, method: "GET", path: "/api/v1/system/settings", status: 200, assertion: true });
    }
    state.caseId = "auth.invalid";
    await requireStatus(sessions.admin.page, null, "GET", "/api/v1/system/settings", 401);
    await requireStatus(sessions.admin.page, "invalid", "GET", "/api/v1/system/settings", 401);
    sanitized.push({ case_id: "auth.invalid", method: "GET", path: "/api/v1/system/settings", status: 401, assertion: true });
    state.caseId = "organization.setup";
    const a = organizationId(await requireStatus(sessions["partner-a"].page, sessions["partner-a"].token, "POST", "/api/v1/organizations/", 201, orgPayload("a")));
    appendPrivateLedger(ledgerFd, { event: "organization_created", organization_id: a });
    const b = organizationId(await requireStatus(sessions["partner-b"].page, sessions["partner-b"].token, "POST", "/api/v1/organizations/", 201, orgPayload("b")));
    appendPrivateLedger(ledgerFd, { event: "organization_created", organization_id: b });
    for (const [org, actor] of [[a, "tenant-a1"], [a, "tenant-a2"], [b, "tenant-b1"]]) {
      const owner = org === a ? "partner-a" : "partner-b";
      const member = await requireStatus(sessions[owner].page, sessions[owner].token, "POST", `/api/v1/organizations/${org}/members`, 201, { user_email: ACTORS[actor].email });
      if (!Number.isInteger(member?.id) || member?.organization_id !== org || member?.user_email !== ACTORS[actor].email) fail("membership creation did not return the accepted private identity");
      appendPrivateLedger(ledgerFd, { event: "member_created", member_id: member.id, organization_id: org });
    }
    const members = await requireStatus(sessions["partner-a"].page, sessions["partner-a"].token, "GET", `/api/v1/organizations/${a}/members`, 200);
    if (!Array.isArray(members) || !["tenant-a1", "tenant-a2"].every((actor) => members.some((member) => member?.user_email === ACTORS[actor].email))) fail("membership persistence readback failed");
    sanitized.push({ case_id: "organization.setup", method: "POST", path: "/api/v1/organizations/", status: 201, assertion: true });
    state.caseId = "organization.neighbor-denial";
    await requireStatus(sessions["tenant-a1"].page, sessions["tenant-a1"].token, "GET", `/api/v1/organizations/${b}/preferences`, 403);
    await requireStatus(sessions["tenant-a1"].page, sessions["tenant-a1"].token, "PUT", `/api/v1/organizations/${b}/preferences`, 403, { settings: {} });
    sanitized.push({ case_id: "organization.neighbor-denial", method: "GET", path: "/api/v1/organizations/:neighbor/preferences", status: 403, assertion: true });
    state.caseId = "organization.branding";
    const initialVersion = await requireStatus(sessions["tenant-a1"].page, sessions["tenant-a1"].token, "GET", "/api/v1/organizations/my-preferences/version", 200);
    if (initialVersion?.organization_id !== a || initialVersion?.updated_at !== null) fail("new organization did not begin with a null branding version");
    await sessions["tenant-a1"].page.goto("/settings", { waitUntil: "networkidle" });
    await sessions["tenant-a1"].page.bringToFront();
    const branded = await requireStatus(sessions["partner-a"].page, sessions["partner-a"].token, "PUT", `/api/v1/organizations/${a}/preferences`, 200, { settings: { branding: { appName: "Orcastra QA A", footerText: "Orcastra QA A footer" }, theme: "light" } });
    const version = await requireStatus(sessions["tenant-a1"].page, sessions["tenant-a1"].token, "GET", "/api/v1/organizations/my-preferences/version", 200);
    const inherited = await requireStatus(sessions["tenant-a1"].page, sessions["tenant-a1"].token, "GET", "/api/v1/organizations/my-preferences", 200);
    if (branded?.organization_id !== a || !branded?.updated_at || version?.organization_id !== a || version?.updated_at !== branded.updated_at || inherited?.organization_id !== a || inherited?.settings?.branding?.footerText !== "Orcastra QA A footer") fail("branding/version inheritance did not persist in Tenant A scope");
    await sessions["tenant-a1"].page.getByText("Orcastra QA A footer", { exact: true }).waitFor({ state: "visible", timeout: 65_000 });
    sanitized.push({ case_id: "organization.branding", method: "PUT", path: "/api/v1/organizations/:owned/preferences", status: 200, assertion: true });
    state.caseId = "organization.revoke";
    await requireStatus(sessions["partner-a"].page, sessions["partner-a"].token, "DELETE", `/api/v1/organizations/${a}/members/${encodeURIComponent(ACTORS["tenant-a2"].email)}`, 204);
    await requireStatus(sessions["tenant-a2"].page, sessions["tenant-a2"].token, "GET", `/api/v1/organizations/${a}/preferences`, 403);
    sanitized.push({ case_id: "organization.revoke", method: "DELETE", path: "/api/v1/organizations/:owned/members/:synthetic", status: 204, assertion: true });
    state.caseId = "settings.urls";
    await settingsChecks(sessions["partner-a"].page, "partner-a", sanitized);
    state.caseId = "settings.tenant-denial";
    await settingsChecks(sessions["tenant-a1"].page, "tenant-a1", sanitized);
    state.caseId = "settings.personal-theme-cache";
    await cacheThemeChecks(sessions["tenant-a1"].page, "tenant-a1", sanitized);
    state.caseId = "auth.logout";
    await logoutChecks(sessions["tenant-b1"].page, sanitized);
    writeJsonNew(path.join(artifactRoot, "sanitized-results.json"), { schema_version: 1, lab: "orcastra-qa-hardening", results: sanitized.map(sanitizeRecord) });
  } finally {
    await Promise.all(Object.values(sessions).map(async ({ context }) => context.close()));
    if (browser) await browser.close();
    fs.closeSync(ledgerFd);
  }
}

if (require.main === module) {
  const args = parseArgs(process.argv);
  if (!args.execute) process.stdout.write(`${JSON.stringify({ schema_version: 1, execution: "refused-by-default", cases: plan() })}\n`);
  else {
    const state = { caseId: "startup" };
    execute(args, state).catch(() => { process.stderr.write(failureNotice(state.caseId)); process.exitCode = 1; });
  }
}
module.exports = { ACTORS, AUDIENCE, AUTH_ORIGIN, BACKEND, CASES, FRONTEND, ISSUER, MAX_CREDENTIAL_BYTES, assertClaims, failureNotice, parseArgs, parseCredentialsBytes, plan, privateDirectory, privateRegular, readCredentials, requireExecution, sanitizeRecord };
