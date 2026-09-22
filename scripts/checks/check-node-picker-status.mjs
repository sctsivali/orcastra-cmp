#!/usr/bin/env node
// Structural guard: a control that asks an operator to choose a cluster must say whether that
// cluster is answering.
//
// The vocabulary, the dot and the consequence line all live in lib/cluster-status.ts,
// components/ui/searchable-select.tsx and components/ui/node-status-notice.tsx. What keeps going
// wrong is coverage, and it takes two shapes. Four pickers were never wired to the hook and kept
// a plain Select over bare names, two of them dropping the clusters that were not answering. Then
// a fifth called useNodePickerOptions, so a guard that only looked for the call was satisfied,
// and rendered the result through a plain Select that printed the display name and nothing else.
//
// Neither shape is visible from inside one file, which is why it is checked here rather than
// reviewed. Every picker is registered below with how it resolves status; a 'hook' picker must
// also hand the hook's options to SearchableSelect, and a create form must render the
// NodeStatusNotice that says creating on an offline cluster will fail.
//
// Run: node scripts/checks/check-node-picker-status.mjs [repo-root]

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const srcDir = join(root, 'frontend', 'src');

// How each known picker resolves status.
//   kind 'hook' - useNodePickerOptions, the one place options are assembled
//   kind 'meta' - builds options inline but resolves through clusterStatusMeta
//   notice     - a create form must render NodeStatusNotice; a scope picker must not disable
//   list       - a 'meta' control that renders its options as a checkbox list rather than a
//                SearchableSelect (a multi-select has no single trigger to search from)
//   deferred   - a gap still open, carrying the reason
const PICKERS = new Map([
  ['components/instance-create-form.tsx', {
    kind: 'hook',
    notice: true,
    hookSource: 'components/instance-create/use-base-inventory.ts',
    optionsAlias: 'nodeOptions',
  }],
  // exported, mounted by no route; covered statically only
  ['components/vm-create-form.tsx', { kind: 'hook', notice: true }],
  ['components/project-create-form.tsx', { kind: 'hook', notice: true }],
  ['components/networks/network-create-dialog.tsx', { kind: 'hook', notice: true }],
  ['components/network-acls/acl-form-dialog.tsx', { kind: 'hook', notice: true }],
  ['components/profiles/profile-form-dialog.tsx', { kind: 'hook', notice: true }],
  ['components/image-upload-dialog.tsx', { kind: 'hook', notice: true }],
  ['components/iso-upload-dialog.tsx', { kind: 'hook', notice: true }],
  ['components/groups/create-group-dialog.tsx', { kind: 'hook', notice: true }],
  ['app/my-keys/page.tsx', { kind: 'hook', notice: true }],
  ['app/inventory/snapshots/components/take-snapshot-dialog.tsx', { kind: 'hook', notice: true }],
  ['components/tenant-access-dialog.tsx', { kind: 'meta', notice: false }],
  ['components/policy-management-section.tsx', { kind: 'meta', notice: false }],
  ['app/inventory/clustering/members/page.tsx', { kind: 'meta', notice: false }],
  ['components/monitoring/node-filter-select.tsx', { kind: 'meta', notice: false }],
  // Closed. /api/v1/integrations/available-clusters now sends status, reason and checked_at, so
  // the component resolves it like every other picker instead of offering bare names. It is a
  // checkbox list of clusters an API key may reach, not a single choice, so no SearchableSelect.
  ['components/cluster-access-selector.tsx', { kind: 'meta', notice: false, list: true }],
  // note: this one slipped the check for its whole life by being split across two files. The page
  // called useNodePickerOptions but rendered no Select, and the picker rendered a Select whose
  // options arrived as a prop named `nodeOptions`, so neither half matched both halves of the
  // shape test. Splitting a picker across a component boundary is a way past this guard, which
  // is worth knowing about the guard. A scope picker, so no create notice.
  ['app/inventory/snapshots/components/snapshot-scope-picker.tsx', { kind: 'hook', notice: false }],
]);

