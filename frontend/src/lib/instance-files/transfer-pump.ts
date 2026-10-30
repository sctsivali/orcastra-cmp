import {
  captureExternalMutationContext,
  reconcileExternalMutation,
} from "@/lib/mutation-coherence";
import { APIError } from "@/types/api";
import { fileErrorCode, instanceFilesAPI, type GuestTarget, type OnConflict } from "./api";
import type { PickedTree } from "./drop-entries";
import { joinPath, parentOf } from "./path";
import { clearManifest, forgetUpload, readManifest, rememberUpload } from "./transfer-manifest";
import {
  isActive,
  onTransfersReset,
  transferFiles,
  useTransferStore,
  type Transfer,
} from "./transfer-store";

const PARALLEL_TRANSFERS = 3;
const MIN_CHUNK = 256 * 1024;
const MAX_CHUNK = 16 * 1024 * 1024;
const START_CHUNK = 2 * 1024 * 1024;
// important: the proxy's 100 s deadline covers the request body, so a chunk sized for a fast link
// would fail on a slow one. Aiming each at 15 s leaves room for a slower moment.
const TARGET_CHUNK_SECONDS = 15;
const MAX_ATTEMPTS = 6;
const BUNDLE_FILE_BYTES = 2 * 1024 * 1024;
const BUNDLE_MAX_BYTES = 8 * 1024 * 1024;
const BUNDLE_MAX_FILES = 200;
const PROGRESS_INTERVAL_MS = 250;

const FATAL = new Set([
  "no_space", "permission_denied", "protected_path", "read_only_fs", "invalid_path", "too_large",
  "not_a_directory", "is_a_directory", "upload_session_lost", "size_mismatch", "offset_conflict",
  "break_glass_required", "feature_disabled", "operation_not_permitted",
]);

const controllers = new Map<string, AbortController>();
let running = 0;
let reconcileTimer: ReturnType<typeof setTimeout> | undefined;
let externalContext = captureExternalMutationContext();

onTransfersReset(() => {
  clearManifest();
  controllers.forEach((controller) => controller.abort());
  controllers.clear();
  if (reconcileTimer) clearTimeout(reconcileTimer);
  reconcileTimer = undefined;
});

function store() {
  return useTransferStore.getState();
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(new DOMException("Cancelled", "AbortError"));
    }, { once: true });
  });
}

function isRetryable(error: unknown): boolean {
  if (error instanceof APIError) {
    const code = fileErrorCode(error);
    if (code && FATAL.has(code)) return false;
    return error.status === 429 || error.status >= 500;
  }
  return error instanceof TypeError;
}

function backoffMs(error: unknown, attempt: number): number {
  const asked = error instanceof APIError ? error.retryAfter : undefined;
  return asked ? asked * 1000 : Math.min(1000 * 2 ** attempt, 30_000);
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : "This upload failed";
}

/** One listing refresh for a burst of finishing files, so a folder does not refetch per file. */
function scheduleReconcile(): void {
  if (reconcileTimer) return;
  reconcileTimer = setTimeout(() => {
    reconcileTimer = undefined;
    void reconcileExternalMutation("instance-files.content", {}, externalContext).catch(() => undefined);
  }, 1500);
}

