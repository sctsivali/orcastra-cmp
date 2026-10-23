import type { QueryClient, QueryKey } from "@tanstack/react-query";

import { api, carryStaleMark } from "@/lib/api";
import type { VMInfo, VMListResponse } from "@/types/api";

export interface InstanceIdentity {
  actorScope: string;
  host: string;
  project: string;
  name: string;
}

export interface ExactInstanceReadLease {
  identity: InstanceIdentity;
  epoch: number;
  signal: AbortSignal;
}

interface ExactInstanceReadState {
  identity: InstanceIdentity;
  epoch: number;
  controller?: AbortController;
}

/**
 * Fence exact reads started by lifecycle events.
 *
 * A new event aborts and supersedes the previous read for the same actor/cluster/project/name.
 * Delete and project-less rename events can tombstone pending reads before they touch the cache.
 */
export function createExactInstanceReadFence() {
  const states = new Map<string, ExactInstanceReadState>();
  const keyOf = (identity: InstanceIdentity) => JSON.stringify([
    identity.actorScope,
    identity.host,
    identity.project,
    identity.name,
  ]);

  const start = (identity: InstanceIdentity): ExactInstanceReadLease => {
    const key = keyOf(identity);
    const previous = states.get(key);
    previous?.controller?.abort();
    const controller = new AbortController();
    const next = {
      identity,
      epoch: (previous?.epoch ?? 0) + 1,
      controller,
    };
    states.set(key, next);
    return { identity, epoch: next.epoch, signal: controller.signal };
  };

  const isCurrent = (lease: ExactInstanceReadLease): boolean => {
    const current = states.get(keyOf(lease.identity));
    return current?.epoch === lease.epoch && !lease.signal.aborted;
  };

  const release = (lease: ExactInstanceReadLease): void => {
    const key = keyOf(lease.identity);
    const current = states.get(key);
    if (current?.epoch === lease.epoch) current.controller = undefined;
  };

  const tombstone = (identity: InstanceIdentity): void => {
    const key = keyOf(identity);
    const previous = states.get(key);
    previous?.controller?.abort();
    states.set(key, {
      identity,
      epoch: (previous?.epoch ?? 0) + 1,
    });
  };

  const tombstoneName = (actorScope: string, host: string, name: string): void => {
    for (const state of states.values()) {
      if (
        state.identity.actorScope === actorScope &&
        state.identity.host === host &&
        state.identity.name === name
      ) {
        tombstone(state.identity);
      }
    }
  };

  const abortAll = (): void => {
    for (const state of states.values()) state.controller?.abort();
    states.clear();
  };

  return { start, isCurrent, release, tombstone, tombstoneName, abortAll };
}

export interface FleetReconcileOptions {
  fresh: boolean;
  signal?: AbortSignal;
}

export interface InstanceReconcileRateLimit {
  queuedFresh: boolean;
  retryAt: number;
}

/** Preserve only an authoritative retry across a Retry-After window. */
export function retainInstanceReconcileRateLimit(
  queuedFresh: boolean,
  requestedFresh: boolean,
  retryAvailable: boolean,
  retryAfterSeconds: number | undefined,
  now: number,
): InstanceReconcileRateLimit {
  const retrySeconds = Math.max(1, retryAfterSeconds ?? 30);
  return {
    // One request may earn one delayed retry. A retry that is itself rate limited stops unless a
    // new lifecycle/reconnect/visibility intent arrived while it was in flight.
    queuedFresh: queuedFresh || (requestedFresh && retryAvailable),
    retryAt: now + retrySeconds * 1_000,
  };
}

interface InstanceDeleteRollbackEntry {
  queryKey: QueryKey;
  row: VMInfo;
  index: number;
}

export interface InstanceDeleteRollback {
  identity: InstanceIdentity;
  entries: InstanceDeleteRollbackEntry[];
  /** The pending-delete entry this delete owns. */
  token: number;
}

