/**
 * Snapshot rows follow the operator's action instead of the next fleet read.
 *
 * The fleet read behind the Snapshots page lists every cluster and then measures sizes, so it
 * takes seconds. A delete therefore takes its row out of every cached snapshot list as soon as
 * the cluster accepts it, and a list read that started before the delete settled cannot put it
 * back; a create replaces its own cluster and project inside the fleet list from the narrow read
 * of that one slice. This is the sibling of the instance pending deletes in
 * lib/instance-reconciliation.ts and uses the same clock and lifetimes.
 */
import type { QueryClient, QueryKey } from "@tanstack/react-query";

import { api, isAuthActorSynchronized } from "@/lib/api";
import { readClock } from "@/lib/instance-reconciliation";
import type { FleetSnapshotListResponse, SnapshotInfo } from "@/types/api";

export interface SnapshotIdentity {
  actorScope: string;
  host: string;
  project: string;
  /** The instance the snapshot belongs to (`source_name` on a row). */
  instance: string;
  name: string;
}

interface PendingSnapshotDelete {
  /** Which delete owns the entry, so a failed second delete cannot clear the first one's. */
  token: number;
  /** Reads that started before this may not show the row; undefined while the cluster works. */
  settledAt?: number;
  expiresAt: number;
}

type SnapshotList = { items: SnapshotInfo[]; total: number };

const RUNNING_MS = 10 * 60_000;
const SETTLED_MS = 2 * 60_000;
const pendingDeletes = new WeakMap<QueryClient, Map<string, PendingSnapshotDelete>>();
let nextToken = 0;

const projectOf = (project?: string | null): string => project || "default";

const keyOf = (identity: SnapshotIdentity): string =>
  JSON.stringify([identity.actorScope, identity.host, identity.project, identity.instance, identity.name]);

function entriesFor(queryClient: QueryClient, now: number): Map<string, PendingSnapshotDelete> {
  let entries = pendingDeletes.get(queryClient);
  if (!entries) {
    entries = new Map();
    pendingDeletes.set(queryClient, entries);
  }
  for (const [key, entry] of entries) {
    if (entry.expiresAt <= now) entries.delete(key);
  }
  return entries;
}

function isHidden(queryClient: QueryClient, identity: SnapshotIdentity, readStartedAt: number): boolean {
  const entry = entriesFor(queryClient, readClock()).get(keyOf(identity));
  if (!entry) return false;
  return entry.settledAt === undefined || readStartedAt < entry.settledAt;
}

/**
 * Drop the rows a pending or just-settled delete owns from a list read that began at
 * `readStartedAt`. Fleet rows carry their own cluster and project; a per-instance list does not,
 * so its caller passes them as `where`.
 */
export function withoutDeletedSnapshots<T extends SnapshotList>(
  queryClient: QueryClient,
  actorScope: string,
  data: T,
  readStartedAt: number,
  where?: { host: string; project: string },
): T {
  if (entriesFor(queryClient, readClock()).size === 0 || !Array.isArray(data?.items)) return data;
  const items = data.items.filter((row) => !isHidden(queryClient, {
    actorScope,
    host: row.host ?? where?.host ?? "",
    project: projectOf(row.project ?? where?.project),
    instance: row.source_name,
    name: row.name,
  }, readStartedAt));
  if (items.length === data.items.length) return data;
  return { ...data, items, total: Math.max(0, data.total - (data.items.length - items.length)) };
}

/** Run one snapshot list read and filter it through the deletes it may predate. */
export async function readSnapshotList<T extends SnapshotList>(
  queryClient: QueryClient,
  actorScope: string,
  read: () => Promise<T>,
  where?: { host: string; project: string },
): Promise<T> {
  const startedAt = readClock();
  return withoutDeletedSnapshots(queryClient, actorScope, await read(), startedAt, where);
}

function holdsSnapshot(queryKey: QueryKey, identity: SnapshotIdentity): boolean {
  const [kind, a, b, c, d] = queryKey;
  if (kind === "fleetSnapshots") {
    return (a == null || a === identity.host) && (b == null || b === identity.project);
  }
  return kind === "instanceSnapshots" &&
    a === identity.actorScope && b === identity.host && c === identity.project && d === identity.instance;
}

