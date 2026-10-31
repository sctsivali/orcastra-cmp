#!/usr/bin/env node
// The paid term behind a Tenant's access (backend core/tenant_term.py). After OrcaHub's term ends
// the Tenant's instances are view only, and after the retention window the access is gone. This
// pins that the console says so instead of leaving a disabled Start button as the only clue: the
// notice is mounted on every page inside the shell, it adds no request of its own, its copy names
// no date it was not given, and an instance row says read only without opening its menu.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const ts = createRequire(join(root, "frontend/package.json"))("typescript");
const read = (path) => readFileSync(join(root, path), "utf8");

const layout = read("frontend/src/components/main-layout.tsx");
const gate = layout.indexOf('access.state !== "allowed"');
const vault = layout.indexOf("<VaultNotice />");
const term = layout.indexOf("<TenantTermNotice summary={access.tenantAccess} />");
assert.ok(gate > 0 && term > gate, "the term notice belongs inside the shell, after the access gate");
assert.ok(term > vault, "the term notice sits below the Vault notice");
assert.match(layout, /endedAt=\{access\.tenantAccess\?\.ended_at \?\? null\}/, "the no-access notice is told when access ended");

const notice = read("frontend/src/components/tenant-term-notice.tsx");
for (const forbidden of ["useQuery", "fetchAPI", "fetchRead", "accessAPI"]) {
  assert.ok(!notice.includes(forbidden), `the term notice reads the verdict the shell already has, not ${forbidden}`);
}
assert.match(notice, /role="status"/, "the notice announces itself politely");

// The copy, rendered from the module itself: every phase, with and without dates.
const output = ts.transpileModule(notice, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React },
}).outputText;
const lines = [];
const React = { createElement: (_type, props, ...children) => { lines.push(children.flat().filter((c) => typeof c === "string" || typeof c === "number").join("")); return null; } };
const module = { exports: {} };
new Function("require", "module", "exports", "React", output)((name) => {
  if (name === "lucide-react") return { Clock: "Clock", Lock: "Lock" };
  if (name === "@/lib/utils") return { cn: (...c) => c.filter(Boolean).join(" "), formatDateTime: (v) => `<${v}>`, plural: (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}` };
  throw new Error(`Unexpected module dependency: ${name}`);
}, module, module.exports, React);
const { TenantTermNotice } = module.exports;
const item = (over) => ({ cluster_id: "nuc2", project_name: "shop", phase: "read_only", inferred: false, term_ended_at: null, access_ends_at: null, ...over });
const render = (items) => { lines.length = 0; TenantTermNotice({ summary: { total_clusters: 1, total_rules: 1, items } }); return lines.join(" | "); };

assert.equal(TenantTermNotice({ summary: null }), null, "nothing renders without a term");
assert.equal(TenantTermNotice({ summary: { total_clusters: 1, total_rules: 1, items: [] } }), null);
assert.match(render([item({ term_ended_at: "T", access_ends_at: "A" })]), /ended <T>\. Read-only access until <A>, then the project is deleted\./);
assert.match(render([item({ inferred: true })]), /Subscription for shop ended\. Its instances are read-only until your provider removes them\./);
assert.doesNotMatch(render([item({ inferred: true })]), /<null>|undefined/, "no date is invented");
assert.match(render([item({ phase: "ending", term_ended_at: "T" })]), /ends <T>\. After that its instances become read-only\./);
assert.match(render(["a", "b", "c", "d", "e"].map((p) => item({ project_name: p }))), /and 2 more projects\./, "a long list is cut with a count");

for (const path of [
  "frontend/src/components/tenant-term-notice.tsx",
  "frontend/src/components/access-blocked-notice.tsx",
  "frontend/src/components/tenant-access/policy-term-status.tsx",
  "frontend/src/components/tenant-access/orphan-rules-notice.tsx",
]) {
  assert.ok(!read(path).includes("—"), `${path} carries an em dash in its copy`);
}

const row = read("frontend/src/app/inventory/instances/components/instance-table-row.tsx");
assert.match(row, /termReadOnlyReason\?\.\(instance\.host, project, instance\.name\)/, "the instance row asks why it is read only");
assert.match(row, /data-testid="term-read-only"/, "the instance row marks a read-only instance");
const page = read("frontend/src/app/inventory/instances/page.tsx");
assert.match(page, /termReadOnlyReason\(host, project, name\)/, "a menu item says the term, not a bare missing permission");

const dashboard = read("frontend/src/components/dashboard.tsx");
assert.match(dashboard, /tenantReachesNothing\(projectsData, vmsData\)/, "a Tenant whose access reaches nothing is told so");
assert.match(dashboard, /host\.reachable === false\)\) return false/, "an unreachable cluster is never read as nothing");

console.log("tenant term notice: ok");
