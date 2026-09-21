/**
 * Pending instance-creation store.
 *
 * A tiny module-level store (consumed via useSyncExternalStore) that tracks
 * instances which have been submitted for creation but haven't materialized in
 * the instance list yet. The create form seeds an entry on submit; the
 * operation-events stream updates its live status/progress; the instances list
 * renders it as a "Creating… 48%" placeholder row until the real instance
 * appears (or the operation fails).
 *
 * Kept outside React (and outside React Query) so it survives the navigation
 * from the create page to the list page. Entries and subscriptions are scoped
 * by authenticated actor because this module also survives sign-out/sign-in
 * transitions in the same browser document.
 */
import { requestRowFocus } from "@/lib/row-focus";

export interface PendingCreate {
  /** Stable key: `${host}/${project}/${name}`. */
  id: string;
  name: string;
  host: string;
  project: string;
  /** "container" | "virtual-machine" */
  type: string;
  /** Durable backend job used for exact polling across navigation and reload. */
  jobId?: string;
  operationId?: string;
  phase?: string;
  jobStatus?: "pending" | "running" | "success" | "failure" | "partial" | "unknown";
  /** A permanent operation-read refusal stops polling without hiding the operator's row. */
  pollingHalted?: boolean;
  description?: string;
  status?: string;
  statusCode?: number;
  /** LXD image-pull progress, e.g. "rootfs: 48%". */
  progress?: string;
  error?: string;
  createdAt: number;
  /** Latest authorized job or daemon signal. Memory-only, never persisted. */
  lastSignalAt?: number;
}

/**
 * How long a create may sit with no signal before the row stops claiming to be in progress.
 *
 * important: this store had no expiry at all, and that is half of the stuck-Creating defect. A placeholder
 * is cleared by an operation frame, a lifecycle frame, or the instance appearing in a list.
 * A create that LXD refused in the background produces none of the three, and the events feed
 * is subscribed without a project so a create into a non-default project may not produce them
 * either. So the row spun on "Creating..." indefinitely and the operator was told nothing.
 *
 * Ten minutes is above a slow remote image pull, which is the longest legitimate wait, so this
 * expires a create that has genuinely lost its signal rather than one that is merely slow.
 */
export const PENDING_STALE_MS = 10 * 60 * 1000;

/** Whether this placeholder has waited past the point where it can still be believed. */
export function isStalePending(
  p: Pick<PendingCreate, "createdAt" | "lastSignalAt" | "status" | "jobStatus">,
  now: number = Date.now(),
): boolean {
  if (
    p.status === "Error" ||
    p.jobStatus === "failure" ||
    p.jobStatus === "partial" ||
    p.jobStatus === "unknown"
  ) return false;
  return now - (p.lastSignalAt ?? p.createdAt) > PENDING_STALE_MS;
}

type Listener = () => void;

const EMPTY_PENDING: PendingCreate[] = [];
const pendingByActor = new Map<string, PendingCreate[]>();
const listenersByActor = new Map<string, Set<Listener>>();
const hydratedActors = new Set<string>();
const STORAGE_PREFIX = "orcastra.pending-creates.v1";

