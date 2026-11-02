#!/usr/bin/env node
// The close contract between the interactive sockets and the pages that open them.
//
// A terminal or console refused before accept() reached the browser as a bare 1006, so a deep
// link opened in a new tab said "disconnected" and nothing recovered. The backend now accepts and
// closes with a code and a reason token (backend/app/core/ws_close.py, plus the relay's own endings
// in services/interactive_relay.py), and the page decides from them (frontend/src/lib/ws-close.ts,
// lib/socket-recovery.ts). Two hand-kept tables drift, so this reads both sides and holds them
// equal, then runs the page's decisions: what each close means, the backoff, and what a refused
// ticket or a close leads to.
//
// Run: node scripts/checks/check-ws-close-contract.mjs [repo-root]

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compileStandalone } from './standalone-ts.mjs';

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..'));
const read = (rel) => readFileSync(join(root, rel), 'utf8');

// ---------------------------------------------------------------- the backend's table
const wsClosePy = read('backend/app/core/ws_close.py');
const relayPy = read('backend/app/services/interactive_relay.py');
const constant = (source, name) => {
  const match = source.match(new RegExp(`^${name} = (\\d+)$`, 'm'));
  assert.ok(match, `backend constant ${name} not found`);
  return Number(match[1]);
};
const backend = {
  NORMAL: constant(relayPy, 'CLOSE_NORMAL'),
  UPSTREAM_FAILED: constant(relayPy, 'CLOSE_UPSTREAM'),
  POWERED_DOWN: constant(relayPy, 'CLOSE_POWERED_DOWN'),
  TAKEN_OVER: constant(relayPy, 'CLOSE_TAKEN_OVER'),
  UNAVAILABLE: constant(relayPy, 'CLOSE_UNAVAILABLE'),
  SESSION_ENDED: constant(wsClosePy, 'AUTH'),
  ACCESS_REVOKED: constant(wsClosePy, 'FORBIDDEN'),
  BREAK_GLASS: constant(wsClosePy, 'BREAK_GLASS'),
  FENCED: constant(wsClosePy, 'FENCED'),
  POLICY_VIOLATION: constant(wsClosePy, 'POLICY'),
};
assert.equal(constant(wsClosePy, 'SERVER'), backend.UPSTREAM_FAILED, 'ws_close.SERVER and the relay 1011 differ');