const projectOf = (project?: string | null) => project || "default";

// Latest-started fleet read wins for each QueryClient and actor. Exact event patches and
// optimistic writes also advance this epoch so an older full snapshot cannot undo them.
const fleetWriteEpochs = new WeakMap<QueryClient, Map<string, number>>();

function advanceFleetWriteEpoch(queryClient: QueryClient, actorScope: string): number {
  let actors = fleetWriteEpochs.get(queryClient);
  if (!actors) {
    actors = new Map<string, number>();
    fleetWriteEpochs.set(queryClient, actors);
  }
  const next = (actors.get(actorScope) ?? 0) + 1;
  actors.set(actorScope, next);
  return next;
}

function isCurrentFleetWriteEpoch(
  queryClient: QueryClient,
  actorScope: string,
  epoch: number,
): boolean {
  return fleetWriteEpochs.get(queryClient)?.get(actorScope) === epoch;
}

interface PendingDelete {
  identity: InstanceIdentity;
  /** Which delete owns the entry, so a failed second delete cannot clear the first one's. */
  token: number;
  /** Reads that started before this may not show the row; undefined while the delete runs. */
  settledAt?: number;
  expiresAt: number;
}

// The backend holds a DELETE open until LXD has finished (up to 300 s) and clears its caches
// before answering, so a read that STARTED after the answer is the truth. Anything that started
// earlier (a lifecycle event's exact read during the force stop, a fleet poll, a stale
// follow-up, a hover prefetch) can still carry the row and must not put it back.
const PENDING_DELETE_RUNNING_MS = 10 * 60_000;
const PENDING_DELETE_SETTLED_MS = 2 * 60_000;
// An instance-deleted event can arrive before the server has cleared the cached list (the
// route clears after LXD answers; another operator, OrcaHub or the CLI may be the writer), so
// reads that start within this window after the event are still treated as predating it.
const DELETED_EVENT_GRACE_MS = 5_000;
const pendingDeletes = new WeakMap<QueryClient, Map<string, PendingDelete>>();
let nextDeleteToken = 0;

/** Monotonic milliseconds for read ordering; a wall-clock step must not hide a re-create. */
export const readClock = (): number =>
  typeof performance !== "undefined" ? performance.now() : Date.now();

const identityKey = (identity: InstanceIdentity) => JSON.stringify([
  identity.actorScope,
  identity.host,
  identity.project,
  identity.name,
]);

