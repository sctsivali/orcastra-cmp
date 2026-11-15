#!/usr/bin/env node
// Structural guard: no new real host address in a tracked file.
//
// This repository is public. Code, tests and docs may show IPv4 addresses only from the example
// ranges below; anything else is usually a host that slipped in from a live system (a lab node, a
// production logging host). The literals already in the tree when this guard arrived are listed
// per file in ip-literals-baseline.json: they may stay or go, but a file may not gain one. A
// tailnet hostname (*.ts.net) is never allowed. The ranges mirror orcastra-docs'
// scripts/check_ip_literals.py so both public repositories hold the same line.
//
// Run: node scripts/checks/check-ip-literals.mjs [repo-root]
//      node scripts/checks/check-ip-literals.mjs [repo-root] --write-baseline   (after review)

import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const writeBaseline = args.includes('--write-baseline');
const root = args.find((a) => !a.startsWith('--')) ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const BASELINE = 'scripts/checks/ip-literals-baseline.json';

const ALLOWED = [
  ['0.0.0.0', 32], // bind to all interfaces
  ['127.0.0.0', 8], // loopback
  ['192.0.2.0', 24], // RFC 5737 TEST-NET-1
  ['198.51.100.0', 24], // RFC 5737 TEST-NET-2
  ['203.0.113.0', 24], // RFC 5737 TEST-NET-3
  ['10.0.0.0', 24], // generic private examples (10.0.0.5 and so on)
  ['172.17.0.0', 16], // Docker default bridge
  ['192.168.1.0', 24], // home or office LAN examples
  ['8.8.8.0', 24], // public DNS, also a "public address" case in tests
  ['8.8.4.4', 32],
  ['1.1.1.1', 32],
  ['1.0.0.1', 32],
  ['9.9.9.9', 32],
  ['1.2.3.4', 32], // obviously fake public addresses
  ['2.2.2.2', 32],
  ['255.255.255.0', 32], // a netmask, not a host
  ['255.255.255.255', 32],
];
// Whole private ranges written in CIDR form, for example in trusted proxy lists.
const ALLOWED_CIDR = new Set(['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', '127.0.0.0/8',
  '169.254.0.0/16', 'fc00::/7']);

// Four dotted numbers not inside a longer dotted run, so OIDs and versions such as 1.3.6.1.4.1 do
// not match. A trailing full stop ends a sentence and is not part of the address.
const IPV4 = /(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(\/\d{1,2})?(?!\d|\.\d)/g;
const TAILNET = /\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b/gi;

const toInt = (ip) => ip.split('.').reduce((acc, octet) => acc * 256 + Number(octet), 0);
// Division rather than bitwise masks: JavaScript's bitwise operators are signed 32-bit, which
// breaks every address whose first octet is 128 or more.
const inRange = (ip, [base, bits]) => Math.floor(toInt(ip) / 2 ** (32 - bits)) === Math.floor(toInt(base) / 2 ** (32 - bits));
const allowed = (literal, ip) => ALLOWED_CIDR.has(literal) || ALLOWED.some((net) => inRange(ip, net));

function tracked() {
  const out = execFileSync('git', ['ls-files', '-z'], { cwd: root, maxBuffer: 64 * 1024 * 1024 });
  return out.toString('utf8').split('\0').filter(Boolean);
}

const found = new Map(); // file -> Set(literal)
const tailnet = [];
for (const file of tracked()) {
  if (file === BASELINE) continue;
  let data;
  try {
    data = readFileSync(join(root, file));
  } catch {
    continue; // a tracked file deleted in the working tree
  }
  if (data.subarray(0, 8192).includes(0)) continue; // binary
  const text = data.toString('utf8');
  for (const m of text.matchAll(IPV4)) {
    const ip = m[1];
    if (ip.split('.').some((octet) => Number(octet) > 255)) continue;
    if (allowed(m[0], ip)) continue;
    if (!found.has(file)) found.set(file, new Set());
    found.get(file).add(ip);
  }
  for (const m of text.matchAll(TAILNET)) {
    tailnet.push(`${file}:${text.slice(0, m.index).split('\n').length}: ${m[0]}`);
  }
}

if (writeBaseline) {
  const out = Object.fromEntries([...found.entries()].sort().map(([file, ips]) => [file, [...ips].sort()]));
  writeFileSync(join(root, BASELINE), `${JSON.stringify(out, null, 1)}\n`);
  console.log(`ip literals: baseline written for ${found.size} files`);
  process.exit(0);
}

let baseline = {};
try {
  baseline = JSON.parse(readFileSync(join(root, BASELINE), 'utf8'));
} catch (error) {
  console.error(`ip literals: cannot read ${BASELINE}: ${error.message}`);
  process.exit(1);
}

const offenders = [];
for (const [file, ips] of found) {
  const known = new Set(baseline[file] ?? []);
  for (const ip of ips) if (!known.has(ip)) offenders.push(`${file}: ${ip} is outside the example ranges`);
}
const stale = [];
for (const [file, ips] of Object.entries(baseline)) {
  const now = found.get(file) ?? new Set();
  for (const ip of ips) if (!now.has(ip)) stale.push(`${file}: ${ip}`);
}
for (const hit of tailnet) offenders.push(`${hit} is a tailnet hostname`);

if (offenders.length || stale.length) {
  if (offenders.length) {
    console.error('ip literals: FAILED\n');
    for (const o of offenders) console.error(`  - ${o}`);
    console.error('\nUse an address from an example range (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 10.0.0.0/24) or a placeholder.');
  }
  if (stale.length) {
    console.error('\nip literals: the baseline lists literals the tree no longer has; drop them from ' +
      `${BASELINE} (or rerun with --write-baseline after review):`);
    for (const s of stale) console.error(`  - ${s}`);
  }
  process.exit(1);
}

const grandfathered = Object.values(baseline).reduce((n, ips) => n + ips.length, 0);
console.log(`ip literals: no new address outside the example ranges, no tailnet host (${grandfathered} grandfathered in ${Object.keys(baseline).length} files)`);