function actorStorageKey(actorScope: string): string {
  // Keep email/group names out of an inspectable storage key. This is isolation, not a
  // security hash: backend authorization still owns every read of the referenced job.
  let hash = 2166136261;
  for (let index = 0; index < actorScope.length; index += 1) {
    hash ^= actorScope.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `${STORAGE_PREFIX}.${(hash >>> 0).toString(16)}`;
}

function persist(actorScope: string, entries: PendingCreate[]): void {
  if (typeof window === "undefined") return;
  try {
    const key = actorStorageKey(actorScope);
    if (entries.length === 0) {
      window.sessionStorage.removeItem(key);
      return;
    }
    // Explicit projection prevents a later form field from being persisted by accident.
    const safe = entries.map((entry) => ({
      id: entry.id,
      name: entry.name,
      host: entry.host,
      project: entry.project,
      type: entry.type,
      jobId: entry.jobId,
      createdAt: entry.createdAt,
    }));
    window.sessionStorage.setItem(key, JSON.stringify(safe));
  } catch {
    // Private browsing or a full storage quota must not break the in-memory workflow.
  }
}

function hydrate(actorScope: string): void {
  if (hydratedActors.has(actorScope)) return;
  hydratedActors.add(actorScope);
  if (typeof window === "undefined") return;
  try {
    const raw = window.sessionStorage.getItem(actorStorageKey(actorScope));
    if (!raw) return;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return;
    const entries = parsed.flatMap((candidate): PendingCreate[] => {
      if (!candidate || typeof candidate !== "object") return [];
      const item = candidate as Partial<PendingCreate>;
      if (
        typeof item.name !== "string" ||
        typeof item.host !== "string" ||
        typeof item.project !== "string" ||
        typeof item.type !== "string" ||
        typeof item.createdAt !== "number" ||
        (item.jobId !== undefined && typeof item.jobId !== "string")
      ) return [];
      return [{
        id: keyFor(item.host, item.project, item.name),
        name: item.name,
        host: item.host,
        project: item.project,
        type: item.type,
        jobId: item.jobId,
        createdAt: item.createdAt,
        // Reload recovery immediately polls a durable job. Avoid a false stale flash while
        // that authorized read is in flight, without persisting a browser heartbeat.
        lastSignalAt: item.jobId ? Date.now() : item.createdAt,
      }];
    });
    if (entries.length > 0) pendingByActor.set(actorScope, entries);
  } catch {
    // A malformed or blocked session store is equivalent to no saved operations.
  }
}

function emit(actorScope: string) {
  for (const listener of listenersByActor.get(actorScope) ?? []) listener();
}

export function subscribePending(actorScope: string, listener: Listener): () => void {
  const listeners = listenersByActor.get(actorScope) ?? new Set<Listener>();
  listeners.add(listener);
  listenersByActor.set(actorScope, listeners);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) listenersByActor.delete(actorScope);
  };
}

export function getPendingSnapshot(actorScope: string): PendingCreate[] {
  hydrate(actorScope);
  return pendingByActor.get(actorScope) ?? EMPTY_PENDING;
}

export function keyFor(host: string, project: string, name: string): string {
  return `${host}/${project || "default"}/${name}`;
}

export function addPendingCreate(
  actorScope: string,
  input: Omit<PendingCreate, "id" | "createdAt"> & { createdAt?: number },
): void {
  const id = keyFor(input.host, input.project, input.name);
  const createdAt = input.createdAt ?? Date.now();
  const entry: PendingCreate = {
    ...input,
    id,
    status: input.status ?? "Pending",
    createdAt,
    lastSignalAt: input.lastSignalAt ?? createdAt,
  };
  // Replace any existing entry with the same key, keep newest first.
  const pending = getPendingSnapshot(actorScope);
  const next = [entry, ...pending.filter((p) => p.id !== id)];
  pendingByActor.set(actorScope, next);
  persist(actorScope, next);
  emit(actorScope);
}

export function updatePendingCreate(
  actorScope: string,
  id: string,
  patch: Partial<PendingCreate>,
): void {
  const pending = getPendingSnapshot(actorScope);
  let changed = false;
  const next = pending.map((p) => {
    if (p.id !== id) return p;
    changed = true;
    return { ...p, ...patch };
  });
  if (changed) {
    pendingByActor.set(actorScope, next);
    persist(actorScope, next);
    emit(actorScope);
  }
}

export function removePendingCreate(actorScope: string, id: string): void {
  const pending = getPendingSnapshot(actorScope);
  const next = pending.filter((p) => p.id !== id);
  if (next.length !== pending.length) {
    if (next.length > 0) pendingByActor.set(actorScope, next);
    else pendingByActor.delete(actorScope);
    persist(actorScope, next);
    emit(actorScope);
  }
}

/**
 * The placeholder's instance exists in the list now: retire the row and ask the list to bring
 * the real row into view.
 *
 * note: a dedicated function rather than a flag on removePendingCreate, so Dismiss on a
 * failed row and the delete path keep calling removePendingCreate and cannot request focus by
 * accident. Resolution already proved the row exists, so the focus intent starts now. Reusing
 * a long-running create's original timestamp would make the five-minute focus TTL discard it.
 */
export function resolvePendingCreate(actorScope: string, id: string): void {
  const entry = getPendingSnapshot(actorScope).find((p) => p.id === id);
  if (!entry) return;
  removePendingCreate(actorScope, id);
  requestRowFocus("instances", entry.id, entry.name);
}