// Not pickers, though they trip the shape test below.
const NOT_A_PICKER = new Map([
  ['components/groups/edit-group-dialog.tsx', 'node is a disabled Input, not a choice'],
  ['app/inventory/monitoring/monitoring-content.tsx', 'delegates to NodeFilterSelect; its own Select is Top-N'],
  ['components/integrations/create-api-key-dialog.tsx', 'mounts ClusterAccessSelector, registered above'],
  ['components/integrations/edit-api-key-permissions-dialog.tsx', 'mounts ClusterAccessSelector, registered above'],
]);

// A file worth classifying: it renders a picker control AND names a cluster/node option source.
const RENDERS_PICKER = /<SearchableSelect|<Select[\s>]/;
const CLUSTER_SOURCE =
  /useNodeNames|useNodesStatus|useNodePickerOptions|useClusters|nodeNames|availableClusters|AvailableCluster/;

const HOOK_HOLDER = /const\s+([A-Za-z_$][\w$]*)\s*=\s*useNodePickerOptions\s*\(/;

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (entry.endsWith('.tsx')) out.push(full);
  }
  return out;
}

/** Opening tags for `name`, with their body, brace-aware so nested JSX does not end them early. */
function tags(code, name) {
  const found = [];
  const open = new RegExp(`<${name}\\b`, 'g');
  let m;
  while ((m = open.exec(code)) !== null) {
    let i = m.index + m[0].length;
    let depth = 0;
    let quote = null;
    for (; i < code.length; i += 1) {
      const c = code[i];
      if (quote) {
        if (c === quote) quote = null;
        continue;
      }
      if (c === '"' || c === "'" || c === '`') quote = c;
      else if (c === '{') depth += 1;
      else if (c === '}') depth -= 1;
      else if (c === '>' && depth === 0) break;
    }
    found.push(code.slice(m.index, i + 1));
  }
  return found;
}

/**
 * The expression a file binds the hook's options to: `nodeOptions` for
 * `const { options: nodeOptions } = useNodePickerOptions()` (or `= holder`), `options` for a
 * bare destructure, `holder.options` when nothing destructures it. Null when the hook is never
 * bound at all.
 */
function optionsAlias(body) {
  const holder = body.match(HOOK_HOLDER)?.[1];
  const sources = ['useNodePickerOptions\\s*\\(', holder && `${holder}\\b`]
    .filter(Boolean)
    .join('|');
  const inner = body.match(new RegExp(`const\\s*\\{([^}]*)\\}\\s*=\\s*(?:${sources})`))?.[1];
  if (inner) {
    const m = inner.match(/(?:^|,)\s*options\s*(?::\s*([A-Za-z_$][\w$]*))?\s*(?=,|$)/);
    if (m) return m[1] ?? 'options';
  }
  return holder ? `${holder}.options` : null;
}

const escape = (s) => s.replace(/[.$]/g, '\\$&');

