#!/usr/bin/env node
/** Runtime and structural contract for durable image and ISO import completion. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(
  process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."),
);
const requireFromFrontend = createRequire(join(root, "frontend/package.json"));
const ts = requireFromFrontend("typescript");
const read = (path) => readFileSync(join(root, path), "utf8");
const failures = [];

function requireText(path, source, snippets) {
  for (const snippet of snippets) {
    if (!source.includes(snippet)) failures.push(`${path} is missing ${JSON.stringify(snippet)}`);
  }
}

class APIError extends Error {
  constructor(message, status, detail, retryAfter) {
    super(message);
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
  }
}

const helperPath = "frontend/src/lib/upload-terminal.ts";
const helperSource = read(helperPath);
const helperOutput = ts.transpileModule(helperSource, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: helperPath,
}).outputText;
const helperModule = { exports: {} };
new Function("require", "module", "exports", helperOutput)(
  (specifier) => {
    if (specifier === "@/types/api") return { APIError };
    throw new Error(`Unexpected runtime import in upload terminal helper: ${specifier}`);
  },
  helperModule,
  helperModule.exports,
);

const {
  followUploadToTerminal,
  UploadOutcomeUnknown,
  UploadTerminalFailure,
} = helperModule.exports;
const status = (state, error = null) => ({ status: state, error });

let reads = 0;
const initialReady = await followUploadToTerminal({
  signal: new AbortController().signal,
  initialStatus: status("ready"),
  readStatus: async () => {
    reads += 1;
    return status("ready");
  },
  pollMs: 0,
  maxPollMs: 100,
  unknownMessage: "unknown",
});
assert.equal(initialReady.status, "ready");
assert.equal(reads, 0, "an authoritative ready response must not trigger another read");

await assert.rejects(
  () => followUploadToTerminal({
    signal: new AbortController().signal,
    initialStatus: status("failed", "daemon refused import"),
    readStatus: async () => status("ready"),
    pollMs: 0,
    maxPollMs: 100,
    unknownMessage: "unknown",
  }),
  (error) => error instanceof UploadTerminalFailure && error.message === "daemon refused import",
);

let rateLimitedReads = 0;
const afterRateLimit = await followUploadToTerminal({
  signal: new AbortController().signal,
  readStatus: async () => {
    rateLimitedReads += 1;
    if (rateLimitedReads === 1) throw new APIError("slow down", 429, null, 0);
    return status("ready");
  },
  pollMs: 0,
  maxPollMs: 100,
  maxTransientFailures: 1,
  unknownMessage: "unknown",
});
assert.equal(afterRateLimit.status, "ready");
assert.equal(rateLimitedReads, 2, "429 must respect backpressure without consuming retry budget");

let transientReads = 0;
await assert.rejects(
  () => followUploadToTerminal({
    signal: new AbortController().signal,
    readStatus: async () => {
      transientReads += 1;
      throw new Error("network reset");
    },
    pollMs: 0,
    maxPollMs: 100,
    maxTransientFailures: 2,
    unknownMessage: "transient budget exhausted",
  }),
  (error) => error instanceof UploadOutcomeUnknown && error.message === "transient budget exhausted",
);
assert.equal(transientReads, 2);

await assert.rejects(
  () => followUploadToTerminal({
    signal: new AbortController().signal,
    readStatus: (signal) => new Promise((resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    }),
    pollMs: 0,
    maxPollMs: 10,
    unknownMessage: "deadline reached",
  }),
  (error) => error instanceof UploadOutcomeUnknown && error.message === "deadline reached",
);

for (const [path, apiName, impact] of [
  ["frontend/src/components/image-upload-dialog.tsx", "imageUploads", "image.upload"],
  ["frontend/src/components/iso-upload-dialog.tsx", "isoUploads", "iso.upload"],
]) {
  const source = read(path);
  requireText(path, source, [
    "onAuthActorChanged(() => {",
    "actorEpochRef.current += 1",
    "actorEpochRef.current === requestActorEpoch",
    "isAuthActorSynchronized(requestScope)",
    "sessionRef.current = null",
    "sessionRef.current.abortable = false",
    "signal: controller.signal",
    "actorScope: requestScope",
    "followUploadToTerminal({",
    "UploadTerminalFailure",
    "UploadOutcomeUnknown",
    `reconcileMutationImpact(queryClient, "${impact}")`,
  ]);
  const finalizeCalls = source.match(new RegExp(`api\\.${apiName}\\.finalize\\(`, "g")) ?? [];
  if (finalizeCalls.length !== 1) {
    failures.push(`${path} must contain exactly one finalize call; found ${finalizeCalls.length}`);
  }
}

const apiPath = "frontend/src/lib/api.ts";
const apiSource = read(apiPath);
requireText(apiPath, apiSource, [
  "interface UploadRequestOptions",
  "assertUploadActor(actorScope)",
  "if (opts.actorScope && !isAuthActorSynchronized(opts.actorScope)) return",
  "parseRetryAfter(res.headers.get(\"Retry-After\"))",
  "signal: opts.signal",
  "300_000",
]);
if ((apiSource.match(/fetchJSONWithTimeout\(\n\s*`\$\{(?:ISO|IMAGE)_UPLOAD_BASE\}\/\$\{sessionId\}`/g) ?? []).length !== 2) {
  failures.push("both binary chunk transports must keep caller cancellation and a timeout");
}

if (failures.length) {
  console.error("Upload terminal durability check failed:\n");
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}

console.log("Upload terminal durability check passed.");