function pendingDeletesFor(queryClient: QueryClient, now: number): Map<string, PendingDelete> {
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

/** Start hiding the row for the delete that returns this token. */
export function markInstanceDeleting(queryClient: QueryClient, identity: InstanceIdentity): number {
  const now = readClock();
  nextDeleteToken += 1;
  pendingDeletesFor(queryClient, now).set(identityKey(identity), {
    identity,
    token: nextDeleteToken,
    expiresAt: now + PENDING_DELETE_RUNNING_MS,
  });
  return nextDeleteToken;
}

function settleAt(queryClient: QueryClient, identity: InstanceIdentity, settledAt: number): void {
  const entries = pendingDeletesFor(queryClient, readClock());
  const key = identityKey(identity);
  const current = entries.get(key);
  const latest = Math.max(settledAt, current?.settledAt ?? 0);
  entries.set(key, {
    identity,
    token: current?.token ?? ++nextDeleteToken,
    settledAt: latest,
    expiresAt: latest + PENDING_DELETE_SETTLED_MS,
  });
}

/** The DELETE answered: only reads that started before now are still pre-delete. */
export function settleInstanceDelete(queryClient: QueryClient, identity: InstanceIdentity): void {
  settleAt(queryClient, identity, readClock());
}

/** LXD reported the instance deleted, possibly before the server cleared its cached lists. */
export function noteInstanceDeletedEvent(queryClient: QueryClient, identity: InstanceIdentity): void {
  settleAt(queryClient, identity, readClock() + DELETED_EVENT_GRACE_MS);
}

/** The instance exists again (a same-name create): nothing may hide it any more. */
export function noteInstanceCreatedEvent(queryClient: QueryClient, identity: InstanceIdentity): void {
  pendingDeletes.get(queryClient)?.delete(identityKey(identity));
}

/** A failed delete stops hiding the row, unless another delete or an event owns it by now. */
export function clearInstanceDelete(
  queryClient: QueryClient,
  identity: InstanceIdentity,
  token: number,
): void {
  const entries = pendingDeletes.get(queryClient);
  const key = identityKey(identity);
  const current = entries?.get(key);
  if (current && current.token === token && current.settledAt === undefined) entries!.delete(key);
}

/** True when a read that started at `readStartedAt` (readClock) may not show this exact row. */
export function isHiddenByPendingDelete(
  queryClient: QueryClient,
  identity: InstanceIdentity,
  readStartedAt: number,
): boolean {
  const entry = pendingDeletesFor(queryClient, readClock()).get(identityKey(identity));
  if (!entry) return false;
  return entry.settledAt === undefined || readStartedAt < entry.settledAt;
}

/** Drop rows a pending or just-finished delete owns from a list read that began at `readStartedAt`. */
export function withoutPendingDeletes(
  queryClient: QueryClient,
  actorScope: string,
  data: VMListResponse,
  readStartedAt: number,
): VMListResponse {
  const entries = pendingDeletesFor(queryClient, readClock());
  if (entries.size === 0 || !Array.isArray(data?.vms)) return data;
  const vms = data.vms.filter((vm) => !isHiddenByPendingDelete(queryClient, {
    actorScope,
    host: vm.host,
    project: projectOf(vm.project),
    name: vm.name,
  }, readStartedAt));
  if (vms.length === data.vms.length) return data;
  return carryStaleMark(data, {
    ...data,
    vms,
    total: Math.max(0, data.total - (data.vms.length - vms.length)),
  });
}

/** Run one instance list read and filter it through the pending deletes it may predate. */
export async function readInstanceList(
  queryClient: QueryClient,
  actorScope: string,
  read: () => Promise<VMListResponse>,
): Promise<VMListResponse> {
  const startedAt = readClock();
  return withoutPendingDeletes(queryClient, actorScope, await read(), startedAt);
}

export function isExactInstance(vm: VMInfo, identity: InstanceIdentity): boolean {
  return (
    vm.host === identity.host &&
    projectOf(vm.project) === identity.project &&
    vm.name === identity.name
  );
}

export function instanceListHolds(queryKey: QueryKey, identity: InstanceIdentity): boolean {
  const [kind, actor, host, project] = queryKey;
  return (
    kind === "instances" &&
    actor === identity.actorScope &&
    (host === "*" || host === identity.host) &&
    (project === "*" || project === identity.project)
  );
}

// A list read cancelled by an event patch is asked again after this pause, once per key however
// many events arrive in it: a burst then costs one read instead of one per event, and the
// server has had time to clear the cache the event's write made stale.
export const CANCELLED_READ_RETRY_MS = 1_000;
const scheduledRereads = new WeakMap<QueryClient, Map<string, ReturnType<typeof setTimeout>>>();

function rereadLater(queryClient: QueryClient, queryKey: QueryKey, queryHash: string): void {
  let timers = scheduledRereads.get(queryClient);
  if (!timers) {
    timers = new Map();
    scheduledRereads.set(queryClient, timers);
  }
  clearTimeout(timers.get(queryHash));
  timers.set(queryHash, setTimeout(() => {
    timers!.delete(queryHash);
    void queryClient
      .refetchQueries({ queryKey, exact: true, type: "active" })
      .catch(() => undefined);
  }, CANCELLED_READ_RETRY_MS));
}

function patchLists(
  queryClient: QueryClient,
  identity: InstanceIdentity,
  update: (data: VMListResponse) => VMListResponse,
): void {
  const queries = queryClient.getQueryCache().findAll({
    queryKey: ["instances", identity.actorScope],
  });
  const patches: Array<[QueryKey, VMListResponse]> = [];
  for (const query of queries) {
    if (!instanceListHolds(query.queryKey, identity)) continue;
    const data = queryClient.getQueryData<VMListResponse>(query.queryKey);
    if (!data?.vms) continue;
    const next = update(data);
    if (next !== data) patches.push([query.queryKey, next]);
  }
  // An event that changes nothing (the IP retry's repeat reads, a duplicate frame) touches no
  // read in flight.
  if (patches.length === 0) return;
  // Query cancellation is synchronous even though cancelQueries returns a promise. Cancel before
  // writing the event patch so React Query cannot later commit a list response that started before
  // this lifecycle event and restore its pre-cancel snapshot over the patch.
  const cancelled = queryClient.getQueryCache().findAll({
    queryKey: ["instances", identity.actorScope],
    fetchStatus: "fetching",
  });
  void queryClient.cancelQueries({
    queryKey: ["instances", identity.actorScope],
  });
  advanceFleetWriteEpoch(queryClient, identity.actorScope);
  for (const [queryKey, next] of patches) {
    queryClient.setQueryData<VMListResponse>(queryKey, next);
  }
  // important: ask the cancelled reads again. One of them can be the refetch a mutation asked
  // for (a delete's reconcile), and a lifecycle event for any other instance used to cancel it
  // for good, leaving the pre-delete list on screen until the next safety read.
  for (const query of cancelled) rereadLater(queryClient, query.queryKey, query.queryHash);
}

function actorListQueries(queryClient: QueryClient, actorScope: string) {
  return [
    ...queryClient.getQueryCache().findAll({ queryKey: ["instances", actorScope] }),
    ...queryClient.getQueryCache().findAll({ queryKey: ["vms", actorScope] }),
  ];
}

/**
 * Optimistically remove one exact row while retaining only the row-level inverse operation.
 * Keeping whole-list snapshots would let a failed delete overwrite another concurrent delete or
 * a WebSocket patch that completed while this request was in flight.
 */
export async function optimisticallyRemoveExactInstance(
  queryClient: QueryClient,
  identity: InstanceIdentity,
): Promise<InstanceDeleteRollback> {
  const token = markInstanceDeleting(queryClient, identity);
  await Promise.all([
    queryClient.cancelQueries({ queryKey: ["instances", identity.actorScope] }),
    queryClient.cancelQueries({ queryKey: ["vms", identity.actorScope] }),
  ]);
  advanceFleetWriteEpoch(queryClient, identity.actorScope);

  const entries: InstanceDeleteRollbackEntry[] = [];
  for (const query of actorListQueries(queryClient, identity.actorScope)) {
    const data = queryClient.getQueryData<VMListResponse>(query.queryKey);
    if (!data?.vms) continue;
    const index = data.vms.findIndex((candidate) => isExactInstance(candidate, identity));
    if (index < 0) continue;
    entries.push({ queryKey: query.queryKey, row: data.vms[index], index });
    queryClient.setQueryData<VMListResponse>(query.queryKey, {
      ...data,
      vms: data.vms.filter((_, rowIndex) => rowIndex !== index),
      total: Math.max(0, data.total - 1),
    });
  }

  return { identity, entries, token };
}

/** Merge only the failed delete's row into the latest cache state. */
export function restoreExactInstanceRollback(
  queryClient: QueryClient,
  rollback: InstanceDeleteRollback | undefined,
): void {
  if (!rollback) return;
  clearInstanceDelete(queryClient, rollback.identity, rollback.token);
  advanceFleetWriteEpoch(queryClient, rollback.identity.actorScope);
  for (const entry of rollback.entries) {
    queryClient.setQueryData<VMListResponse | undefined>(entry.queryKey, (current) => {
      if (!current?.vms) return current;
      if (current.vms.some((candidate) => isExactInstance(candidate, rollback.identity))) {
        return current;
      }
      const index = Math.min(entry.index, current.vms.length);
      const vms = [...current.vms];
      vms.splice(index, 0, entry.row);
      return { ...current, vms, total: current.total + 1 };
    });
  }
}

/**
 * Patch only lists and detail keys that contain this exact actor/cluster/project/name tuple.
 * `readStartedAt` is when the read that produced `vm` began: a row a delete owns is accepted
 * (true, nothing to reconcile) but not written back.
 */
export function upsertExactInstance(
  queryClient: QueryClient,
  identity: InstanceIdentity,
  vm: VMInfo,
  readStartedAt: number,
): boolean {
  if (!isExactInstance(vm, identity)) return false;
  if (isHiddenByPendingDelete(queryClient, identity, readStartedAt)) return true;
  patchLists(queryClient, identity, (data) => {
    const index = data.vms.findIndex((row) => isExactInstance(row, identity));
    if (index >= 0 && JSON.stringify(data.vms[index]) === JSON.stringify(vm)) return data;
    const vms = index < 0
      ? [vm, ...data.vms]
      : data.vms.map((row, rowIndex) => (rowIndex === index ? vm : row));
    return { ...data, vms, total: index < 0 ? data.total + 1 : data.total };
  });
  queryClient.setQueryData(
    ["instance", identity.actorScope, identity.host, identity.project, identity.name],
    vm,
  );
  queryClient.setQueryData(
    ["vm", identity.actorScope, identity.host, identity.project, identity.name],
    vm,
  );
  return true;
}

/** Remove only the exact identity. A project-less event is deliberately not accepted here. */
export function removeExactInstance(
  queryClient: QueryClient,
  identity: InstanceIdentity,
): void {
  patchLists(queryClient, identity, (data) => {
    const vms = data.vms.filter((row) => !isExactInstance(row, identity));
    if (vms.length === data.vms.length) return data;
    return { ...data, vms, total: Math.max(0, data.total - 1) };
  });
  queryClient.removeQueries({
    queryKey: ["instance", identity.actorScope, identity.host, identity.project, identity.name],
    exact: true,
  });
  queryClient.removeQueries({
    queryKey: ["vm", identity.actorScope, identity.host, identity.project, identity.name],
    exact: true,
  });
}

/**
 * Read the actor's authoritative fleet snapshot and replace only its fleet cache.
 * Failed and rate-limited reads never clear the previous snapshot.
 */
export async function reconcileInstanceFleet(
  queryClient: QueryClient,
  actorScope: string,
  options: FleetReconcileOptions,
): Promise<VMListResponse> {
  const epoch = advanceFleetWriteEpoch(queryClient, actorScope);
  // The fleet read runs outside React Query. Cancel canonical reads before it starts and again
  // before publication so a query that began on either side of the network request cannot commit
  // an older snapshot after this authoritative reconciliation.
  await queryClient.cancelQueries({ queryKey: ["instances", actorScope] });
  const startedAt = readClock();
  const data = withoutPendingDeletes(queryClient, actorScope, await api.instances.list(
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    { fresh: options.fresh, signal: options.signal },
  ), startedAt);
  if (
    options.signal?.aborted ||
    !isCurrentFleetWriteEpoch(queryClient, actorScope, epoch)
  ) return data;
  await queryClient.cancelQueries({ queryKey: ["instances", actorScope] });
  if (
    options.signal?.aborted ||
    !isCurrentFleetWriteEpoch(queryClient, actorScope, epoch)
  ) return data;
  queryClient.setQueryData(["instances", actorScope, "*", "*"], data);
  await queryClient.invalidateQueries({
    predicate: (query) => {
      const [kind, actor, host, project] = query.queryKey;
      return (
        kind === "instances" &&
        actor === actorScope &&
        (host !== "*" || project !== "*")
      );
    },
    refetchType: "none",
  });
  return data;
}
