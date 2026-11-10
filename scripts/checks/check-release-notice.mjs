#!/usr/bin/env node
// The release notice tells an Administrator when the running version is out of date, from the
// backend's last read of the public release manifest (services/release_check/). This pins the
// parts a refactor could break: only Administrators ask, the notice sits in the shell below
// the Vault notice, a critical verdict can never be dismissed, links open in a new tab without
// an opener, and the hermetic browser fixtures answer the read.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const read = (path) => readFileSync(join(root, path), "utf8");

const hook = read("frontend/src/hooks/use-release-status.ts");
assert.match(hook, /enabled: isAuthenticated && !isLoading && isAdmin,/, "only an Administrator may ask for the release status");
assert.match(hook, /useState\(\(\) => jitter\(POLL_MS\)\)/, "the poll interval is jittered once per hook instance");

const layout = read("frontend/src/components/main-layout.tsx");
const vault = layout.indexOf("<VaultNotice />");
const release = layout.indexOf("<ReleaseNotice />");
assert.ok(vault > 0 && release > vault, "the release notice sits in the shell, below the Vault notice");

const dismissal = read("frontend/src/lib/release-dismissal.ts");
assert.match(dismissal, /return status\.level === "info" \|\| status\.level === "warning";/,
  "a critical release notice (end of life, a missing security fix) must never be dismissable");
assert.match(dismissal, /safeGetItem\(/, "dismissals read storage through the safe helpers");
assert.match(dismissal, /`\$\{KEY_PREFIX\}:\$\{scopeKey\}`/, "a dismissal is kept per account, never shared across sign-ins");

const notice = read("frontend/src/components/release/release-notice.tsx");
for (const link of notice.match(/<a [^>]*>/g) ?? []) {
  assert.match(link, /target="_blank"/, "release links open the documentation in a new tab");
  assert.match(link, /rel="noopener noreferrer"/, "a link opened in a new tab carries rel=noopener noreferrer");
}
assert.match(notice, /role="status"/, "the notice announces itself politely");

const copy = read("frontend/src/components/release/release-copy.ts");
assert.ok(!/[—]/.test(copy), "no em dashes in release copy");

assert.match(read("frontend/e2e/support/fixtures.cjs"), /path === "\/system\/release-status"/,
  "the hermetic browser fixtures answer the Administrator's release read");

console.log("release notice: Administrators only, in the shell, critical is never dismissed, links leave safely");