// REASONS = { NAME: ("a", "b"), ... } in ws_close.py
const reasonsBlock = wsClosePy.slice(wsClosePy.indexOf('REASONS: dict'), wsClosePy.indexOf('MAX_REASON_BYTES'));
const backendReasons = {};
for (const entry of reasonsBlock.matchAll(/^\s{4}([A-Z_]+): \(([\s\S]*?)\),$/gm)) {
  const code = constant(wsClosePy, entry[1]);
  backendReasons[code] = [...entry[2].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}
assert.ok(Object.keys(backendReasons).length >= 5, 'could not read REASONS from ws_close.py');

// Every token the backend actually closes with is in the table.
const used = new Set();
for (const rel of [
  'backend/app/core/ws_auth.py', 'backend/app/routers/terminal.py', 'backend/app/routers/console.py',
  'backend/app/services/ws_session_guard.py', 'backend/app/services/break_glass_service.py',
  'backend/app/services/terminal_proxy.py', 'backend/app/services/console_proxy.py',
]) {
  for (const m of read(rel).matchAll(/ws_close\.[A-Z_]+, "([a-z_:]+)"/g)) used.add(m[1]);
  for (const m of read(rel).matchAll(/reason="([a-z_:]+)"/g)) used.add(m[1]);
  for (const m of read(rel).matchAll(/_refuse_as\(websocket, ws_close\.[A-Z_]+, "([a-z_:]+)"\)/g)) used.add(m[1]);
}
for (const m of read('backend/app/services/ws_session_guard.py').matchAll(/^(?:ACCOUNT_ENDED|ROLE_CHANGED|ACCESS_ENDED) = "([a-z_]+)"$/gm)) {
  used.add(`access_revoked:${m[1]}`);
}
const allBackendReasons = new Set(Object.values(backendReasons).flat());
for (const token of used) assert.ok(allBackendReasons.has(token), `backend closes with "${token}", which ws_close.REASONS does not list`);

// ---------------------------------------------------------------- the page's table
const { module: page, cleanup } = await compileStandalone(root, 'ws-close.ts', 'ws-close');
try {
  const { WS_CLOSE, WS_CLOSE_REASONS, classifyClose, messageFor, backoffMs, reconnectingMessage, MAX_RECONNECT_ATTEMPTS, SOCKET_MESSAGES, isReasonToken } = page;
  assert.deepEqual({ ...WS_CLOSE }, backend, 'frontend WS_CLOSE and the backend codes differ');
  assert.deepEqual(
    Object.fromEntries(Object.entries(WS_CLOSE_REASONS).map(([k, v]) => [k, [...v]])),
    Object.fromEntries(Object.entries(backendReasons).map(([k, v]) => [String(k), v])),
    'frontend WS_CLOSE_REASONS and backend ws_close.REASONS differ',
  );
  for (const token of allBackendReasons) {
    assert.ok(token.length <= 64 && /^[a-z0-9_:.-]+$/.test(token), `"${token}" is not a short ASCII token`);
    assert.ok(isReasonToken(token));
  }
  assert.equal(isReasonToken('Lost the connection to the shell'), false);

  // What each close means.
  const opened = { opened: true };
  for (const [code, kind] of [
    [1000, 'normal'], [4401, 'auth'], [4403, 'forbidden'], [1008, 'forbidden'], [4428, 'break_glass'],
    [4423, 'fenced'], [4404, 'unavailable'], [4409, 'taken_over'], [1012, 'powered_down'],
    [1011, 'server'], [1006, 'network'], [1001, 'network'], [4000, 'network'],
  ]) {
    assert.equal(classifyClose(code, '', opened), kind, `close ${code} after open`);
  }
  // A refusal from a backend that closed before accept arrives as 1006 on a socket that never
  // opened: read as a refusal once, then as the network.
  assert.equal(classifyClose(1006, '', { opened: false }), 'auth');
  assert.equal(classifyClose(1006, '', { opened: false, authTried: true }), 'network');
  assert.equal(classifyClose(1006, '', { opened: true }), 'network');
  // A dropped connection that carried an error frame is read by the frame's code.
  assert.equal(classifyClose(1006, '', { opened: true, errorFrame: { code: 4423 } }), 'fenced');
  assert.equal(classifyClose(4404, '', { opened: true, errorFrame: { code: 4423 } }), 'unavailable');

  // What the page says.
  assert.equal(messageFor('auth'), 'Your session ended. Sign in again to open this terminal.');
  assert.equal(messageFor('forbidden', 'forbidden'), 'You do not have access to this instance.');
  assert.equal(messageFor('forbidden', 'access_revoked:role_changed'), 'Access to this instance was revoked.');
  assert.equal(messageFor('break_glass', 'break_glass_required'), 'Break-glass access is required to open a session on this Cluster.');
  assert.equal(messageFor('break_glass', 'break_glass_expired'), 'Your break-glass access ended, so the session was closed.');
  assert.equal(messageFor('fenced', 'migration_fenced'), 'Migration Center is moving this instance. Try again when the move finishes.');
  assert.equal(messageFor('unavailable', 'The VM agent is not running'), 'The VM agent is not running', 'a sentence passes through');
  for (const sentence of Object.values(SOCKET_MESSAGES)) {
    assert.ok(/\.$/.test(sentence), `"${sentence}" must end with a period`);
    assert.ok(!/[–—]/.test(sentence), `"${sentence}" must not carry a dash`);
  }
  assert.equal(reconnectingMessage(4000, 3), 'Connection lost. Reconnecting in 4s (attempt 3 of 5).');

  // The backoff: 1, 2, 4, 8, 16 s within 20 % either way, five attempts.
  assert.equal(MAX_RECONNECT_ATTEMPTS, 5);
  [1000, 2000, 4000, 8000, 16000].forEach((base, attempt) => {
    assert.equal(backoffMs(attempt, 0), base * 0.8);
    assert.equal(backoffMs(attempt, 0.5), base);
    assert.equal(backoffMs(attempt, 1), base * 1.2);
  });
  assert.equal(backoffMs(9, 0.5), 16000, 'the wait never grows past the last step');

  // ---------------------------------------------------------------- the decisions
  const ts = createRequire(join(root, 'frontend/package.json'))('typescript');
  const wsCloseCjs = (() => {
    const out = ts.transpileModule(read('frontend/src/lib/ws-close.ts'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
    const module = { exports: {} };
    new Function('require', 'module', 'exports', out)(() => { throw new Error('ws-close.ts imports nothing'); }, module, module.exports);
    return module.exports;
  })();
  const recoveryOut = ts.transpileModule(read('frontend/src/lib/socket-recovery.ts'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const recoveryModule = { exports: {} };
  new Function('require', 'module', 'exports', recoveryOut)((name) => {
    if (name === '@/lib/ws-close') return wsCloseCjs;
    if (name === '@/lib/api') return { forceSessionRefresh: async () => 'unavailable' };
    if (name === '@/lib/reauth') return { reauthenticate: () => false };
    throw new Error(`socket-recovery.ts imports ${name}`);
  }, recoveryModule, recoveryModule.exports);
  const { SocketRecovery } = recoveryModule.exports;

  function harness({ verdicts = [], navigates = true } = {}) {
    const calls = { refresh: 0, reauth: [] };
    const recovery = new SocketRecovery({
      refresh: async () => {
        calls.refresh += 1;
        return verdicts.shift() ?? 'unchanged';
      },
      reauthenticate: (options) => {
        calls.reauth.push(options.auto);
        return navigates;
      },
      random: () => 0.5,
    });
    return { recovery, calls };
  }
  const apiError = (status, extra = {}) => Object.assign(new Error(extra.message ?? `HTTP ${status}`), { status, ...extra });
  const cold = { liveSocket: false };
  const live = { liveSocket: true };

  {
    // A refused ticket refreshes once and mints again at once.
    const { recovery, calls } = harness({ verdicts: ['refreshed'] });
    assert.deepEqual(await recovery.onMintError(apiError(401), cold), { action: 'retry', delayMs: 0, message: '' });
    // The same refusal again on this connect: the session cannot be saved from here.
    const again = await recovery.onMintError(apiError(401), cold);
    assert.equal(again.action, 'stop');
    assert.equal(again.signIn, true);
    assert.equal(again.message, 'Your session ended. Sign in again to open this terminal.');
    assert.equal(calls.refresh, 1, 'the refresh path runs at most once per connect');
  }
  {
    // An ended session signs in again on its own where nothing was live, behind a button where it was.
    const coldTab = harness({ verdicts: ['revoked'] });
    assert.deepEqual(await coldTab.recovery.onMintError(apiError(401), cold), { action: 'leaving', message: 'Your sign-in ended. Signing you in again.' });
    assert.deepEqual(coldTab.calls.reauth, [true]);
    const liveTab = harness({ verdicts: ['revoked'], navigates: false });
    const held = await liveTab.recovery.onMintError(apiError(401), live);
    assert.deepEqual(liveTab.calls.reauth, [false]);
    assert.equal(held.action, 'stop');
    assert.equal(held.signIn, true);
  }
  {
    // An IdP that does not answer backs off, and the next round may ask again.
    const { recovery, calls } = harness({ verdicts: ['unavailable', 'refreshed'] });
    const first = await recovery.onMintError(apiError(401), cold);
    assert.equal(first.action, 'retry');
    assert.equal(first.delayMs, 1000);
    assert.equal(first.message, 'The sign-in service did not answer. Retrying.');
    assert.equal((await recovery.onMintError(apiError(401), cold)).delayMs, 2000, 'the second try keeps the backoff');
    assert.equal(calls.refresh, 2);
  }
  {
    // 403 and 409 are refused again on a retry; 429 waits as told; 503 backs off.
    const { recovery } = harness();
    assert.deepEqual(await recovery.onMintError(apiError(403, { message: 'Accessing this tenant resource requires an active break-glass grant.' }), cold),
      { action: 'stop', message: 'Accessing this tenant resource requires an active break-glass grant.' });
    assert.equal((await recovery.onMintError(apiError(409), cold)).action, 'stop');
    assert.deepEqual(await recovery.onMintError(apiError(429, { retryAfter: 7, message: 'Too many' }), cold), { action: 'retry', delayMs: 7000, message: 'Too many' });
    const busy = await recovery.onMintError(apiError(503), cold);
    assert.equal(busy.action, 'retry');
    assert.equal(busy.message, 'Connection lost. Reconnecting in 2s (attempt 2 of 5).');
  }
  {
    // Final closes stop and say why; nothing reconnects into them.
    for (const [code, reason, message] of [
      [4403, 'forbidden', 'You do not have access to this instance.'],
      [4403, 'access_revoked:account_ended', 'Access to this instance was revoked.'],
      [4428, 'break_glass_required', 'Break-glass access is required to open a session on this Cluster.'],
      [4428, 'break_glass_expired', 'Your break-glass access ended, so the session was closed.'],
      [4423, 'migration_fenced', 'Migration Center is moving this instance. Try again when the move finishes.'],
    ]) {
      const { recovery, calls } = harness();
      assert.deepEqual(await recovery.onClose(code, reason, { opened: true, liveSocket: true }), { action: 'stop', message }, `${code} ${reason}`);
      assert.equal(calls.refresh, 0);
    }
  }
  {
    // A 4401 refreshes once, then a second one on the same connect asks to sign in.
    const { recovery, calls } = harness({ verdicts: ['unchanged'] });
    assert.equal((await recovery.onClose(4401, 'ticket_invalid', { opened: true, liveSocket: false })).delayMs, 0);
    assert.equal((await recovery.onClose(4401, 'ticket_invalid', { opened: true, liveSocket: false })).signIn, true);
    assert.equal(calls.refresh, 1);
    // A session that worked resets that, so a later 4401 may refresh again, now after a wait.
    recovery.ready();
    const later = await recovery.onClose(4401, 'session_ended', { opened: true, liveSocket: true });
    assert.equal(calls.refresh, 2);
    assert.equal(later.delayMs, 2000);
  }
  {
    // A SPICE display whose main channel links and whose other channel is then refused 4401 (a
    // ticket store failing over): each round works for a moment and is refused again. Only the
    // first round is immediate; the rest back off and the fifth is the last.
    const { recovery, calls } = harness({ verdicts: [] });
    const delays = [];
    let step;
    for (let round = 0; round < 12; round++) {
      recovery.ready();
      step = await recovery.onClose(4401, 'ticket_invalid', { opened: true, liveSocket: false });
      if (step.action !== 'retry') break;
      delays.push(step.delayMs);
    }
    assert.deepEqual(delays, [0, 2000, 4000, 8000, 16000], 'immediate once, then the backoff');
    assert.deepEqual(step, { action: 'stop', message: 'Could not reconnect. Select Reconnect to try again.', retry: true });
    assert.equal(calls.refresh, 5, 'one session refresh per round, and none once it gave up');
    // A display that stays up resets the count; one that keeps dropping never does.
    recovery.stable();
    recovery.ready();
    assert.equal((await recovery.onClose(4401, 'ticket_invalid', { opened: true, liveSocket: false })).delayMs, 0);
  }
  {
    // An older backend's refusal (1006 before open) is tried as a sign-in once, then as the network.
    const { recovery, calls } = harness({ verdicts: ['unchanged'] });
    assert.equal((await recovery.onClose(1006, '', { opened: false, liveSocket: false })).delayMs, 0);
    const second = await recovery.onClose(1006, '', { opened: false, liveSocket: false });
    assert.equal(second.action, 'retry');
    assert.equal(second.delayMs, 2000, 'the sign-in try counted as the first attempt');
    assert.equal(calls.refresh, 1);
  }
  {
    // A dropped connection backs off five times and then gives up, offering Reconnect.
    const { recovery } = harness();
    const delays = [];
    for (let i = 0; i < 5; i++) {
      const step = await recovery.onClose(1006, '', { opened: true, liveSocket: true });
      assert.equal(step.action, 'retry');
      delays.push(step.delayMs);
      assert.equal(step.message, `Connection lost. Reconnecting in ${Math.round(step.delayMs / 1000)}s (attempt ${i + 1} of 5).`);
    }
    assert.deepEqual(delays, [1000, 2000, 4000, 8000, 16000]);
    assert.deepEqual(await recovery.onClose(1011, 'Lost the connection to the shell', { opened: true, liveSocket: true }),
      { action: 'stop', message: 'Could not reconnect. Select Reconnect to try again.', retry: true });
    // A session that stays up counts from the first attempt again; the operator's Reconnect too.
    recovery.stable();
    assert.equal((await recovery.onClose(1006, '', { opened: true, liveSocket: true })).delayMs, 1000);
    recovery.restart();
    assert.equal(recovery.used, 0);
  }
  {
    // The relay's own endings are the caller's to handle.
    const { recovery } = harness();
    for (const [code, kind] of [[1000, 'normal'], [4404, 'unavailable'], [4409, 'taken_over'], [1012, 'powered_down']]) {
      assert.deepEqual(await recovery.onClose(code, '', { opened: true, liveSocket: true }), { action: 'own', kind });
    }
  }
  // The SPICE session calls ready() once the guest is on screen, never when only the main channel
  // linked: the other channels present their own tickets and may still be refused.
  const spice = read('frontend/src/lib/spice/session.ts');
  const onsuccess = spice.slice(spice.indexOf('onsuccess: () =>'), spice.indexOf('onagent: () =>'));
  const firstDraw = spice.slice(spice.indexOf('sc.onfirstdraw = () =>'), spice.indexOf('sc.onmousemode'));
  assert.ok(onsuccess && !/recovery\.ready\(/.test(onsuccess), 'spice/session.ts: ready() on a linked main channel lets a refused channel loop');
  assert.ok(/this\.recovery\.ready\(\)/.test(firstDraw), 'spice/session.ts: ready() belongs on the first frame');
} finally {
  cleanup();
}

console.log(`ws close contract: ${Object.keys(backend).length} codes and ${allBackendReasons.size} reasons match; the page's decisions hold`);
