#!/usr/bin/env node
// Both addresses the console sends a browser to on the identity provider come from its end-session
// URL, the one per-deployment IdP address the browser bundle carries: Profile (the account
// settings page) and sign-out (the end-session endpoint). A wrong derivation sends a partner's
// users to someone else's IdP, to a page that does not exist, or to the unreplaced build
// placeholder, and nothing errors. This compiles frontend/src/lib/idp-urls.ts standalone and pins
// both mappings.
//
// Run: node scripts/checks/check-idp-urls.mjs [repo-root]

import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compileStandalone } from './standalone-ts.mjs';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { module, cleanup } = await compileStandalone(root, 'idp-urls.ts', 'idp-urls');
const { idpAccountSettingsUrl, idpEndSessionUrl } = module;

const CASES = [
  // [why, end-session URL as the operator sets it, expected account settings URL]
  ['Authentik at the root of its host', 'https://auth.example.com/application/o/orcastra-dashboard/end-session/',
    'https://auth.example.com/if/user/#/settings'],
  ['another deployment\'s IdP host', 'https://sso.example.org/application/o/orcastra-dashboard/end-session/',
    'https://sso.example.org/if/user/#/settings'],
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

// note: documentation addresses (RFC 5737, RFC 2606), never a real deployment's.
const SIGN_IN = 'http://192.0.2.10:2025/auth/signin';
const RETURN = 'post_logout_redirect_uri=http%3A%2F%2F192.0.2.10%3A2025%2Fauth%2Fsignin';
const END_SESSION_CASES = [
  // [why, end-session URL as the operator sets it, expected sign-out destination]
  ['Authentik at the root of its host', 'https://auth.example.com/application/o/orcastra-dashboard/end-session/',
    `https://auth.example.com/application/o/orcastra-dashboard/end-session/?${RETURN}`],
  ['a query the operator set is kept, and the return address is added once',
    'https://sso.example.com/application/o/cmp/end-session/?client_id=abc',
    `https://sso.example.com/application/o/cmp/end-session/?client_id=abc&${RETURN}`],
  ['a return address already in the value is replaced, not doubled',
    'https://sso.example.com/application/o/cmp/end-session/?post_logout_redirect_uri=https%3A%2F%2Fevil.example%2F',
    `https://sso.example.com/application/o/cmp/end-session/?${RETURN}`],
  ['credentials in the value never reach the browser',
    'https://user:secret@sso.example.com/application/o/cmp/end-session/',
    `https://sso.example.com/application/o/cmp/end-session/?${RETURN}`],
  ['any OIDC end-session endpoint signs out, not only Authentik',
    'https://kc.example.com/realms/acme/protocol/openid-connect/logout',
    `https://kc.example.com/realms/acme/protocol/openid-connect/logout?${RETURN}`],
  ['surrounding whitespace is ignored', ' https://auth.example.com/application/o/x/end-session/\n',
    `https://auth.example.com/application/o/x/end-session/?${RETURN}`],
  ['unreplaced build placeholder', 'http://NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL_PLACEHOLDER', null],
  ['unset', '', null],
  ['not a URL', 'auth.example.com/application/o/x/end-session/', null],
  ['javascript scheme', 'javascript:alert(1)//application/o/x/end-session/', null],
];

let failures = 0;
for (const [why, input, want] of CASES) {
  const got = idpAccountSettingsUrl(input);
  if (got !== want) {
    failures += 1;
    console.error(`  FAIL account settings, ${why}: ${JSON.stringify(input)} -> ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
  }
}
for (const [why, input, want] of END_SESSION_CASES) {
  const got = idpEndSessionUrl(input, SIGN_IN);
  if (got !== want) {
    failures += 1;
    console.error(`  FAIL end session, ${why}: ${JSON.stringify(input)} -> ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
  }
}

const total = CASES.length + END_SESSION_CASES.length;
cleanup();
if (failures) {
  console.error(`\nidp urls: ${failures} of ${total} cases failed`);
  process.exit(1);
}
console.log(`idp urls: ${CASES.length} account settings and ${END_SESSION_CASES.length} end-session cases pass`);
