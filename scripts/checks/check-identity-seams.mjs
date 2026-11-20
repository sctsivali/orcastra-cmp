#!/usr/bin/env node
// Structural guard: sign-in and identity reads go through the edition seams.
//
// Another edition of the product (app/editions/ in the backend, lib/edition*.ts and
// components/edition-*.tsx in the frontend) swaps how tokens are verified, where roles are read
// and written, and how the browser signs in. It can only do that where Full asks the seam.
// Code that reaches the identity provider directly works in Full and silently keeps Full's
// rules in the other edition: an Authentik call on a server with no Authentik, or a sign-in
// button that names a provider the build does not have.
//
// Allowed: the modules that ARE the Full implementation behind each seam. Anything else must
// call the seam: token_verification_configured / verify_access_token (core/auth.py),
// get_authentik_sync() (the identity directory), authority_verifier() (Migration Center), and
// AUTH_PROVIDER_ID (lib/edition.ts) for every signIn().
//
// Run: node scripts/checks/check-identity-seams.mjs [repo-root]

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const backend = join(root, "backend", "app");
const frontend = join(root, "frontend", "src");

const RULES = [
  {
    what: "reads the identity provider's issuer",
    pattern: /\bauthentik_issuer\b/,
    allowed: [
      "core/config.py", "core/auth.py", "core/jwks.py", "main.py",
      "services/host_health/checks/identity.py", "services/migrations/authority.py",
    ],
    fix: "ask core.auth.token_verification_configured(settings)",
  },
  {
    what: "builds the Authentik directory itself",
    pattern: /\bAuthentikSyncService\s*\(/,
    allowed: ["services/authentik_sync.py"],
    fix: "call get_authentik_sync(), which returns the edition's directory when it has one",
  },
  {
    what: "builds Migration Center's Authentik verifier itself",
    pattern: /\bAuthentikAuthorityVerifier\s*\(/,
    allowed: ["services/migrations/authority.py"],
    fix: "call authority_verifier(settings)",
  },
  {
    what: "reads an account from the Authentik admin API",
    pattern: /\b(fetch_current_roles|find_account)\s*\(/,
    allowed: ["services/authentik_role_reading.py", "services/authentik_sync.py"],
    fix: "read roles through get_authentik_sync().read_current_roles(email)",
  },
  {
    what: "verifies a token against the identity provider's keys",
    pattern: /\b(_jwks_client|validate_jwt\s*\(|JWKSClient\s*\()/,
    // The Host Health identity check reports the key cache; an edition replaces the check by id.
    allowed: ["core/auth.py", "core/jwks.py", "main.py", "services/host_health/checks/identity.py"],
    fix: "call core.auth.verify_access_token(token, settings, connection)",
  },
  {
    what: "uses the Authentik admin API credentials",
    pattern: /\bauthentik_api_(url|token)\b/,
    allowed: [
      "core/config.py", "services/authentik_sync.py",
      "services/host_health/checks/identity.py", "services/migrations/authority.py",
    ],
    fix: "go through get_authentik_sync()",
  },
];

const FRONTEND_RULES = [
  {
    // A string literal as the provider: signIn("authentik", ...) names a provider an edition lacks.
    what: "names a sign-in provider literally",
    pattern: /\bsignIn\(\s*["'`]/,
    allowed: [],
    fix: "pass AUTH_PROVIDER_ID from @/lib/edition",
  },
  {
    what: "reads the identity provider's settings",
    pattern: /\b(process\.env\.AUTHENTIK_|NEXT_PUBLIC_AUTHENTIK_)/,
    allowed: ["auth.ts", "lib/identity-provider.ts", "lib/idp-urls.ts", "lib/sign-out.ts"],
    fix: "use the helpers in lib/identity-provider.ts, which answer nothing when no IdP is configured",
  },
];

function walk(dir, extensions, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === "__pycache__" || entry.name.startsWith(".")) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) walk(full, extensions, out);
    else if (extensions.some((ext) => entry.name.endsWith(ext))) out.push(full);
  }
  return out;
}

// Code lines only: a comment that names the provider explains it, it does not call it. A leading
// "*" is a block-comment line in TypeScript; in Python it is an unpacking (**kwargs) and stays.
function codeLines(text, commentPrefix) {
  const blockStars = commentPrefix === "//";
  return text.split("\n").map((line, index) => ({ line, number: index + 1 }))
    .filter(({ line }) => !line.trim().startsWith(commentPrefix) && !(blockStars && line.trim().startsWith("*")));
}

const offenders = [];
let scanned = 0;

for (const file of walk(backend, [".py"])) {
  const rel = relative(backend, file).split(sep).join("/");
  // An edition package implements the seams; it may reach its own identity store freely.
  if (rel.startsWith("editions/")) continue;
  scanned += 1;
  const lines = codeLines(readFileSync(file, "utf8"), "#");
  for (const rule of RULES) {
    if (rule.allowed.includes(rel)) continue;
    for (const { line, number } of lines) {
      if (rule.pattern.test(line)) offenders.push(`backend/app/${rel}:${number} ${rule.what}; ${rule.fix}`);
    }
  }
}

for (const file of walk(frontend, [".ts", ".tsx"])) {
  const rel = relative(frontend, file).split(sep).join("/");
  if (rel.startsWith("editions/")) continue;
  scanned += 1;
  const lines = codeLines(readFileSync(file, "utf8"), "//");
  for (const rule of FRONTEND_RULES) {
    if (rule.allowed.includes(rel)) continue;
    for (const { line, number } of lines) {
      if (rule.pattern.test(line)) offenders.push(`frontend/src/${rel}:${number} ${rule.what}; ${rule.fix}`);
    }
  }
}

// A scan over nothing passes for the wrong reason.
if (scanned < 500) offenders.push(`only ${scanned} source files scanned; expected the backend and frontend trees`);

if (offenders.length) {
  console.error("Identity code that bypasses the edition seams:");
  for (const offender of offenders) console.error(`  ${offender}`);
  process.exit(1);
}
console.log(`identity seams: ${scanned} files, every identity-provider call goes through a seam`);