function checkHookPicker(rel, body, entry, failures) {
  let hookBody = body;
  if (entry.hookSource) {
    try {
      hookBody = readFileSync(join(srcDir, entry.hookSource), 'utf8');
    } catch {
      failures.push(`${rel}: registered hook source ${entry.hookSource} does not exist.`);
      return;
    }
  }
  if (!/\buseNodePickerOptions\b/.test(hookBody)) {
    failures.push(
      `${entry.hookSource ?? rel}: registered as 'hook' but does not call useNodePickerOptions. ` +
        `A cluster picker must resolve status through lib/cluster-status.ts.`,
    );
    return;
  }
  const alias = entry.optionsAlias ?? optionsAlias(body);
  if (!alias) {
    failures.push(`${rel}: calls useNodePickerOptions but never binds its options.`);
    return;
  }
  const esc = escape(alias);
  // The hook's options must reach the control as they are: `options={alias}`, or the one
  // gating shape `options={<cond> ? [] : alias}` (an empty list while the read has failed).
  // A reshaped copy (`alias.map(...)`, `toPlain(alias)`) is not accepted: `status` is optional
  // on SearchableSelectOption, so a copy that drops it renders bare names without a type error.
  const fed = tags(body, 'SearchableSelect').some((tag) =>
    new RegExp(`\\boptions=\\{\\s*(?:[^{}?]+\\?\\s*\\[\\]\\s*:\\s*)?${esc}\\s*\\}`).test(tag),
  );
  if (!fed) {
    failures.push(
      `${rel}: binds the hook's options as '${alias}' but no <SearchableSelect options={${alias}}> ` +
        `renders them. A plain Select drops the dot, the sublabel and the reason.`,
    );
  }
  if (new RegExp(`\\b${esc}\\.map\\s*\\([\\s\\S]{0,400}?<SelectItem\\b`).test(body)) {
    failures.push(
      `${rel}: maps '${alias}' into <SelectItem>, which prints the label and nothing else.`,
    );
  }
  if (entry.notice && !/<NodeStatusNotice\b/.test(body)) {
    failures.push(
      `${rel}: a create-form picker must render <NodeStatusNotice> under the field so an ` +
        `operator is told before the request fails.`,
    );
  }
}

function checkMetaPicker(rel, body, entry, failures) {
  // note: word-boundary, not includes(). A first cut used includes() and a probe that renamed
  // the call to clusterStatusMetaX still passed, because the old name is a prefix of the new.
  if (!/\bclusterStatusMeta\b/.test(body)) {
    failures.push(
      `${rel}: registered as 'meta' but does not use clusterStatusMeta. ` +
        `A cluster picker must resolve status through lib/cluster-status.ts.`,
    );
  }
  if (!entry.list && !/<SearchableSelect\b/.test(body)) {
    failures.push(`${rel}: registered as 'meta' but renders no <SearchableSelect>.`);
  }
}

const failures = [];
const files = walk(srcDir);
const seen = new Set();

for (const full of files) {
  const rel = relative(srcDir, full).split('\\').join('/');
  const body = readFileSync(full, 'utf8');

  const entry = PICKERS.get(rel);
  if (entry?.kind === 'hook') {
    seen.add(rel);
    checkHookPicker(rel, body, entry, failures);
    continue;
  }
  if (entry?.kind === 'meta') {
    seen.add(rel);
    checkMetaPicker(rel, body, entry, failures);
    continue;
  }
  if (entry !== undefined) {
    seen.add(rel);
    continue; // a deferred gap, carrying its reason
  }
  if (NOT_A_PICKER.has(rel)) {
    seen.add(rel);
    continue;
  }

  if (RENDERS_PICKER.test(body) && CLUSTER_SOURCE.test(body)) {
    failures.push(
      `${rel}: looks like a cluster or node picker but is not registered in ` +
        `scripts/checks/check-node-picker-status.mjs. Wire it to useNodePickerOptions, or ` +
        `record why it is not a picker.`,
    );
  }
}

// A registry entry naming a file that no longer exists is a stale claim of coverage.
for (const rel of [...PICKERS.keys(), ...NOT_A_PICKER.keys()]) {
  if (!seen.has(rel)) {
    failures.push(`${rel}: registered here but not found under frontend/src. Stale entry.`);
  }
}

const wired = [...PICKERS.values()].filter((v) => v.kind).length;
const deferred = [...PICKERS].filter(([, v]) => v.deferred);

console.log(
  `node picker status: ${files.length} files scanned, ${wired} pickers carrying status, ` +
    `${deferred.length} deferred`,
);
for (const [rel, v] of deferred) console.log(`  deferred  ${rel}: ${v.deferred}`);

if (failures.length) {
  console.error('\nnode picker status: FAILED');
  for (const f of failures) console.error(`  ${f}`);
  process.exit(1);
}
console.log('node picker status: every cluster picker names the state of what it offers');