async function uploadChunks(transfer: Transfer, file: File, signal: AbortSignal): Promise<void> {
  let token = transfer.token;
  let offset = transfer.sent;
  if (!token) {
    const created = await instanceFilesAPI.createUpload(transfer.target, transfer.dest, file.size, transfer.onConflict);
    token = created.token;
    offset = 0;
    store().patch(transfer.id, { token });
  }
  rememberUpload({ ...transfer, token, sent: offset, lastModified: file.lastModified });
  let chunkSize = START_CHUNK;
  let attempt = 0;
  let lastPaint = 0;
  while (offset < file.size) {
    const start = offset;
    const end = Math.min(start + chunkSize, file.size);
    const started = performance.now();
    try {
      offset = await instanceFilesAPI.patchChunk(transfer.target, token, offset, file.slice(start, end), signal);
      attempt = 0;
    } catch (error) {
      if (signal.aborted) throw error;
      if (attempt >= MAX_ATTEMPTS || !isRetryable(error)) throw error;
      attempt += 1;
      // A slower link is the likely reason, so the retry asks less of it.
      chunkSize = Math.max(MIN_CHUNK, Math.floor(chunkSize / 2));
      await sleep(backoffMs(error, attempt), signal);
      continue;
    }
    const seconds = Math.max((performance.now() - started) / 1000, 0.05);
    const bytesPerSecond = (end - start) / seconds;
    chunkSize = Math.min(MAX_CHUNK, Math.max(MIN_CHUNK, Math.floor(bytesPerSecond * TARGET_CHUNK_SECONDS)));
    rememberUpload({ ...transfer, token, sent: offset, lastModified: file.lastModified });
    if (performance.now() - lastPaint >= PROGRESS_INTERVAL_MS) {
      lastPaint = performance.now();
      store().patch(transfer.id, { sent: offset, rate: bytesPerSecond });
    }
  }
  store().patch(transfer.id, { sent: file.size, state: "finishing" });
  for (attempt = 0; ; attempt += 1) {
    try {
      await instanceFilesAPI.finalizeUpload(transfer.target, token, transfer.onConflict);
      break;
    } catch (error) {
      if (fileErrorCode(error) === "already_exists") {
        store().patch(transfer.id, { state: "conflict", error: "Something with this name appeared during the upload." });
        forgetUpload(transfer.id);
        return;
      }
      if (attempt >= MAX_ATTEMPTS || !isRetryable(error)) throw error;
      await sleep(backoffMs(error, attempt), signal);
    }
  }
  store().patch(transfer.id, { state: "done" });
  forgetUpload(transfer.id);
  scheduleReconcile();
}

async function runTransfer(transfer: Transfer): Promise<void> {
  const controller = new AbortController();
  controllers.set(transfer.id, controller);
  const file = transferFiles.get(transfer.id);
  store().patch(transfer.id, { state: "uploading", startedAt: Date.now(), error: undefined, errorCode: undefined });
  try {
    if (!file) throw new Error("The file is no longer available in this page.");
    await uploadChunks(store().transfers.find((item) => item.id === transfer.id) ?? transfer, file, controller.signal);
  } catch (error) {
    if (controller.signal.aborted) return;
    if (fileErrorCode(error) === "already_exists") {
      store().patch(transfer.id, { state: "conflict", error: "Something with this name already exists." });
    } else {
      store().patch(transfer.id, { state: "failed", error: messageOf(error), errorCode: fileErrorCode(error) });
    }
  } finally {
    controllers.delete(transfer.id);
  }
}

interface BundleItem {
  transfer: Transfer;
  file: File;
  relative: string;
}

async function runBundle(target: GuestTarget, base: string, items: BundleItem[], folders: string[], onConflict: OnConflict): Promise<void> {
  const controller = new AbortController();
  items.forEach(({ transfer }) => controllers.set(transfer.id, controller));
  items.forEach(({ transfer }) => store().patch(transfer.id, { state: "uploading", startedAt: Date.now() }));
  const manifest = new TextEncoder().encode(
    JSON.stringify({
      base,
      on_conflict: onConflict,
      folders,
      files: items.map(({ file, relative }) => ({ path: relative, size: file.size })),
    }),
  );
  const prefix = new Uint8Array(4);
  new DataView(prefix.buffer).setUint32(0, manifest.length);
  const body = new Blob([prefix, manifest, ...items.map(({ file }) => file)], { type: "application/octet-stream" });
  try {
    for (let attempt = 0; ; attempt += 1) {
      try {
        const { results } = await instanceFilesAPI.uploadBundle(target, body, controller.signal);
        items.forEach(({ transfer, relative }, index) => {
          const error = results.find((row) => row.path === relative)?.error ?? results[index]?.error ?? null;
          if (error === "already_exists") {
            store().patch(transfer.id, { state: "conflict", error: "Something with this name already exists." });
          } else if (error) {
            store().patch(transfer.id, { state: "failed", error: "This file could not be written.", errorCode: error });
          } else {
            store().patch(transfer.id, { state: "done", sent: transfer.size });
          }
        });
        scheduleReconcile();
        return;
      } catch (error) {
        if (controller.signal.aborted) return;
        if (attempt >= MAX_ATTEMPTS || !isRetryable(error)) throw error;
        await sleep(backoffMs(error, attempt), controller.signal);
      }
    }
  } catch (error) {
    if (!controller.signal.aborted) {
      items.forEach(({ transfer }) =>
        store().patch(transfer.id, { state: "failed", error: messageOf(error), errorCode: fileErrorCode(error) }),
      );
    }
  } finally {
    items.forEach(({ transfer }) => controllers.delete(transfer.id));
  }
}

interface Work {
  run: () => Promise<void>;
}

