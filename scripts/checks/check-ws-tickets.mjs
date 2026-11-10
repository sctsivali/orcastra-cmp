#!/usr/bin/env node
// Structural guard: every WebSocket this app opens authenticates with a single-use ticket.
//
// A browser cannot set headers on an upgrade, and an access token in the query string ends up
// wherever proxies, access logs and crash reports keep URLs. Each socket mints a ticket over the
// authenticated fetch right before it connects (lib/ws-ticket-api.ts), and the vendored SPICE
// client gives each of its channel sockets its own ticket instead of reusing the main socket's
// URL. This keeps tokens out of socket URLs, every opener on the ticket helper, and the SPICE
// patch in place.
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
  if (/new WebSocket\(/.test(text) || /new (?:G\.)?SpiceMainConn\(/.test(text)) {
    openers += 1;
    if (!/\b(ticketedSocketUrl|spiceSocketUrls)\(/.test(text)) {
      failures.push(`${rel}: opens a socket without minting a ticket through lib/ws-url.ts`);
    }
  }
  if (/new (?:G\.)?SpiceMainConn\(/.test(text) && !/channel_uris: urls\.channels/.test(text)) {
    failures.push(`${rel}: a SPICE session must hand its channels their own ticketed URIs`);
  }
}

const wsUrl = read('frontend/src/lib/ws-url.ts');
const wsClose = read('frontend/src/lib/ws-close.ts');
if (/getAccessToken|token=/.test(wsUrl)) failures.push('frontend/src/lib/ws-url.ts: must not read or send the access token');
if (!/mintWsTicket\(request\)/.test(wsUrl)) failures.push('frontend/src/lib/ws-url.ts: socket URLs must carry a freshly minted ticket');
// The close codes live in lib/ws-close.ts (one table, compiled alone by check-ws-close-contract);
// ws-url.ts re-exports WS_CLOSE for the openers that import it from there.
if (!/SESSION_ENDED: 4401/.test(wsClose) || !/ACCESS_REVOKED: 4403/.test(wsClose)) {
  failures.push('frontend/src/lib/ws-close.ts: the session-ended and access-revoked close codes must be exported');
}
if (!/export \{ WS_CLOSE \} from "@\/lib\/ws-close"/.test(wsUrl)) {
  failures.push('frontend/src/lib/ws-url.ts: must re-export WS_CLOSE from lib/ws-close.ts');
}

// Every interactive opener (a shell, a serial console, a SPICE display) reads its closes and its
// refused tickets through lib/socket-recovery.ts, which classifies them with classifyClose. A
// hand-written code list drifts from that table and misreads a close such as 4401.
const recovery = read('frontend/src/lib/socket-recovery.ts');
if (!/classifyClose\(/.test(recovery)) failures.push('frontend/src/lib/socket-recovery.ts: closes must go through classifyClose');
for (const rel of ['frontend/src/lib/use-terminal-socket.ts', 'frontend/src/lib/spice/session.ts']) {
  const text = read(rel);
  if (!/new SocketRecovery\(/.test(text)) failures.push(`${rel}: an interactive socket must hold a SocketRecovery`);
  if (!/recovery\s*\.onClose\(/.test(text)) failures.push(`${rel}: its closes must go through SocketRecovery.onClose`);
  if (!/recovery\s*\.onMintError\(/.test(text)) failures.push(`${rel}: a refused ticket must go through SocketRecovery.onMintError`);
  if (/FINAL_CODES|RETRY_DELAYS_MS|RECONNECT_DELAYS_MS/.test(text)) failures.push(`${rel}: keeps its own close-code list or backoff table`);
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
if (!/else if \(code >= 4000\)/.test(spiceConn) || spiceConn.indexOf('code >= 4000') > spiceConn.indexOf('this.parent.state == "connecting"')) {
  failures.push('frontend/public/spice-html5/spiceconn.js: a server close code (4000 and up) must be read before the channel state');
}
if (!/e\.code = code;/.test(spiceConn) || !/e\.reason = event_reason;/.test(spiceConn) || !/e\.opened = /.test(spiceConn)) {
  failures.push('frontend/public/spice-html5/spiceconn.js: a channel error must carry its close code, reason and whether it opened');
}

// The floor guards against an opener moving somewhere this scan cannot see: the terminal hook
// (lib/use-terminal-socket.ts), the monitoring, events and task feeds, and the SPICE session.
if (openers < 5) failures.push(`expected at least 5 socket openers, found ${openers}`);

if (failures.length > 0) {
  console.error(`WebSocket tickets: ${failures.length} failure(s)`);
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}
console.log(`WebSocket tickets: ${openers} openers mint a ticket per connect; no token reaches a socket URL`);
