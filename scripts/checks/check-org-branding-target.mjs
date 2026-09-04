#!/usr/bin/env node
// Structural guard: the organization branding editor targets the organization the session
// actually renders, and organization ids are strings by the time they reach a picker.
//
// Both halves are defects that shipped.
//
// The first: components/org-personalization-settings.tsx auto-selected the first entry of
// GET /organizations. The backend orders that list by name, and for an Admin it is every active
// organization, so "the first one" was whichever customer sorted alphabetically first. The
// sidebar resolves its organization somewhere else entirely, through _get_primary_org_id, which
// is the caller's earliest membership. Two rules for "your organization" that agree for most
// accounts and silently disagree for anyone who can see more than one. An operator edited
// ALIMRUGI's branding while watching Sivali Cloud Technology's: every change looked like it
// reverted, and a customer's colours were overwritten by somebody who never chose them.
//
// The second: OrgResponse.id is an int and the frontend type said string, so the component held
// a raw number in a `string` state and handed it to a Radix Select, which matches by value
// against String(id) on each item. An organization was selected and the trigger rendered
// "Select organization".
//
// Why a structural guard rather than a browser test. The Playwright spec
// (/root/qa-orcastra/tests/org-branding-targets-your-org.spec.ts) asserts the invariant against
// real accounts, but it can only catch a regression on an account whose primary organization is
// not the alphabetically first one it can list, and no QA account has that shape today. So the
// spec would go green through the exact regression it describes. This file gates the line
// instead, which is the part that cannot be arranged away by test data.
//
// Deliberately parser-free and dependency-free, like the other guards here: the CI `guards` job
// runs checkout and setup-node and no `npm ci`.
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

// Rule 2: nothing seeds the selected organization straight out of an ordered list. This is the
// exact shape that shipped: `const firstId = orgList[0].id; setOrgId(firstId);`
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
// important: a property READ, not the identifier anywhere. Matching bare `is_member` passed on
// the explanatory comment three lines above the code, which is a guard reporting green because
// somebody described the rule rather than followed it.
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