const queue: Work[] = [];

function pump(): void {
  while (running < PARALLEL_TRANSFERS && queue.length > 0) {
    const work = queue.shift();
    if (!work) return;
    running += 1;
    void work.run().finally(() => {
      running -= 1;
      pump();
    });
  }
}

let nextId = 0;

function newId(): string {
  nextId += 1;
  return `${Date.now().toString(36)}-${nextId}`;
}

function describe(target: GuestTarget, dest: string, size: number, batch: string, onConflict: OnConflict): Transfer {
  return {
    id: newId(), target, name: dest.slice(dest.lastIndexOf("/") + 1), dest, size, sent: 0,
    state: "queued", onConflict, rate: 0, batch,
  };
}

/** Upload a tree into `folder`. Small files share requests, because a folder must not cost one per file. */
export function enqueueUploads(
  target: GuestTarget,
  folder: string,
  tree: PickedTree,
  onConflict: OnConflict,
  rename: ReadonlyMap<string, string> = new Map(),
): void {
  externalContext = captureExternalMutationContext();
  const batch = newId();
  const small: BundleItem[] = [];
  const created: Transfer[] = [];
  for (const { file, relativePath } of tree.files) {
    const relative = rename.get(relativePath) ?? relativePath;
    const transfer = describe(target, joinPath(folder, relative), file.size, batch, onConflict);
    transferFiles.set(transfer.id, file);
    created.push(transfer);
    if (file.size <= BUNDLE_FILE_BYTES) small.push({ transfer, file, relative });
    else queue.push({ run: () => runTransfer(transfer) });
  }
  store().add(created);

  const folders = [...tree.folders];
  let group: BundleItem[] = [];
  let bytes = 0;
  const flush = () => {
    const items = group;
    const pending = folders.splice(0, 200);
    group = [];
    bytes = 0;
    if (items.length || pending.length) {
      queue.unshift({ run: () => runBundle(target, folder, items, pending, onConflict) });
    }
  };
  for (const item of small) {
    if (group.length >= BUNDLE_MAX_FILES || bytes + item.file.size > BUNDLE_MAX_BYTES) flush();
    group.push(item);
    bytes += item.file.size;
  }
  flush();
  while (folders.length) flush();
  pump();
}

export function cancelTransfer(id: string): void {
  const transfer = store().transfers.find((item) => item.id === id);
  if (!transfer) return;
  controllers.get(id)?.abort();
  store().patch(id, { state: "cancelled" });
  forgetUpload(id);
  if (transfer.token) void instanceFilesAPI.abortUpload(transfer.target, transfer.token).catch(() => undefined);
}

export function cancelAll(): void {
  store().transfers.filter(isActive).forEach((transfer) => cancelTransfer(transfer.id));
}

export function retryTransfer(id: string, onConflict?: OnConflict): void {
  const transfer = store().transfers.find((item) => item.id === id);
  if (!transfer) return;
  const change: Partial<Transfer> = { state: "queued", error: undefined, errorCode: undefined };
  if (onConflict) change.onConflict = onConflict;
  // A commit that met a conflict keeps its bytes: the choice only changes how it lands.
  if (transfer.errorCode === "upload_session_lost" || !transfer.token) {
    change.token = undefined;
    change.sent = 0;
  }
  store().patch(id, change);
  const current = { ...transfer, ...change };
  queue.push({ run: () => runTransfer(current) });
  pump();
}

export function resumeWithFile(id: string, file: File): boolean {
  const entry = readManifest().find((item) => item.id === id);
  if (!entry || entry.name !== file.name || entry.size !== file.size || entry.lastModified !== file.lastModified) {
    return false;
  }
  transferFiles.set(id, file);
  retryTransfer(id);
  return true;
}

export function restoreInterrupted(): void {
  const known = new Set(store().transfers.map((item) => item.id));
  const restored = readManifest().filter((item) => !known.has(item.id));
  if (restored.length) {
    store().add(restored.map(({ lastModified: _modified, actor: _actor, ...item }) => ({ ...item, state: "interrupted", rate: 0 })));
  }
}

export function discardInterrupted(id: string): void {
  const transfer = store().transfers.find((item) => item.id === id);
  forgetUpload(id);
  store().remove([id]);
  if (transfer?.token) void instanceFilesAPI.abortUpload(transfer.target, transfer.token).catch(() => undefined);
}

export function destinationFolder(transfer: Transfer): string {
  return parentOf(transfer.dest);
}
