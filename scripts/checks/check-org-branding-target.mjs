#!/usr/bin/env node
// Structural guard: the organization branding editor targets the organization the session
// renders, and organization ids are strings by the time they reach a picker.
//
// GET /organizations is ordered by name and, for an Admin, lists every active organization. The
// sidebar resolves the caller's organization on the server (services/branding/subject.py, read
// through GET /organizations/my-preferences). An editor that defaults to the first list entry
// edits another organization's branding for anyone who can see more than one. A browser test
// only sees that on an account whose organization is not the first one it can list, so the
// source is checked here.
//
// Run: node scripts/checks/check-org-branding-target.mjs [repo-root]

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const REL = 'frontend/src/components/org-personalization-settings.tsx';

let src;
try {
  src = readFileSync(join(root, REL), 'utf8');
} catch {
  console.error(`org branding target: ${REL} is missing. If the editor moved, move this guard.`);
  process.exit(1);
}

const offenders = [];
const lines = src.split('\n');

// Rule 1: the default selection is resolved the same way the sidebar resolves it.
// getMyOrgPreferences() is GET /organizations/my-preferences, which is the endpoint
// PersonalizationProvider reads. Anything else reintroduces a second definition of "yours".
if (!/getMyOrgPreferences\s*\(/.test(src)) {
  offenders.push(
    `${REL}: does not call api.organizations.getMyOrgPreferences(). The default organization has ` +
      `to come from the same endpoint the sidebar reads, or the editor and the sidebar can point ` +
      `at different organizations again.`,
  );
}

// Rule 2: nothing seeds the selected organization straight out of an ordered list, ex;
// `const firstId = orgList[0].id; setOrgId(firstId);`
lines.forEach((line, i) => {
  const n = i + 1;
  if (/setOrgId\s*\(\s*[A-Za-z_$][\w$]*\s*\[\s*0\s*\]/.test(line)) {
    offenders.push(
      `${REL}:${n}: seeds the selected organization from the first entry of a list. That list is ` +
        `ordered by name and, for an Admin, is every organization on the deployment.`,
    );
  }
  // A local named after the first element, then handed to setOrgId, is the same defect one line
  // further apart. Catch the assignment too.
  if (/\b(?:const|let)\s+[A-Za-z_$][\w$]*\s*=\s*[A-Za-z_$][\w$]*\s*\[\s*0\s*\]\s*\.\s*id\b/.test(line)) {
    offenders.push(
      `${REL}:${n}: takes .id off the first entry of a list. If this becomes the selected ` +
        `organization, the editor and the sidebar disagree for every account that can see more ` +
        `than one.`,
    );
  }
});

// Rule 3: ids are coerced where they enter the component. Radix Select matches by value, and an
// int id in a string-typed state renders the placeholder over a real selection.
const mapsRawId = /\bid\s*:\s*o(?:rg)?\.id\b/.test(src);
if (mapsRawId) {
  offenders.push(
    `${REL}: maps an organization id into component state without String(). OrgResponse.id is an ` +
      `int, so the picker will match nothing and show its placeholder while an organization is ` +
      `selected.`,
  );
}

// Rule 4: the editor tells the operator which organization it is about to rewrite, and whether
// that organization is theirs. is_member is what separates the two for an Admin.
// Match a property read: a bare `is_member` also matches a comment that describes the rule.
if (!/\.is_member\b/.test(src)) {
  offenders.push(
    `${REL}: does not read is_member. Without it the editor cannot tell an operator that they ` +
      `are about to change a customer's branding rather than their own.`,
  );
}
if (!/Changes here apply to/.test(src)) {
  offenders.push(
    `${REL}: no longer states which organization the controls below write to. That sentence is ` +
      `the only thing between an operator and rewriting somebody else's branding.`,
  );
}

if (offenders.length) {
  console.error('org branding target: FAILED\n');
  for (const o of offenders) console.error(`  - ${o}`);
  console.error('');
  process.exit(1);
}

console.log(
  'org branding target: the editor defaults to the organization the sidebar renders, coerces ids, ' +
    'and names its target',
);
