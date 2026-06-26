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
 * from the create page to the list page.
 */

export interface PendingCreate {
  /** Stable key: `${host}/${project}/${name}`. */
  id: string;
  name: string;
  host: string;
  project: string;
  /** "container" | "virtual-machine" */
  type: string;
  operationId?: string;
  description?: string;
  status?: string;
  statusCode?: number;
  /** LXD image-pull progress, e.g. "rootfs: 48%". */
  progress?: string;
  error?: string;
  createdAt: number;
}

type Listener = () => void;

let pending: PendingCreate[] = [];
const listeners = new Set<Listener>();

function emit() {
  for (const l of listeners) l();
}

export function subscribePending(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getPendingSnapshot(): PendingCreate[] {
  return pending;
}

export function keyFor(host: string, project: string, name: string): string {
  return `${host}/${project || "default"}/${name}`;
}

export function addPendingCreate(
  input: Omit<PendingCreate, "id" | "createdAt"> & { createdAt?: number },
): void {
  const id = keyFor(input.host, input.project, input.name);
  const entry: PendingCreate = {
    ...input,
    id,
    status: input.status ?? "Pending",
    createdAt: input.createdAt ?? Date.now(),
  };
  // Replace any existing entry with the same key, keep newest first.
  pending = [entry, ...pending.filter((p) => p.id !== id)];
  emit();
}

export function updatePendingCreate(id: string, patch: Partial<PendingCreate>): void {
  let changed = false;
  pending = pending.map((p) => {
    if (p.id !== id) return p;
    changed = true;
    return { ...p, ...patch };
  });
  if (changed) emit();
}

export function removePendingCreate(id: string): void {
  const next = pending.filter((p) => p.id !== id);
  if (next.length !== pending.length) {
    pending = next;
    emit();
  }
}
