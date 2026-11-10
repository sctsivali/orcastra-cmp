#!/usr/bin/env node
/**
 * The documentation index must list every document, and say the right thing about each.
 *
 * Rules, all derived from the index block in documentation/README.md:
 *   1. Every .md under documentation/, except README.md, TEMPLATE.md and adr/ (which keeps
 *      its own index under its own markers), appears exactly once.
 *   2. Each row's Status matches the document's frontmatter, so a Draft cannot be
 *      listed as Active.
 *   3. Rows are grouped by folder then sorted by name, so a diff reader can check rule 1.
 *
 * documentation/ is gitignored, so this runs on the working tree only and passes without it.
 */

import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, relative, dirname, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const docs = join(root, 'documentation');

if (!existsSync(docs)) {
  console.log('doc index: no documentation/ directory, nothing to check');
  process.exit(0);
}

const SKIP_FILES = new Set(['README.md', 'TEMPLATE.md']);
const SKIP_DIRS = new Set(['adr']);

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (!SKIP_DIRS.has(entry)) out.push(...walk(full));
      continue;
    }
    if (!entry.endsWith('.md') || SKIP_FILES.has(entry)) continue;
    out.push(full);
  }
  return out;
}

function frontmatterStatus(file) {
  const head = readFileSync(file, 'utf8').split('\n', 40);
  const line = head.find((l) => /^status:\s*\S/.test(l));
  return line ? line.replace(/^status:\s*/, '').trim() : null;
}

const onDisk = walk(docs).map((full) => {
  const rel = relative(docs, full).split(sep).join('/');
  return {
    rel,
    folder: rel.split('/')[0],
    stem: rel.split('/').pop().replace(/\.md$/, ''),
    status: frontmatterStatus(full),
  };
});

const readme = readFileSync(join(docs, 'README.md'), 'utf8');
const START = '<!-- INDEX:START -->';
const END = '<!-- INDEX:END -->';
const from = readme.indexOf(START);
const to = readme.indexOf(END);
if (from === -1 || to === -1) {
  console.error('doc index: the INDEX:START / INDEX:END markers are missing from documentation/README.md');
  process.exit(1);
}

const listed = [];
for (const line of readme.slice(from + START.length, to).split('\n')) {
  const m = line.match(/^\|\s*([^|]+?)\s*\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*([^|]+?)\s*\|$/);
  if (m) listed.push({ folder: m[1], stem: m[2], rel: m[3], status: m[4] });
}

const problems = [];

const listedByRel = new Map(listed.map((r) => [r.rel, r]));
for (const doc of onDisk) {
  const row = listedByRel.get(doc.rel);
  if (!row) {
    problems.push(`missing from the index: ${doc.rel}`);
    continue;
  }
  if (doc.status && row.status !== doc.status) {
    problems.push(`${doc.rel}: index says "${row.status}", the document says "${doc.status}"`);
  }
  if (row.folder !== doc.folder) {
    problems.push(`${doc.rel}: Domain column says "${row.folder}", the folder is "${doc.folder}"`);
  }
  if (row.stem !== doc.stem) {
    problems.push(`${doc.rel}: link text "${row.stem}" is not the file name`);
  }
}

const onDiskRels = new Set(onDisk.map((d) => d.rel));
for (const row of listed) {
  if (!onDiskRels.has(row.rel)) problems.push(`indexed but not on disk: ${row.rel}`);
}

const seen = new Set();
for (const row of listed) {
  if (seen.has(row.rel)) problems.push(`listed twice: ${row.rel}`);
  seen.add(row.rel);
}

const expectedOrder = [...onDisk]
  .sort((a, b) => a.folder.localeCompare(b.folder) || a.stem.localeCompare(b.stem))
  .map((d) => d.rel);
const actualOrder = listed.map((r) => r.rel);
if (
  actualOrder.length === expectedOrder.length &&
  actualOrder.join('\n') !== expectedOrder.join('\n')
) {
  const at = actualOrder.findIndex((r, i) => r !== expectedOrder[i]);
  problems.push(
    `out of order at row ${at + 1}: expected ${expectedOrder[at]}, found ${actualOrder[at]}`,
  );
}

console.log(
  `doc index: ${onDisk.length} documents on disk, ${listed.length} listed, ` +
    `${new Set(onDisk.map((d) => d.folder)).size} folders`,
);

if (problems.length) {
  console.error('\nThe documentation index has drifted:\n  ' + problems.join('\n  '));
  console.error(
    '\nGroup by folder, then by file name. The Domain column is the folder, the link text is ' +
      'the file name, and the Status is the document\'s own frontmatter.',
  );
  process.exit(1);
}

console.log('doc index: every document is listed, in order, with its own status');
