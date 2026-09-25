#!/usr/bin/env node
// Every structural guard `make test-frontend` runs must also run in CI, and the other way round.
//
// The two lists are maintained by hand in two files. They drifted once: CI ran 39 guards and the
// Makefile 40, so a regression the missing guard catches would pass CI and fail only on a laptop.

import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const guards = (text) =>
  new Set([...text.matchAll(/node scripts\/checks\/([A-Za-z0-9._-]+\.mjs)/g)].map((m) => m[1]));

const make = guards(readFileSync(join(root, "Makefile"), "utf8"));
const ci = guards(readFileSync(join(root, ".github/workflows/ci.yml"), "utf8"));
const onlyMake = [...make].filter((g) => !ci.has(g)).sort();
const onlyCi = [...ci].filter((g) => !make.has(g)).sort();

if (onlyMake.length || onlyCi.length) {
  if (onlyMake.length) console.error(`run by make test-frontend but not by CI: ${onlyMake.join(", ")}`);
  if (onlyCi.length) console.error(`run by CI but not by make test-frontend: ${onlyCi.join(", ")}`);
  process.exit(1);
}
console.log(`guard parity: ${make.size} guards run by both make test-frontend and CI`);
