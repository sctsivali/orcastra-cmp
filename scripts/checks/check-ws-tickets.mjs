#!/usr/bin/env node
// Structural guard: every WebSocket this app opens authenticates with a single-use ticket.
//
// A browser cannot set headers on an upgrade, so the sockets used to carry the access token in
// their query string, where proxies, access logs and crash reports keep URLs. Each socket now
// mints a ticket over the authenticated fetch right before it connects (lib/ws-ticket-api.ts),
// and the vendored SPICE client gives each of its channel sockets its own ticket instead of
// reusing the main socket's URL. This keeps a token from coming back into a socket URL, keeps
// every opener on the ticket helper, and keeps the SPICE patch in place.
//
// Run: node scripts/checks/check-ws-tickets.mjs [repo-root]

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const srcDir = join(root, 'frontend', 'src');
const read = (rel) => readFileSync(join(root, rel), 'utf8');

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(ts|tsx)$/.test(entry)) out.push(full);
  }
  return out;
}

const failures = [];
let openers = 0;
for (const file of walk(srcDir)) {
  const rel = relative(root, file);
  const text = readFileSync(file, 'utf8');
  if (/[?&]token=/.test(text)) failures.push(`${rel}: builds a URL with a token query parameter`);
  if (/new WebSocket\(/.test(text) || /new SpiceMainConn\(/.test(text)) {
    openers += 1;
    if (!/\b(ticketedSocketUrl|spiceSocketUrls)\(/.test(text)) {
      failures.push(`${rel}: opens a socket without minting a ticket through lib/ws-url.ts`);
    }
  }
  if (/new SpiceMainConn\(/.test(text) && !/channel_uris: urls\.channels/.test(text)) {
    failures.push(`${rel}: a SPICE session must hand its channels their own ticketed URIs`);
  }
}

const wsUrl = read('frontend/src/lib/ws-url.ts');
if (/getAccessToken|token=/.test(wsUrl)) failures.push('frontend/src/lib/ws-url.ts: must not read or send the access token');
if (!/mintWsTicket\(request\)/.test(wsUrl)) failures.push('frontend/src/lib/ws-url.ts: socket URLs must carry a freshly minted ticket');
if (!/SESSION_ENDED: 4401/.test(wsUrl) || !/ACCESS_REVOKED: 4403/.test(wsUrl)) {
  failures.push('frontend/src/lib/ws-url.ts: the session-ended and access-revoked close codes must be exported');
}

const spiceMain = read('frontend/public/spice-html5/main.js');
const spiceConn = read('frontend/public/spice-html5/spiceconn.js');
if ((spiceMain.match(/conn\.uri = this\.next_channel_uri\(\);/g) || []).length !== 3) {
  failures.push('frontend/public/spice-html5/main.js: display, inputs and cursor must each take their own channel URI');
}
if (/uri: this\.ws\.url/.test(spiceMain)) failures.push('frontend/public/spice-html5/main.js: a channel reuses the main socket URL and its spent ticket');
if (!/"Connected to " \+ String\(this\.ws\.url\)\.split\("\?"\)\[0\]/.test(spiceMain)) {
  failures.push('frontend/public/spice-html5/main.js: the logged URL must drop its query (and its ticket)');
}
if (!/String\(e\.target\.url\)\.split\("\?"\)\[0\]/.test(spiceConn) || !/String\(o\.uri\)\.split\("\?"\)\[0\]/.test(spiceConn)) {
  failures.push('frontend/public/spice-html5/spiceconn.js: logged and thrown URLs must drop their query');
}
if (!/code === 4403/.test(spiceConn)) failures.push('frontend/public/spice-html5/spiceconn.js: a 4403 close must say access was revoked');

if (openers < 7) failures.push(`expected at least 7 socket openers, found ${openers}`);

if (failures.length > 0) {
  console.error(`WebSocket tickets: ${failures.length} failure(s)`);
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}
console.log(`WebSocket tickets: ${openers} openers mint a ticket per connect; no token reaches a socket URL`);
