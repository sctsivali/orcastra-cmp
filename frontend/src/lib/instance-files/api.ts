import {
  API_URL,
  fetchAPI,
  fetchJSONWithTimeout,
  getAccessToken,
  recoverFrom401,
} from "@/lib/api";
import { APIError } from "@/types/api";

/** One instance's filesystem, as every call names it. */
export interface GuestTarget {
  host: string;
  project: string;
  instanceName: string;
}

export type GuestEntryType =
  | "file" | "directory" | "symlink" | "fifo" | "socket" | "char" | "block" | "unknown";

export interface GuestEntry {
  name: string;
  type: GuestEntryType;
  size: number;
  mode: number;
  uid: number | null;
  gid: number | null;
  owner: string | null;
  group: string | null;
  mtime: number | null;
  link_target: string | null;
  target_type: string | null;
}

export interface DirectoryListing {
  path: string;
  entries: GuestEntry[];
  truncated: boolean;
  dropped: number;
  stale_uploads: string[];
  free_bytes: number | null;
  total_bytes: number | null;
}

export interface EntryStat {
  entry: GuestEntry;
  realpath: string;
}

export interface SelectionSummary {
  files: number;
  folders: number;
  bytes: number;
  complete: boolean;
}

export interface TextFile {
  path: string;
  text: string;
  etag: string;
  size: number;
  read_only: boolean;
}

export type OnConflict = "refuse" | "replace";

export interface AttributeResult {
  changed: number;
  skipped: number;
  remaining: boolean;
}

function query(target: GuestTarget): string {
  return new URLSearchParams({
    host: target.host,
    project: target.project,
    instance_name: target.instanceName,
  }).toString();
}

// Paths travel in JSON bodies only. `request_security` answers 404 for a `path` query that looks
// like /etc/shadow, .ssh or anything ending .conf, which is ordinary in a guest.
function route(target: GuestTarget, name: string): string {
  return `/instance-files/${name}?${query(target)}`;
}

function refusal(status: number, body: unknown, fallback: string, retryAfter?: number): APIError {
  const detail = (body as { detail?: unknown } | undefined)?.detail;
  const message =
    typeof detail === "string"
      ? detail
      : typeof (detail as { message?: unknown } | undefined)?.message === "string"
        ? (detail as { message: string }).message
        : fallback;
  return new APIError(message, status, detail, retryAfter);
}

/** A raw request with a body that is not JSON, or an answer that is not. */
async function raw(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  fallback: string,
): Promise<{ response: Response; body?: unknown }> {
  for (let attempt = 0; ; attempt += 1) {
    const token = getAccessToken();
    const headers = new Headers(init.headers);
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const { response, body } = await fetchJSONWithTimeout(url, { ...init, headers }, timeoutMs);
    if (response.ok) return { response, body };
    if (response.status === 401 && attempt === 0 && (await recoverFrom401(init.signal))) continue;
    const retryAfter = Number(response.headers.get("Retry-After")) || undefined;
    throw refusal(response.status, body, fallback, retryAfter);
  }
}

const LONG_MS = 60_000;

