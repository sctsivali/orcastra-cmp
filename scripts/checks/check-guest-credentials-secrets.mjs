#!/usr/bin/env node
// The Credentials panel handles guest passwords and pasted SSH keys. This pins where they may go.
//
// A password typed for a guest's root account is a secret the dashboard must not keep: not in the
// TanStack mutation cache (useMutation holds the variables of its last call), not in browser
// storage or a persisted store, not in a console line, and not in a browser-managed password
// field, which offers to save it into the operator's synced profile. The generator must draw from
// crypto.getRandomValues, and the browser must recognise a private key before anything is sent.
//
// Scans components/guest-credentials, lib/guest-credentials and the panel's hook, comments
// stripped, then runs the generator and the private-key test themselves.
//
// Run: node scripts/checks/check-guest-credentials-secrets.mjs [repo-root]
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const src = join(root, "frontend", "src");
const SCANNED = ["components/guest-credentials", "lib/guest-credentials", "hooks/use-guest-credentials.ts"];
const FIELD = "components/guest-credentials/password-field.tsx";
const GENERATOR = "lib/guest-credentials/password.ts";

function* walk(path) {
  if (statSync(path).isDirectory()) {
    for (const entry of readdirSync(path)) yield* walk(join(path, entry));
  } else if (/\.(ts|tsx)$/.test(path)) {
    yield path;
  }
}

// Comments go first, so a note explaining why useMutation is not used does not count as using it.
function stripComments(code) {
  return code
    .replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, " "))
    .replace(/(^|[^:"'`])\/\/.*$/gm, (comment, lead) => lead + " ".repeat(comment.length - lead.length));
}

const RULES = [
  [/\buseMutation\b/, "useMutation keeps the variables of its last call, which here are a password or a key"],
  [/\b(?:localStorage|sessionStorage)\b/, "browser storage outlives the dialog; a secret lives in component state only"],
  [/\bpersist\s*\(/, "a persisted store writes to browser storage"],
  [/\bconsole\.(?:log|debug|info|warn|error)\b/, "a console line can carry a secret into a shared log or a screenshot"],
  [/\bMath\.random\b/, "passwords are drawn from crypto.getRandomValues, never a predictable generator"],
  [/\btype\s*=\s*\{?\s*["'`]password["'`]/, "a password field offers to save the value into the browser profile; use PasswordField"],
];
// Masked inputs and password-named inputs belong to the shared field alone.
const FIELD_ONLY = [
  [/text-security/, "a masked input outside PasswordField"],
  [/<(?:Input|input)\b[^>]*password/i, "a password input outside PasswordField"],
];

const failures = [];
let scanned = 0;
for (const entry of SCANNED) {
  const path = join(src, entry);
  if (!existsSync(path)) {
    failures.push(`${entry}: missing, so nothing here would be checked`);
    continue;
  }
  for (const file of walk(path)) {
    scanned += 1;
    const rel = relative(src, file).split("\\").join("/");
    const lines = stripComments(readFileSync(file, "utf8")).split("\n");
    const rules = rel === FIELD ? RULES : [...RULES, ...FIELD_ONLY];
    lines.forEach((line, index) => {
      for (const [pattern, reason] of rules) {
        if (pattern.test(line)) failures.push(`${rel}:${index + 1}: ${reason}`);
      }
    });
  }
}

// Anti-vacuous: the shared field still masks with CSS and opts out of password managers, and the
// walk still reaches the panel.
const field = readFileSync(join(src, FIELD), "utf8");
for (const marker of ['type="text"', "[-webkit-text-security:disc]", 'data-1p-ignore="true"', 'autoComplete="off"']) {
  if (!field.includes(marker)) failures.push(`${FIELD}: lost ${marker}`);
}
if (!readFileSync(join(src, GENERATOR), "utf8").includes("crypto.getRandomValues")) {
  failures.push(`${GENERATOR}: the generator no longer draws from crypto.getRandomValues`);
}
if (scanned < 12) failures.push(`only ${scanned} files scanned; the panel moved and this check went blind`);

// Run the real generator and private-key test, beyond scanning their text.
const ts = createRequire(join(root, "frontend/package.json"))("typescript");
function load(relativePath) {
  const output = ts.transpileModule(readFileSync(join(src, relativePath), "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", output)((name) => {
    throw new Error(`Unexpected module dependency: ${name}`);
  }, module, module.exports);
  return module.exports;
}

const { generatePassword, GENERATED_ALPHABET, GENERATED_LENGTH, passwordGroups } = load(GENERATOR);
assert.equal(GENERATED_ALPHABET, "bcdefghjknprstuvx", "the alphabet must stay the same on US, QWERTZ and AZERTY keymaps");
assert.equal(GENERATED_LENGTH, 22);
// 255 is the one byte at or above 17 * 15 and must be dropped; 254 maps to 254 % 17 = 16.
const scripted = [255, 0, 255, 16, 254, 17, ...Array.from({ length: 40 }, (_, i) => i)];
let offset = 0;
const fill = (bytes) => {
  for (let i = 0; i < bytes.length; i += 1) bytes[i] = scripted[(offset + i) % scripted.length];
  offset += bytes.length;
  return bytes;
};
const drawn = generatePassword(fill);
assert.equal(drawn.length, 22);
assert.equal(drawn.slice(0, 4), "bxxb", "bytes at or above 255 are rejected, the rest map through index % 17");
for (let run = 0; run < 200; run += 1) {
  const value = generatePassword((bytes) => {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor((i * 97 + run * 31) % 256);
    return bytes;
  });
  assert.match(value, /^[bcdefghjknprstuvx]{22}$/);
}
assert.deepEqual(passwordGroups("abcdefghij"), ["abcd", "efgh", "ij"]);

const { looksLikePrivateKey, precheckPublicKey } = load("lib/guest-credentials/public-key.ts");
for (const sample of [
  "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----",
  "-----BEGIN RSA PRIVATE KEY-----",
  "-----BEGIN PRIVATE KEY-----",
  "PuTTY-User-Key-File-3: ssh-ed25519",
  "---- BEGIN SSH2 ENCRYPTED PRIVATE KEY ----",
  "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU",
]) {
  assert.ok(looksLikePrivateKey(sample), `not recognised as a private key: ${sample.slice(0, 40)}`);
  assert.equal(precheckPublicKey(sample).state, "private");
}
const publicKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl user@host";
assert.equal(looksLikePrivateKey(publicKey), false);
assert.equal(precheckPublicKey(publicKey).state, "plausible");
assert.equal(precheckPublicKey("").state, "empty");

if (failures.length) {
  console.error("guest credentials secrets: a password or a pasted key can leak");
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}
console.log(`guest credentials secrets: ${scanned} files scanned, generator and private-key test hold`);