function dropFromCachedLists(queryClient: QueryClient, identity: SnapshotIdentity): void {
  const now = readClock();
  for (const kind of ["fleetSnapshots", "instanceSnapshots"]) {
    for (const [queryKey, data] of queryClient.getQueriesData<SnapshotList>({ queryKey: [kind] })) {
      if (!data || !holdsSnapshot(queryKey, identity)) continue;
      const where = kind === "instanceSnapshots" ? { host: identity.host, project: identity.project } : undefined;
      const next = withoutDeletedSnapshots(queryClient, identity.actorScope, data, now, where);
      if (next !== data) queryClient.setQueryData(queryKey, next);
    }
  }
}

/**
 * The cluster accepted the delete: hide the row everywhere now. Returns the token a failure
 * verdict needs to give the row back. A delete the cluster already finished is settled at once.
 */
export function markSnapshotDeleted(
  queryClient: QueryClient,
  identity: SnapshotIdentity,
  { settled }: { settled: boolean },
): number {
  const now = readClock();
  nextToken += 1;
  entriesFor(queryClient, now).set(keyOf(identity), {
    token: nextToken,
    settledAt: settled ? now : undefined,
    expiresAt: now + (settled ? SETTLED_MS : RUNNING_MS),
  });
  dropFromCachedLists(queryClient, identity);
  return nextToken;
}

/** The cluster finished (or its verdict could not be read): reads that start from now are the truth. */
export function settleSnapshotDelete(queryClient: QueryClient, identity: SnapshotIdentity): void {
  const now = readClock();
  const entries = entriesFor(queryClient, now);
  const current = entries.get(keyOf(identity));
  if (!current) return;
  entries.set(keyOf(identity), { ...current, settledAt: now, expiresAt: now + SETTLED_MS });
}

/** The cluster refused the delete after accepting it: stop hiding the row, unless another delete owns it. */
export function clearSnapshotDelete(queryClient: QueryClient, identity: SnapshotIdentity, token: number): void {
  const entries = pendingDeletes.get(queryClient);
  const current = entries?.get(keyOf(identity));
  if (current && current.token === token) entries!.delete(keyOf(identity));
}

/**
 * A snapshot was created on `host`/`project`: read that one slice (one cluster call and the sizes
 * of that project's holders) and put it in place of the same slice in every fleet-wide list.
 *
 * important: the slice comes from the caller's own read of that cluster and project, so the rows
 * merged are only rows that caller may see. A refused or failed read merges nothing; the regular
 * fleet refetch still follows.
 */
export async function refreshSnapshotSlice(
  queryClient: QueryClient,
  actorScope: string,
  host: string,
  project: string,
): Promise<void> {
  let slice: FleetSnapshotListResponse;
  try {
    slice = await queryClient.fetchQuery({
      queryKey: ["fleetSnapshots", host, project],
      queryFn: () => readSnapshotList(queryClient, actorScope, () => api.snapshots.listFleet(host, project)),
      staleTime: 0,
    });
  } catch {
    return;
  }
  if (!Array.isArray(slice?.items) || !isAuthActorSynchronized(actorScope)) return;
  const inSlice = (row: SnapshotInfo) => row.host === host && projectOf(row.project) === project;
  for (const [queryKey, data] of queryClient.getQueriesData<FleetSnapshotListResponse>({ queryKey: ["fleetSnapshots"] })) {
    const [, keyHost, keyProject] = queryKey;
    if (!data || !Array.isArray(data.items)) continue;
    if (keyHost != null && keyHost !== host) continue;
    if (keyProject != null && keyProject !== project) continue;
    if (keyHost === host && keyProject === project) continue;
    const kept = data.items.filter((row) => !inSlice(row));
    const added = slice.items.filter(inSlice);
    queryClient.setQueryData<FleetSnapshotListResponse>(queryKey, {
      ...data,
      items: [...kept, ...added],
      total: Math.max(0, data.total - (data.items.length - kept.length) + added.length),
    });
  }
}