export const instanceFilesAPI = {
  list: (target: GuestTarget, path: string, signal?: AbortSignal) =>
    fetchAPI<DirectoryListing>(route(target, "list"), { method: "POST", body: JSON.stringify({ path }), signal }),
  stat: (target: GuestTarget, path: string, signal?: AbortSignal) =>
    fetchAPI<EntryStat>(route(target, "stat"), { method: "POST", body: JSON.stringify({ path }), signal }),
  summarize: (target: GuestTarget, paths: string[], signal?: AbortSignal) =>
    fetchAPI<SelectionSummary>(route(target, "summarize"), { method: "POST", body: JSON.stringify({ paths }), signal }, LONG_MS),
  readText: (target: GuestTarget, path: string, signal?: AbortSignal) =>
    fetchAPI<TextFile>(route(target, "read-text"), { method: "POST", body: JSON.stringify({ path }), signal }),
  save: (target: GuestTarget, path: string, text: string, etag: string, allowInPlace = false) =>
    fetchAPI<{ path: string; etag: string; size: number }>(route(target, "write-text"), {
      method: "POST",
      body: JSON.stringify({ path, text, etag, allow_in_place: allowInPlace }),
    }),
  createFolder: (target: GuestTarget, path: string, parents = false) =>
    fetchAPI<EntryStat>(route(target, "mkdir"), { method: "POST", body: JSON.stringify({ path, parents }) }),
  createFile: (target: GuestTarget, path: string) =>
    fetchAPI<EntryStat>(route(target, "create-file"), { method: "POST", body: JSON.stringify({ path }) }),
  rename: (target: GuestTarget, source: string, destination: string, onConflict: OnConflict = "refuse") =>
    fetchAPI<EntryStat>(route(target, "rename"), {
      method: "POST",
      body: JSON.stringify({ source, target: destination, on_conflict: onConflict }),
    }),
  setMode: (target: GuestTarget, path: string, mode: number, recursive: boolean) =>
    fetchAPI<AttributeResult>(route(target, "chmod"), { method: "POST", body: JSON.stringify({ path, mode, recursive }) }, LONG_MS),
  setOwner: (target: GuestTarget, path: string, uid: number, gid: number, recursive: boolean) =>
    fetchAPI<AttributeResult>(route(target, "chown"), { method: "POST", body: JSON.stringify({ path, uid, gid, recursive }) }, LONG_MS),
  delete: (target: GuestTarget, path: string, recursive: boolean, opId?: string) =>
    fetchAPI<{ deleted: number; failed: string[]; failed_count: number; remaining: boolean }>(
      route(target, "delete"),
      { method: "POST", body: JSON.stringify({ path, recursive, op_id: opId }) },
      LONG_MS,
    ),
  purgeUploads: (target: GuestTarget, path: string) =>
    fetchAPI<{ removed: number }>(route(target, "uploads/purge"), { method: "POST", body: JSON.stringify({ path }) }),
  createDownload: (target: GuestTarget, paths: string[], archive: boolean) =>
    fetchAPI<{ url: string; expires_in: number; name: string; size: number | null }>(
      route(target, "downloads"),
      { method: "POST", body: JSON.stringify({ paths, archive }) },
      LONG_MS,
    ),
  createUpload: (target: GuestTarget, dest: string, size: number, onConflict: OnConflict) =>
    fetchAPI<{ token: string; chunk_hint: number; expires_at: number }>(route(target, "uploads"), {
      method: "POST",
      body: JSON.stringify({ dest, size, on_conflict: onConflict }),
    }),
  uploadStatus: (target: GuestTarget, token: string, signal?: AbortSignal) =>
    fetchAPI<{ temp_size: number; expires_at: number }>(route(target, "uploads/status"), {
      method: "POST",
      body: JSON.stringify({ token }),
      signal,
    }),
  finalizeUpload: (target: GuestTarget, token: string, onConflict?: OnConflict) =>
    fetchAPI<EntryStat & { already_committed: boolean }>(
      route(target, "uploads/complete"),
      { method: "POST", body: JSON.stringify({ token, on_conflict: onConflict }) },
      LONG_MS,
    ),
  abortUpload: (target: GuestTarget, token: string) =>
    fetchAPI<void>(route(target, "uploads/abort"), { method: "POST", body: JSON.stringify({ token }) }),

  /** One chunk. Resolves to the offset the server has acknowledged. */
  patchChunk: async (
    target: GuestTarget,
    token: string,
    offset: number,
    chunk: Blob,
    signal: AbortSignal,
  ): Promise<number> => {
    const { response } = await raw(
      `${API_URL}/instance-files/uploads?${query(target)}`,
      {
        method: "PATCH",
        headers: {
          "Content-Type": "application/octet-stream",
          "Upload-Token": token,
          "Upload-Offset": String(offset),
          "Upload-Length": String(chunk.size),
        },
        body: chunk,
        signal,
      },
      // important: below the proxy's 100 s deadline, which covers the body on an upload. An
      // abort here is a clean retry; the proxy's 502 would read as a backend failure.
      95_000,
      "This part of the upload failed",
    );
    return Number(response.headers.get("Upload-Offset") ?? offset + chunk.size);
  },

  uploadBundle: async (
    target: GuestTarget,
    body: Blob,
    signal: AbortSignal,
  ): Promise<{ results: { path: string; error: string | null }[] }> => {
    const { body: parsed } = await raw(
      `${API_URL}/instance-files/uploads/bundle?${query(target)}`,
      { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body, signal },
      95_000,
      "These files could not be uploaded",
    );
    return parsed as { results: { path: string; error: string | null }[] };
  },

  /** An image's bytes as a blob. Only for a preview, and only ever shown through `<img>`. */
  previewImage: async (target: GuestTarget, path: string, signal: AbortSignal): Promise<Blob> => {
    const token = getAccessToken();
    const response = await fetch(`${API_URL}/instance-files/preview-image?${query(target)}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ path }),
      signal,
    });
    if (!response.ok) {
      throw refusal(response.status, await response.json().catch(() => undefined), "This image could not be read");
    }
    return response.blob();
  },
};

/** The stable code on a refusal, for choosing what to show. */
export function fileErrorCode(error: unknown): string | undefined {
  if (!(error instanceof APIError)) return undefined;
  const detail = error.detail as { error?: unknown } | undefined;
  return typeof detail?.error === "string" ? detail.error : undefined;
}
