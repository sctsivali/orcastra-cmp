#!/usr/bin/env node
// The user menu's Profile link is derived from the identity provider's end-session URL, the one
// per-deployment IdP address the browser bundle already carries. A wrong derivation sends every
// user of a partner install to someone else's IdP, or to a page that does not exist, and nothing
// errors. This compiles frontend/src/lib/idp-account-url.ts standalone and pins the mapping.
//
// Run: node scripts/checks/check-idp-account-url.mjs [repo-root]

import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compileStandalone } from './standalone-ts.mjs';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { module, cleanup } = await compileStandalone(root, 'idp-account-url.ts', 'idp-account-url');
const { idpAccountSettingsUrl } = module;

const CASES = [
  // [why, end-session URL as the operator sets it, expected account settings URL]
  ['dev IdP', 'https://auth.orca.id/application/o/orcastra-dashboard/end-session/',
    'https://auth.orca.id/if/user/#/settings'],
  ['production IdP', 'https://sso.orcastra.io/application/o/orcastra-dashboard/end-session/',
    'https://sso.orcastra.io/if/user/#/settings'],
  ['partner IdP on a plain-HTTP address and port',
    'http://10.20.0.11:9000/application/o/orcastra-dashboard/end-session/',
    'http://10.20.0.11:9000/if/user/#/settings'],
  ['Authentik served under a path prefix', 'https://example.com/authentik/application/o/cmp/end-session/',
    'https://example.com/authentik/if/user/#/settings'],
  ['no trailing slash', 'https://sso.example.com/application/o/cmp/end-session',
    'https://sso.example.com/if/user/#/settings'],
  ['query string is dropped', 'https://sso.example.com/application/o/cmp/end-session/?client_id=abc&x=1',
    'https://sso.example.com/if/user/#/settings'],
  ['credentials in the URL are dropped', 'https://user:secret@sso.example.com/application/o/cmp/end-session/',
    'https://sso.example.com/if/user/#/settings'],
  ['surrounding whitespace is ignored', '  https://sso.example.com/application/o/cmp/end-session/\n',
    'https://sso.example.com/if/user/#/settings'],
  ['unreplaced build placeholder', 'http://NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL_PLACEHOLDER', null],
  ['placeholder even with an Authentik-shaped path',
    'http://NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL_PLACEHOLDER/application/o/x/end-session/', null],
  ['unset', '', null],
  ['blank', '   ', null],
  ['not a URL', 'sso.example.com/application/o/cmp/end-session/', null],
  ['javascript scheme', 'javascript:alert(1)//application/o/cmp/end-session/', null],
  ['data scheme', 'data:text/html,<b>x</b>/application/o/cmp/', null],
  ['ftp scheme', 'ftp://sso.example.com/application/o/cmp/end-session/', null],
  ['IdP that is not Authentik', 'https://kc.example.com/realms/acme/protocol/openid-connect/logout', null],
  ['the provider segment only in the query string', 'https://kc.example.com/logout?next=/application/o/x/', null],
  ['a path prefix that looks like a host stays on the configured host',
    'https://sso.example.com//evil.example/application/o/cmp/end-session/',
    'https://sso.example.com//evil.example/if/user/#/settings'],
  ['IPv6 literal with a port', 'http://[fd00::11]:9000/application/o/cmp/end-session/',
    'http://[fd00::11]:9000/if/user/#/settings'],
];

let failures = 0;
for (const [why, input, want] of CASES) {
  const got = idpAccountSettingsUrl(input);
  if (got !== want) {
    failures += 1;
    console.error(`  FAIL ${why}: ${JSON.stringify(input)} -> ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
  }
}

cleanup();
if (failures) {
  console.error(`\nidp account url: ${failures} of ${CASES.length} cases failed`);
  process.exit(1);
}
console.log(`idp account url: ${CASES.length} cases pass`);
