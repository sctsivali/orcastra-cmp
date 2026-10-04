// Real-browser SSO test of https://logs.orcastra.io through Authentik.
//
// Creates temporary Authentik users (admin, viewer) with passwords, signs in with Chromium the way a
// person does (login page -> "Log in with Orcastra SSO" -> Authentik -> callback), then checks the
// session, role-based access through Dashboards, the break-glass form, and logout. Users are
// deleted in a finally block.
//
//   AUTHENTIK_TOKEN=... NODE_PATH=<dir with node_modules/playwright> node tests/browser_sso.mjs
// Screenshots go to $SHOT_DIR (default ./sso-shots).
import { chromium } from 'playwright';
import { randomBytes } from 'node:crypto';
import { mkdirSync } from 'node:fs';

const AK = process.env.AUTHENTIK_URL ?? 'https://sso.orcastra.io';
const OSD = process.env.OSD_URL ?? 'https://logs.orcastra.io';
const SHOTS = process.env.SHOT_DIR ?? './sso-shots';
const run = randomBytes(3).toString('hex');
mkdirSync(SHOTS, { recursive: true });

const results = [];
const check = (ok, name) => { results.push([ok, name]); console.log(`  ${ok ? 'PASS' : 'FAIL'} ${name}`); };

async function ak(method, path, body) {
  const r = await fetch(`${AK}/api/v3${path}`, {
    method,
    headers: { Authorization: `Bearer ${process.env.AUTHENTIK_TOKEN}`, 'Content-Type': 'application/json', 'User-Agent': 'orcastra-logging-qa/1.0' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status >= 300) throw new Error(`authentik ${method} ${path}: ${r.status} ${await r.text()}`);
  return r.status === 204 ? null : r.json();
}

async function signIn(browser, username, password, label) {
  const ctx = await browser.newContext({ ignoreHTTPSErrors: false });
  const page = await ctx.newPage();
  await page.goto(`${OSD}/app/home`);
  await page.waitForURL(/\/app\/login/);
  // The SSO entry is an <a> without href (no link role), so match by text, not by role.
  const sso = page.locator('button, a', { hasText: /Log in with Orcastra SSO/i }).first();
  // The first load pulls the Dashboards bundles through Cloudflare; it can take 20+ seconds.
  await sso.waitFor({ state: 'visible', timeout: 90000 }).catch(() => {});
  check(await sso.isVisible(), `${label}: login page offers "Log in with Orcastra SSO"`);
  check(await page.getByText(/Break-glass login/i).isVisible(), `${label}: break-glass form is present`);
  await sso.click();
  await page.waitForURL(new RegExp(`^${AK.replace(/\./g, '\\.')}/`), { timeout: 30000 });
  // Authentik flow executor: identification, then password (custom elements, pierced by Playwright).
  await page.locator('input[name="uidField"]').fill(username);
  await page.locator('button[type="submit"]').first().click();
  await page.locator('input[name="password"]').fill(password);
  await page.locator('button[type="submit"]').first().click();
  try {
    await page.waitForURL(new RegExp(`^${OSD.replace(/\./g, '\\.')}/app/`), { timeout: 45000 });
  } catch (e) {
    await page.screenshot({ path: `${SHOTS}/${label}-stuck.png` });
    console.log(`    stuck at ${page.url()}`);
    console.log(`    page text: ${(await page.locator('body').innerText().catch(() => '')).replace(/\s+/g, ' ').slice(0, 300)}`);
    throw e;
  }
  // First sign-in may show the tenant picker; keep the default (Global) if it appears.
  const confirm = page.getByRole('button', { name: /^Confirm$/ });
  if (await confirm.isVisible({ timeout: 8000 }).catch(() => false)) await confirm.click();
  await page.waitForLoadState('networkidle');
  await page.screenshot({ path: `${SHOTS}/${label}-landing.png`, fullPage: false });
  return { ctx, page };
}

async function osd(page, method, path, data) {
  const r = await page.request.fetch(`${OSD}${path}`, {
    method, headers: { 'osd-xsrf': 'true', 'content-type': 'application/json' }, data,
  });
  let body = null; try { body = await r.json(); } catch { /* not json */ }
  return { status: r.status(), body };
}

// Console proxy = the Dev Tools path, executed with the signed-in user's own identity.
const proxy = (page, method, path) => osd(page, 'POST', `/api/console/proxy?path=${encodeURIComponent(path)}&method=${method}`);

const created = [];
const browser = await chromium.launch();
try {
  console.log('== setup');
  const groups = Object.fromEntries((await ak('GET', '/core/groups/?search=opensearch-&page_size=50')).results.map((g) => [g.name, g.pk]));
  const users = {};
  for (const [role, group] of [['admin', 'opensearch-admins'], ['viewer', 'opensearch-viewers']]) {
    const username = `qa-os-web-${role}-${run}`;
    const u = await ak('POST', '/core/users/', { username, name: `QA OpenSearch web ${role}`, is_active: true, path: 'users/qa' });
    created.push(u.pk);
    const password = randomBytes(18).toString('base64url');
    await ak('POST', `/core/users/${u.pk}/set_password/`, { password });
    await ak('POST', `/core/groups/${groups[group]}/add_user/`, { pk: u.pk });
    users[role] = { username, password };
  }

  console.log('== viewer');
  {
    const { ctx, page } = await signIn(browser, users.viewer.username, users.viewer.password, 'viewer');
    check(page.url().startsWith(`${OSD}/app/`), `viewer lands in Dashboards (${new URL(page.url()).pathname})`);
    const acct = await osd(page, 'GET', '/api/v1/configuration/account');
    const roles = acct.body?.data?.roles ?? [];
    check(acct.body?.data?.user_name === users.viewer.username, `viewer session user is ${acct.body?.data?.user_name}`);
    check(roles.includes('readall') && roles.includes('logs_readonly_ui') && !roles.includes('all_access'), `viewer roles ${JSON.stringify(roles)}`);
    const cookies = await ctx.cookies(OSD);
    const sess = cookies.find((c) => c.name === 'security_authentication');
    check(!!sess && sess.secure && sess.httpOnly && sess.sameSite === 'Lax', 'session cookie is Secure, HttpOnly, SameSite=Lax');
    const count = await proxy(page, 'GET', 'orcastra-app-*/_count');
    check(count.status === 200 && count.body?.count > 0, `viewer can query Orcastra logs (${count.body?.count} docs)`);
    const del = await proxy(page, 'DELETE', 'orcastra-app-2026.03');
    check(del.status === 403 || del.body?.error?.type === 'security_exception', `viewer cannot delete an index (HTTP ${del.status})`);
    const sec = await proxy(page, 'GET', '_plugins/_security/api/internalusers');
    check(sec.status === 403 || sec.body?.status === 403 || sec.body?.error, 'viewer cannot read the security API');
    // Logout: Dashboards ends the local session and sends the browser to Authentik's end-session.
    const lo = await osd(page, 'POST', '/auth/logout');
    check(lo.status === 200 && String(lo.body?.redirectURL ?? '').startsWith(`${AK}/application/o/opensearch-dashboards/end-session/`), 'logout redirects to Authentik end-session');
    const after = await osd(page, 'GET', '/api/v1/configuration/account');
    check(after.status === 401, `after logout the Dashboards session is gone (HTTP ${after.status})`);
    await ctx.close();
  }

  console.log('== admin');
  {
    const { ctx, page } = await signIn(browser, users.admin.username, users.admin.password, 'admin');
    const acct = await osd(page, 'GET', '/api/v1/configuration/account');
    check((acct.body?.data?.roles ?? []).includes('all_access'), `admin roles ${JSON.stringify(acct.body?.data?.roles)}`);
    const health = await proxy(page, 'GET', '_cluster/health');
    check(health.status === 200 && health.body?.status === 'green', `admin sees cluster health (${health.body?.status})`);
    const sec = await proxy(page, 'GET', '_plugins/_security/api/rolesmapping/all_access');
    check(sec.status === 200, 'admin can read the security API');
    const patterns = await osd(page, 'GET', '/api/saved_objects/_find?type=index-pattern&per_page=100');
    const titles = (patterns.body?.saved_objects ?? []).map((o) => o.attributes.title).sort();
    console.log(`    index patterns visible: ${JSON.stringify(titles)}`);
    await page.goto(`${OSD}/app/data-explorer/discover`);
    await page.waitForLoadState('networkidle');
    await page.screenshot({ path: `${SHOTS}/admin-discover.png` });
    await ctx.close();
  }

  console.log('== unauthenticated');
  {
    const ctx = await browser.newContext(); const page = await ctx.newPage();
    const r = await page.request.get(`${OSD}/api/v1/configuration/account`);
    check(r.status() === 401, `no session -> 401 (HTTP ${r.status()})`);
    await ctx.close();
  }
} finally {
  await browser.close();
  console.log('== cleanup');
  for (const pk of created) await ak('DELETE', `/core/users/${pk}/`).catch((e) => console.log(`  cleanup failed ${pk}: ${e.message}`));
  console.log(`  removed ${created.length} users`);
}

const failed = results.filter(([ok]) => !ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
