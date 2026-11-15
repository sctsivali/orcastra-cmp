#!/usr/bin/env node
// The access notice for a caller with no role. The frontend decides "no role" from the
// session's groups and the backend refuses the same caller from the token's groups, so both must
// read the same claim and map it with the same table, or one side blocks a user the other lets in.
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

// --- the groups claim of an issued access token
const { readGroupsClaim, roleGroups } = load("frontend/src/lib/token-claims.ts");
const jwt = (claims) => `h.${Buffer.from(JSON.stringify(claims)).toString("base64url")}.s`;
assert.deepEqual(readGroupsClaim(jwt({ groups: ["role_tenant", "Everyone"] })), ["role_tenant", "Everyone"]);
assert.deepEqual(readGroupsClaim(jwt({ groups: [] })), [], "Authentik's answer for no groups is a real answer");
assert.deepEqual(readGroupsClaim(jwt({ groups: ["ünïcode-grüppe", "a/b?c"] })), ["ünïcode-grüppe", "a/b?c"]);
for (const token of [undefined, null, "", "nodots", "h..s", "h.!!!.s", jwt({}), jwt({ groups: "role_admin" }), jwt({ groups: [1] }), "h.bnVsbA.s"]) {
  assert.equal(readGroupsClaim(token), null, `unreadable: ${String(token)}`);
}
assert.deepEqual(roleGroups(["role_admin", "Everyone", "admins"]), ["role_admin"]);

// --- the same group table on both sides
const rbac = load("frontend/src/types/rbac.ts");
const backend = readFileSync(join(root, "backend/app/core/rbac.py"), "utf8");
const block = backend.match(/AUTHENTIK_GROUP_MAP: dict\[str, Role\] = \{([\s\S]*?)\n\}/);
assert.ok(block, "backend AUTHENTIK_GROUP_MAP not found");
const backendMap = Object.fromEntries(
  [...block[1].matchAll(/"([^"]+)":\s*Role\.(ADMIN|PARTNER|TENANT)/g)].map(([, key, role]) => [key, role.toLowerCase()]),
);
assert.deepEqual(
  Object.fromEntries(Object.entries(rbac.AUTHENTIK_GROUP_MAP).sort()),
  Object.fromEntries(Object.entries(backendMap).sort()),
  "frontend and backend map different groups to roles",
);
assert.equal(rbac.hasMappedRole([]), false);
assert.equal(rbac.hasMappedRole(["Everyone", "authentik Read-only"]), false);
assert.equal(rbac.hasMappedRole(["Everyone", " ROLE_TENANT "]), true);
assert.equal(rbac.hasMappedRole(["role_orcastra-partners"]), true, "the role_ prefix strips like the backend");
assert.deepEqual(rbac.resolveRoles([]), ["tenant"], "permission checks keep the Tenant default");

// --- the gate runs before any of the shell
const layout = readFileSync(join(root, "frontend/src/components/main-layout.tsx"), "utf8");
const gate = layout.indexOf('access.state !== "allowed"');
assert.ok(gate > 0, "MainLayout must consult the access gate");
for (const shell of ["<Sidebar", "<Header", "<TaskPanel", "<TerminalManager", "<BreakGlassModal"]) {
  assert.ok(layout.indexOf(shell) > gate, `${shell} renders before the access gate`);
}
assert.ok(layout.indexOf("if (isAuthPage)") < gate, "the auth pages must never show the notice");

// A session refresh reports "loading" while it keeps the session; the gate must hold its verdict
// through it, or the shell flashes and the notice loses its Check again result.
const gateHook = readFileSync(join(root, "frontend/src/hooks/use-access-gate.ts"), "utf8");
assert.match(gateHook, /Boolean\(session\) && status !== "unauthenticated"/);
assert.doesNotMatch(gateHook, /status === "authenticated"/);

// A Tenant's pending verdict holds the shell back: rendering it would let pages read the empty
// scope of a Tenant with no access, and those zeros would stay cached after access is granted.
assert.match(gateHook, /else if \(isTenant && !knownVerdict && verdict\.isPending\) state = "checking"/);

// A session whose groups map to no role asks the backend too: an account an active organization
// lists as a member is a Tenant (a partner's federated buyer never holds a role group). Deciding
// "no role" from the groups alone locked every such buyer out.
assert.match(gateHook, /const asksVerdict = groupless \|\| tenantByGroups/);
assert.match(gateHook, /enabled: asksVerdict/, "the verdict must be asked for a groupless session");
assert.match(
  gateHook,
  /tenantByMembership = groupless && knownVerdict\?\.has_role === true && knownVerdict\.role === "tenant"/,
  "a groupless session is a Tenant only when the backend says so",
);
assert.match(
  gateHook,
  /if \(groupless && !isTenant\) state = !knownVerdict && verdict\.isPending \? "checking" : "no_role"/,
  "a groupless session waits for its verdict, then stays on the notice unless the backend admits it",
);
assert.match(gateHook, /if \(!asksVerdict\) return "unchanged"/, "Check again must ask the backend for a groupless session");
assert.doesNotMatch(gateHook, /if \(!isTenant\) return "unchanged"/, "Check again must not skip a groupless session");
for (const consumer of ["frontend/src/components/personalization-provider.tsx", "frontend/src/hooks/use-vault-status.ts"]) {
  const source = readFileSync(join(root, consumer), "utf8");
  assert.match(source, /useSessionHasRole\(\)/, `${consumer} must read the role the backend honours, not the groups alone`);
  assert.doesNotMatch(source, /hasMappedRole\(/, `${consumer} decides from the groups alone`);
}
assert.match(gateHook, /removeQueries/, "reads made before a denial must not survive into the granted dashboard");
assert.match(gateHook, /lastVerdictRef\.current\?\.scope === actorScope/, "Clear Browser Cache must not send a known Tenant back to the loader");
const checking = layout.indexOf('access.state === "checking"');
assert.ok(checking > 0 && checking < gate, "the loader for a pending verdict comes before the shell");

console.log("access gate: ok");
