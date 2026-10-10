/**
 * Custom React Query hooks for Orcastra CMP
 */

import {
  useQuery,
  useMutation,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import {
  isMetricsAccessError,
  metricsRefetchInterval,
  shouldRetryMetrics,
} from "@/lib/instance-metrics-polling";
import { DEFAULT_HISTORY_WINDOW_SECONDS, HISTORY_WINDOWS } from "@/lib/monitoring-history";
import { usePermissions } from "@/hooks/use-permissions";
import { useEffect, useRef, useState } from "react";
import { APIError, type MetricsHistoryPoint } from "@/types/api";
import { 
  vmAPI, 
  imageAPI, 
  nodeAPI, 
  projectAPI, 
  tagAPI, 
  healthCheck,
  api,
  fetchAPI,
  isAuthActorSynchronized,
} from "@/lib/api";
import {
  VMCreateRequest,
  InstanceCreateRequest,
  InstanceSpecificationPreflightRequest,
  InstanceSpecificationUpdateRequest,
  VMActionRequest,
  ProjectCreateRequest,
  SnapshotCreateRequest,
  SnapshotRestoreRequest,
  MoveRequest,
  ProfileCreateRequest,
  ProfileUpdateRequest,
} from "@/types";
import {
  ATTENDED_DATA_CONFIG,
  DYNAMIC_DATA_CONFIG,
  ON_DEMAND_CONFIG,
  POLL_MS,
  SEMI_STATIC_DATA_CONFIG,
  STATIC_DATA_CONFIG,
  backingOffInterval,
  jitter,
} from "@/lib/query-config";
import {
  reconcileMutationImpact,
  type MutationImpactId,
} from "@/lib/mutation-coherence";
import {
  optimisticallyRemoveExactInstance,
  reconcileInstanceFleet,
  restoreExactInstanceRollback,
} from "@/lib/instance-reconciliation";

// Query Keys
export const queryKeys = {
  vms: ["vms"] as const,
  vmsFor: (actorScope: string, host?: string, status?: string) =>
    ["vms", actorScope, { host, status }] as const,
  instances: ["instances"] as const,
  instancesFor: (actorScope: string, host?: string, project?: string) =>
    ["instances", actorScope, host ?? "*", project ?? "*"] as const,
  vm: (
    actorScope: string,
    name: string,
    host: string,
    project?: string,
  ) => ["vm", actorScope, host, project ?? "default", name] as const,
  instanceMetrics: (
    actorScope: string,
    host: string,
    instanceName: string,
    project: string,
  ) => ["instanceMetrics", actorScope, host, project, instanceName] as const,
  instanceDetail: (
    actorScope: string,
    host: string,
    project: string,
    name: string,
  ) => ["instance", actorScope, host, project, name] as const,
  instanceSpecification: (
    actorScope: string,
    host: string,
    project: string,
    name: string,
    nonce: number,
  ) => ["instanceSpecification", actorScope, host, project, name, nonce] as const,
  instanceOperation: (actorScope: string, jobId: string) =>
    ["instanceOperation", actorScope, jobId] as const,
  instanceWritePolicy: (actorScope: string, host: string) =>
    ["instanceWritePolicy", actorScope, host] as const,
  instanceSnapshots: (
    actorScope: string,
    host: string,
    project: string,
    instanceName: string,
  ) => ["instanceSnapshots", actorScope, host, project, instanceName] as const,
  snapshotSchedules: (
    actorScope: string,
    host?: string,
    project?: string,
    instanceName?: string,
  ) => [
    "snapshotSchedules",
    actorScope,
    host ?? "*",
    project ?? "*",
    instanceName ?? "*",
  ] as const,
  memberResources: (actorScope: string, node: string, memberName: string) =>
    ["memberResources", actorScope, node, memberName] as const,
  images: ["images"] as const,
  imagesByHost: (actorScope: string, host: string) =>
    ["images", actorScope, host] as const,
  // A store read is keyed apart from the host's fleet slice, which useImages(host) owns with a
  // different shape (an array of one response).
  imageStore: (actorScope: string, host: string, project: string) =>
    ["images", actorScope, host, "store", project] as const,
  // Every store on a host, distinct from useImages(host)'s one-element array under ["images", host].
  imageStores: (actorScope: string, host: string) =>
    ["images", actorScope, host, "stores"] as const,
  nodes: ["nodes"] as const,
  nodesFor: (actorScope: string) => ["nodes", actorScope] as const,
  nodeNames: ["nodeNames"] as const,
  nodeNamesFor: (actorScope: string) => ["nodeNames", actorScope] as const,
  nodeAvailability: ["nodeAvailability"] as const,
  nodeAvailabilityFor: (actorScope: string) => ["nodeAvailability", actorScope] as const,
  nodeResources: (actorScope: string, nodeName: string) =>
    ["nodeResources", actorScope, nodeName] as const,
  nodeGpus: (actorScope: string, nodeName: string) =>
    ["nodeGpus", actorScope, nodeName] as const,
  projects: (host: string) => ["projects", host] as const,
  projectsFor: (actorScope: string, host: string) =>
    ["projects", host, actorScope] as const,
  allProjects: ["projects", "all"] as const,
  allProjectsFor: (actorScope: string) => ["projects", "all", actorScope] as const,
  clusters: ["clusters"] as const,
  clustersFor: (actorScope: string, fresh: boolean) =>
    ["clusters", actorScope, { fresh }] as const,
  health: ["health"] as const,
};

function useMonitoringScopeKey() {
  return useActorQueryScope().scopeKey;
}

function normalizeProject(project?: string | null) {
  return project || "default";
}

function refreshInstanceQueries(queryClient: QueryClient) {
  void reconcileMutationImpact(queryClient, "instance.state");
}

function reconcile(
  queryClient: QueryClient,
  impact: MutationImpactId,
  queryKeys?: readonly (readonly unknown[])[],
) {
  return reconcileMutationImpact(queryClient, impact, { queryKeys });
}

function removeInstanceDetails(
  queryClient: QueryClient,
  actorScope: string,
  name: string,
  host: string,
  project?: string | null,
) {
  const effectiveProject = normalizeProject(project);
  queryClient.removeQueries({
    queryKey: queryKeys.instanceDetail(actorScope, host, effectiveProject, name),
    exact: true,
  });
  queryClient.removeQueries({
    queryKey: queryKeys.vm(actorScope, name, host, effectiveProject),
    exact: true,
  });
}

// Default retry configuration with exponential backoff
const defaultRetryConfig = {
  retry: 3,
  retryDelay: (attemptIndex: number) => Math.min(1000 * 2 ** attemptIndex, 30000),
};

// VMs Hooks
export function useVMs(host?: string, status?: string) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const [fleetInterval] = useState(() => jitter(POLL_MS.fleet));
  return useQuery({
    queryKey: queryKeys.vmsFor(scopeKey, host, status),
    queryFn: ({ signal }) => vmAPI.list(host, status, undefined, undefined, { signal }),
    enabled: isAuthenticated,
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: fleetInterval,
    refetchIntervalInBackground: false,
    ...STATIC_DATA_CONFIG, // Cache for 3min; manual refresh via button
  });
}

export function useVM(name: string, host: string, project = "default") {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: queryKeys.vm(scopeKey, name, host, project),
    queryFn: () => vmAPI.get(name, host, project),
    enabled: isAuthenticated && !!name && !!host,
  });
}

export function useCreateVM() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (data: VMCreateRequest) => vmAPI.create(data),
    onSuccess: () => reconcile(queryClient, "instance.create-legacy"),
  });
}

export function useStartVM() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({
      name,
      request,
    }: {
      name: string;
      request: VMActionRequest;
    }) => vmAPI.start(name, request),
    onSuccess: () => {
      refreshInstanceQueries(queryClient);
    },
  });
}

export function useStopVM() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({
      name,
      request,
    }: {
      name: string;
      request: VMActionRequest;
    }) => vmAPI.stop(name, request),
    onSuccess: () => {
      refreshInstanceQueries(queryClient);
    },
  });
}

export function useRestartVM() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({
      name,
      request,
    }: {
      name: string;
      request: VMActionRequest;
    }) => vmAPI.restart(name, request),
    onSuccess: () => {
      refreshInstanceQueries(queryClient);
    },
  });
}

export function useDeleteVM() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  
  return useMutation({
    mutationFn: ({
      name,
      host,
      force,
      project,
    }: {
      name: string;
      host: string;
      force?: boolean;
      project?: string;
    }) => vmAPI.delete(name, host, force, project),
    onMutate: (variables) => optimisticallyRemoveExactInstance(queryClient, {
      actorScope: scopeKey,
      name: variables.name,
      host: variables.host,
      project: normalizeProject(variables.project),
    }).then((rollback) => ({ rollback })),
    onError: (_error, _variables, context) => {
      if (!isAuthActorSynchronized(scopeKey)) return;
      restoreExactInstanceRollback(queryClient, context?.rollback);
      void reconcileInstanceFleet(queryClient, scopeKey, { fresh: true }).catch(
        () => undefined,
      );
    },
    onSuccess: async (_data, variables) => {
      removeInstanceDetails(
        queryClient,
        scopeKey,
        variables.name,
        variables.host,
        variables.project,
      );
      await reconcile(queryClient, "instance.delete");
    },
  });
}

// Instance Metrics Hook
export function useInstanceMetrics(
  host: string,
  instanceName: string,
  project = "default",
  enabled = true
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();

  return useQuery({
    queryKey: queryKeys.instanceMetrics(scopeKey, host, instanceName, project),
    queryFn: () => vmAPI.getMetrics(host, instanceName, project),
    enabled: (query) =>
      isAuthenticated && enabled && !!host && !!instanceName &&
      !isMetricsAccessError(query.state.error),
    // Five seconds is fast enough to read as live without opening a WebSocket per dialog. The
    // cadence rules (refusal, Retry-After, backoff, stopped, second sample) live in
    // lib/instance-metrics-polling.ts.
    refetchInterval: metricsRefetchInterval,
    refetchIntervalInBackground: false,
    staleTime: POLL_MS.live / 2,
    retry: shouldRetryMetrics,
    retryOnMount: false,
    refetchOnReconnect: false,
    refetchOnWindowFocus: false,
  });
}

// Images Hooks
export function useImages(host?: string, opts?: { enabled?: boolean }) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const [fleetInterval] = useState(() => jitter(POLL_MS.fleet));
  return useRefreshableQuery({
    queryKey: host
      ? queryKeys.imagesByHost(scopeKey, host)
      : [...queryKeys.images, scopeKey],
    enabled: isAuthenticated && (opts?.enabled ?? true),
    queryFn: async (read) => {
      if (host) {
        const result = await imageAPI.listByHost(host, undefined, read);
        return [result]; // Normalize to array
      }
      return imageAPI.listAll(read);
    },
    // Always enabled - fetches all images when host is undefined, or filtered when host is provided
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: fleetInterval,
    refetchIntervalInBackground: false,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

// The image store a project reads on a host. The backend resolves `features.images`, so the
// response's `project` is the store that actually answered (its own, or `default` when the
// project shares the host's store).
export function useImagesByHost(
  host: string,
  project = "default",
  opts?: { enabled?: boolean; requestEpoch?: number },
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: [
      ...queryKeys.imageStore(scopeKey, host, project),
      opts?.requestEpoch ?? 0,
    ],
    queryFn: ({ signal }) => imageAPI.listByHost(host, project, { signal }),
    enabled: isAuthenticated && !!host && (opts?.enabled ?? true),
    staleTime: 60000,
  });
}

// Every image store on a host the caller may see; rows carry their store. Keyed under
// ["images", host] so the invalidations that reach the fleet list reach this too.
export function useImageStores(host?: string, opts?: { enabled?: boolean }) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: queryKeys.imageStores(scopeKey, host ?? ""),
    queryFn: () => imageAPI.listStores(host ?? ""),
    enabled: isAuthenticated && !!host && (opts?.enabled ?? true),
    staleTime: 60000,
  });
}

export function useDeleteImage() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      fingerprint,
      host,
      force,
      project,
    }: {
      fingerprint: string;
      host: string;
      force?: boolean;
      project?: string;
    }) => imageAPI.delete(fingerprint, host, force, project),
    onSuccess: () => reconcile(queryClient, "image.delete"),
  });
}

export function useRemoteImages(opts?: { enabled?: boolean }) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: ["remoteImages", scopeKey, "all"],
    queryFn: (read) => imageAPI.listRemote(read),
    enabled: isAuthenticated && (opts?.enabled ?? true),
    // Remote catalog changes ~daily and the backend caches it for 1h.
    staleTime: 1000 * 60 * 30,
    gcTime: 1000 * 60 * 60,
    refetchOnWindowFocus: false,
  });
}

// Nodes Hooks
export function useNodesStatus() {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  // note: lazy initialiser, so the cadence is fixed per hook instance rather than wandering
  // on every render. Jitter keeps a page of cards from missing an expired TTL in one burst.
  const [fleetInterval] = useState(() => jitter(POLL_MS.fleet));
  return useRefreshableQuery({
    queryKey: queryKeys.nodesFor(scopeKey),
    queryFn: (read) => nodeAPI.status(read),
    enabled: isAuthenticated,
    // important: this was 3600000. One hour, against a backend that caches the same body for
    // 180s, so every number sourced from /nodes was up to an hour old and the cluster cards
    // simply did not move. Matched to the server TTL: polling faster cannot produce a newer
    // number, it returns the identical cached body.
    refetchInterval: fleetInterval,
    refetchIntervalInBackground: false,
    staleTime: POLL_MS.fleet / 2,
    retry: 3,
    refetchOnWindowFocus: false,
    // note: was 'always', which fired a full fan-out on every mount even seconds after the
    // last one. The sidebar prefetches this key on hover, so sweeping the nav produced a
    // request per hover. Registration freshness comes from the invalidations, not from here.
    refetchOnMount: true,
  });
}

export function useNodeNames() {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: queryKeys.nodeNamesFor(scopeKey),
    queryFn: ({ signal }) => nodeAPI.names({ signal }),
    enabled: isAuthenticated,
    // note: this was staleTime: Infinity with refetchOnMount: false. Membership does change,
    // and registering a cluster then opening a create form showed a stale list until a hard
    // reload, because the register mutation invalidates queryKeys.nodes and not this key.
    // Three minutes plus the invalidation below is the fix.
    staleTime: 1000 * 60 * 3,
    gcTime: 1000 * 60 * 60 * 24, // keep in cache for 24 hours
    refetchOnWindowFocus: false,
    // Catalog data does not poll; a first-load failure recovers through the retry here, the
    // remount retry, and the Retry the picker offers. Never a 429: that would be a storm.
    retry: STATIC_DATA_CONFIG.retry,
  });
}

/**
 * Per-cluster reachability for node pickers.
 *
 * Separate query key from nodeNames on purpose: this polls on a minute, and sharing a key
 * would drag the name list along with it. Cheap by construction on the backend, which rides
 * the cached /nodes fan-out rather than generating its own.
 */
export function useNodeAvailability() {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: queryKeys.nodeAvailabilityFor(scopeKey),
    queryFn: (read) => nodeAPI.availability(read),
    enabled: isAuthenticated,
    // note: this hand-rolled set was character for character the attended tier. Using the
    // preset keeps the one place a 60s cadence is defined from drifting away from the
    // 45s backend TTL it is matched to.
    ...ATTENDED_DATA_CONFIG,
  });
}

export function useUpdateNodeDisplayName() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ nodeName, displayName }: { nodeName: string; displayName: string }) =>
      nodeAPI.updateDisplayName(nodeName, displayName),
    onSuccess: () => reconcile(queryClient, "node.update"),
  });
}

export function useInvalidateNodeCache() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (nodeName: string) => nodeAPI.invalidateCache(nodeName),
    onSuccess: () => reconcile(queryClient, "node.cache"),
  });
}

export function useInvalidateAllNodesCache() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: () => nodeAPI.invalidateAllCache(),
    onSuccess: () => reconcile(queryClient, "node.cache"),
  });
}

/**
 * Live resources for one cluster, for a surface someone is looking at right now.
 *
 * important: GET /nodes/{node}/resources is uncached on the backend, so every poll is a real
 * call into that one cluster. Gate it on the panel being open, and back off geometrically once
 * it starts failing rather than holding a dead host on a connect timeout forever. The query key
 * is per actor and node, which is the only place a per-cluster back-off is even expressible:
 * /nodes is one fan-out and cannot back off for a single member of it.
 */
export function useNodeResources(nodeName: string, opts?: { enabled?: boolean }) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: queryKeys.nodeResources(scopeKey, nodeName),
    queryFn: () => nodeAPI.resources(nodeName),
    ...DYNAMIC_DATA_CONFIG,
    enabled: isAuthenticated && (opts?.enabled ?? true) && !!nodeName,
    refetchInterval: backingOffInterval(POLL_MS.active),
    refetchIntervalInBackground: false,
  });
}

export function useNodeGpus(
  nodeName: string,
  opts?: { enabled?: boolean; requestEpoch?: number },
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: [...queryKeys.nodeGpus(scopeKey, nodeName), opts?.requestEpoch ?? 0],
    queryFn: ({ signal }) => nodeAPI.gpus(nodeName, { signal }),
    enabled: isAuthenticated && !!nodeName && (opts?.enabled ?? true),
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

// Projects Hooks
export function useAllProjects(options: { enabled?: boolean } = {}) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: queryKeys.allProjectsFor(scopeKey),
    // note: opts carries {fresh} through to fetchRead, which appends the cache-bypass
    // parameter. Without it a Retry inside the API-key project picker would re-read the same
    // 30s server cache and report success on the identical unreachable cluster.
    queryFn: (opts) => projectAPI.listAll(opts),
    enabled: isAuthenticated && (options.enabled ?? true),
    refetchInterval: 300000, // refresh every 5 minutes (300 seconds)
    staleTime: 30000, // Consider data stale after 30 seconds
    retry: (failureCount: number, error: unknown) => {
      // don't retry auth/rate-limit errors. On the status, not the message: the 429 text is
      // human now and no longer carries the number.
      if (error instanceof APIError && [401, 403, 429].includes(error.status)) return false;
      return failureCount < 2;
    },
    retryDelay: (attemptIndex: number) => Math.min(1000 * 2 ** attemptIndex, 10000),
    refetchOnWindowFocus: false,
  });
}

export function useProjects(
  host: string,
  options: { enabled?: boolean; verifyFresh?: boolean; requestEpoch?: number } = {},
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: [...queryKeys.projectsFor(scopeKey, host), options.requestEpoch ?? 0],
    queryFn: ({ signal }) => projectAPI.list(host, { signal }),
    enabled: isAuthenticated && !!host && (options.enabled ?? true),
    staleTime: options.verifyFresh ? 0 : 30000,
    refetchOnMount: options.verifyFresh ? "always" : true,
  });
}

/**
 * How much of a project's limits is allocated, for the create form's quota line. Read only
 * once a node and a project are chosen and the project sets a limit; a create or delete
 * invalidates it server-side and through refreshInstanceQueries.
 */
export function useProjectUsage(
  host: string,
  project: string,
  opts?: { enabled?: boolean; requestEpoch?: number },
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: ["projectUsage", scopeKey, host, project, opts?.requestEpoch ?? 0],
    queryFn: ({ signal }) => projectAPI.usage(host, project, { signal }),
    enabled: isAuthenticated && (opts?.enabled ?? true) && !!host && !!project,
    staleTime: 30000,
    retry: (failureCount: number, error: unknown) =>
      !(error instanceof APIError && [401, 403, 404, 429].includes(error.status)) && failureCount < 1,
    refetchOnWindowFocus: false,
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (data: ProjectCreateRequest) => projectAPI.create(data),
    onSuccess: () => reconcile(queryClient, "project.write"),
  });
}

export function useDeleteProject() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ host, projectName }: { host: string; projectName: string }) => 
      projectAPI.delete(host, projectName),
    onSuccess: () => reconcile(queryClient, "project.write"),
  });
}

export function useUpdateProject() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ host, projectName, data }: { host: string; projectName: string; data: any }) => 
      projectAPI.update(host, projectName, data),
    onSuccess: () => reconcile(queryClient, "project.write"),
  });
}

// Tags Hooks
export function useUpdateInstanceTags() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ host, instanceName, tags, project }: { 
      host: string; 
      instanceName: string; 
      tags: string[]; 
      project?: string 
    }) => tagAPI.updateInstanceTags(host, instanceName, tags, project),
    onSuccess: (_, variables) => reconcile(queryClient, "instance.tags", [
      tagQueryKeys.resourceTags(
        "instance",
        variables.instanceName,
        variables.host,
        variables.project,
      ),
    ]),
  });
}

export function useUpdateProjectTags() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ host, projectName, tags }: { 
      host: string; 
      projectName: string; 
      tags: string[] 
    }) => tagAPI.updateProjectTags(host, projectName, tags),
    onSuccess: (_, variables) => reconcile(queryClient, "project.write", [
      tagQueryKeys.resourceTags("project", variables.projectName, variables.host),
    ]),
  });
}

export function useUpdateNodeTags() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ nodeName, tags }: { nodeName: string; tags: string[] }) => 
      tagAPI.updateNodeTags(nodeName, tags),
    onSuccess: (_, variables) => reconcile(queryClient, "node.update", [
      tagQueryKeys.resourceTags("node", variables.nodeName),
    ]),
  });
}

// ========================================
// Enterprise Tag System Hooks
// ========================================

// Query keys for tag system
export const tagQueryKeys = {
  categories: ["tagCategories"] as const,
  tags: ["tags"] as const,
  tagList: (categoryId?: number, search?: string) => ["tags", "list", { categoryId, search }] as const,
  tagStats: ["tags", "stats"] as const,
  resourceTags: (resourceType: string, resourceId: string, host?: string, project?: string) =>
    ["tags", "resource", resourceType, resourceId, host, project] as const,
  userVisibility: ["tags", "visibility"] as const,
};

/**
 * Hook to fetch all tag categories.
 * Categories are relatively static, so use longer cache times.
 */
export function useTagCategories(includeInactive = false) {
  return useQuery({
    queryKey: [...tagQueryKeys.categories, { includeInactive }],
    queryFn: () => tagAPI.listCategories(includeInactive),
    staleTime: 10 * 60 * 1000, // 10 minutes - categories rarely change
    gcTime: 30 * 60 * 1000, // 30 minutes cache
    refetchOnWindowFocus: false,
  });
}

/**
 * Hook to fetch all tags with optional filtering.
 * Supports category filtering and search.
 */
export function useTagList(params?: { categoryId?: number; search?: string }) {
  return useQuery({
    queryKey: tagQueryKeys.tagList(params?.categoryId, params?.search),
    queryFn: () => tagAPI.listTags({ category_id: params?.categoryId, search: params?.search }),
    staleTime: 5 * 60 * 1000, // 5 minutes
    gcTime: 15 * 60 * 1000, // 15 minutes cache
  });
}

/**
 * Hook to fetch tag statistics.
 */
export function useTagStats() {
  return useQuery({
    queryKey: tagQueryKeys.tagStats,
    queryFn: () => tagAPI.getStats(),
    staleTime: 2 * 60 * 1000, // 2 minutes - stats can change more frequently
    refetchInterval: 5 * 60 * 1000, // Auto-refresh every 5 minutes
  });
}

/**
 * Hook to fetch tags for a specific resource.
 */
export function useResourceTags(
  resourceType: "node" | "project" | "instance",
  resourceId: string,
  host?: string,
  project?: string,
  enabled = true
) {
  return useQuery({
    queryKey: tagQueryKeys.resourceTags(resourceType, resourceId, host, project),
    queryFn: () => tagAPI.getResourceTags(resourceType, resourceId, host, project),
    enabled: enabled && !!resourceId,
    staleTime: 60 * 1000, // 1 minute
  });
}

/**
 * Hook to fetch user's tag visibility settings.
 */
export function useUserTagVisibility() {
  return useQuery({
    queryKey: tagQueryKeys.userVisibility,
    queryFn: () => tagAPI.getUserVisibility(),
    staleTime: 5 * 60 * 1000, // 5 minutes
    retry: 1, // Don't retry too much - user might not have settings yet
  });
}

/**
 * Hook to create a new tag category.
 */
export function useCreateTagCategory() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: import("@/types/api").TagCategoryCreate) => tagAPI.createCategory(data),
    onSuccess: () => reconcile(queryClient, "tag.catalogue"),
  });
}

/**
 * Hook to update a tag category.
 */
export function useUpdateTagCategory() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ categoryId, data }: { categoryId: number; data: import("@/types/api").TagCategoryUpdate }) =>
      tagAPI.updateCategory(categoryId, data),
    onSuccess: () => reconcile(queryClient, "tag.catalogue"),
  });
}

/**
 * Hook to create a new tag.
 */
export function useCreateTag() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: import("@/types/api").TagCreate) => tagAPI.createTag(data),
    onSuccess: () => reconcile(queryClient, "tag.catalogue"),
  });
}

/**
 * Hook to update a tag.
 */
export function useUpdateTag() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ tagId, data }: { tagId: number; data: import("@/types/api").TagUpdate }) =>
      tagAPI.updateTag(tagId, data),
    onSuccess: () => reconcile(queryClient, "tag.catalogue"),
  });
}

/**
 * Hook to delete a tag.
 */
export function useDeleteTag() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (tagId: number) => tagAPI.deleteTag(tagId),
    onSuccess: () => reconcile(queryClient, "tag.catalogue"),
  });
}

/**
 * Hook to set tags for a resource (replaces all existing tags).
 * This is the primary hook for updating resource tags.
 */
export function useSetResourceTags() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      resourceType,
      resourceId,
      tags,
      host,
      project,
    }: {
      resourceType: "node" | "project" | "instance";
      resourceId: string;
      tags: string[];
      host?: string;
      project?: string;
    }) => tagAPI.setResourceTags(resourceType, resourceId, tags, host, project),
    onSuccess: (_, variables) => reconcile(queryClient, "tag.resource", [
      tagQueryKeys.resourceTags(
        variables.resourceType,
        variables.resourceId,
        variables.host,
        variables.project,
      ),
    ]),
  });
}

/**
 * Hook to add tags to a resource (keeps existing tags).
 */
export function useAddResourceTags() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      resourceType,
      resourceId,
      tags,
      host,
      project,
    }: {
      resourceType: "node" | "project" | "instance";
      resourceId: string;
      tags: string[];
      host?: string;
      project?: string;
    }) => tagAPI.addResourceTags(resourceType, resourceId, tags, host, project),
    onSuccess: (_, variables) => reconcile(queryClient, "tag.resource", [
      tagQueryKeys.resourceTags(
        variables.resourceType,
        variables.resourceId,
        variables.host,
        variables.project,
      ),
    ]),
  });
}

/**
 * Hook to remove a single tag from a resource.
 */
export function useRemoveResourceTag() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      resourceType,
      resourceId,
      tagName,
      host,
      project,
    }: {
      resourceType: "node" | "project" | "instance";
      resourceId: string;
      tagName: string;
      host?: string;
      project?: string;
    }) => tagAPI.removeResourceTag(resourceType, resourceId, tagName, host, project),
    onSuccess: (_, variables) => reconcile(queryClient, "tag.resource", [
      tagQueryKeys.resourceTags(
        variables.resourceType,
        variables.resourceId,
        variables.host,
        variables.project,
      ),
    ]),
  });
}

/**
 * Hook to update user's tag visibility settings.
 */
export function useUpdateUserTagVisibility() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (settings: import("@/types/api").UserTagVisibilitySettings) =>
      tagAPI.updateUserVisibility(settings),
    onSuccess: () => reconcile(queryClient, "tag.visibility"),
  });
}

/**
 * Hook to initialize default tag categories.
 * Useful for admin setup or first-time initialization.
 */
export function useInitTagCategories() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => tagAPI.initCategories(),
    onSuccess: () => reconcile(queryClient, "tag.catalogue"),
  });
}

// Health Hook
export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: healthCheck,
    refetchInterval: 60000, // Check health every minute
    refetchIntervalInBackground: false,
    retry: 3,
  });
}

// ========================================
// LXD Features Hooks
// ========================================

// Warnings Hooks
// important: /warnings is mounted with require_partner(), so a tenant is answered 403. The
// dashboard mounts this unconditionally, which meant every tenant page load fired two
// forbidden requests and logged two console errors. Gated here rather than at the call sites
// so a new consumer cannot reintroduce it.
export function useWarnings(host?: string, options: { enabled?: boolean } = {}) {
  const { isPartner } = usePermissions();
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: ["warnings", scopeKey, host],
    queryFn: (read) => api.warnings.list(host, read),
    ...DYNAMIC_DATA_CONFIG,
    enabled: isAuthenticated && isPartner && (options.enabled ?? true),
    refetchInterval: 60000, // Check warnings every minute
  });
}

// IPAM Hooks
export function useIPAM(host?: string) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: ["ipam", scopeKey, host],
    queryFn: (read) => api.ipam.list(host, read),
    enabled: isAuthenticated,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

// Storage Pools Hooks
// The pool LIST for one node, without the fleet-wide resources call.
//
// important: the two pool queries are separate caches of the same data, so both must be
// invalidated together. Only ["storagePools"] was, so after creating or deleting a pool the
// create form's dropdown kept the old list until SEMI_STATIC_DATA_CONFIG's staleTime lapsed,
// and the storage-pools refresh button did not touch it at all.
const STORAGE_POOL_LIST_KEY = "storagePoolList";

// important: `useStoragePools` used to resolve the per-node list and `resources-all` in one
// unguarded Promise.all, so a slow or failing fan-out across every cluster took the pool list
// down with it, and TanStack reported that error state identically to "this node has no pools".
// The create form builds `devices.root` from the resolved pool, so it read an unrelated
// cluster's timeout as "no storage here" and refused a create it could have made. Anything that
// only needs the list should still use this hook, which never pays for the capacity read at
// all; the combined hook below now degrades instead of failing.
export function useStoragePoolList(
  host?: string,
  opts?: { enabled?: boolean; requestEpoch?: number },
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: [STORAGE_POOL_LIST_KEY, scopeKey, host, opts?.requestEpoch ?? 0],
    queryFn: (read) => api.storagePools.list(host, read),
    // note: `enabled` lets a form that has no node yet skip the fleet-wide read it would
    // otherwise fire on mount. Default true, so the list pages are unchanged.
    enabled: isAuthenticated && (opts?.enabled ?? true),
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useStoragePools(
  host?: string,
  opts?: { enabled?: boolean; requestEpoch?: number },
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const [fleetInterval] = useState(() => jitter(POLL_MS.fleet));
  return useRefreshableQuery({
    enabled: isAuthenticated && (opts?.enabled ?? true),
    queryKey: ["storagePools", scopeKey, host, opts?.requestEpoch ?? 0],
    queryFn: async (read) => {
      // important: the capacity read is secondary and must not be able to fail the list. It
      // fans out to every cluster, so it is the likelier of the two to time out, and losing it
      // used to blank the whole page under "No storage pools found" while the list itself had
      // answered. An operator reading that would look for pools that were never gone.
      // `capacityFailed` is what lets the surface say which of the two happened, instead of
      // rendering an absent reading the same way as a reading of nothing.
      const [poolsData, resourcesData] = await Promise.all([
        api.storagePools.list(host, read) as Promise<Record<string, unknown>>,
        (api.storagePools.getResourcesAll(read) as Promise<Record<string, unknown> | null>)
          .then((value) => ({ ok: true as const, value }))
          .catch(() => ({ ok: false as const, value: null })),
      ]);
      return {
        ...poolsData,
        capacityFailed: !resourcesData.ok,
        resources: (resourcesData.value as Record<string, unknown>)?.resources || {},
      };
    },
    // Always enabled - fetches all pools when host is undefined
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: fleetInterval,
    refetchIntervalInBackground: false,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useStoragePool(name: string, host: string) {
  return useQuery({
    queryKey: ["storagePool", name, host],
    queryFn: () => api.storagePools.get(name, host),
    enabled: !!name && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useStoragePoolResources(name: string, host: string) {
  return useQuery({
    queryKey: ["storagePoolResources", name, host],
    queryFn: () => api.storagePools.getResources(name, host),
    enabled: !!name && !!host,
    ...DYNAMIC_DATA_CONFIG,
  });
}

export function useCreateStoragePool() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host }: { data: any; host: string }) => api.storagePools.create(data, host),
    onSuccess: () => reconcile(queryClient, "storage.pool"),
  });
}

export function useDeleteStoragePool() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.storagePools.delete(name, host),
    onSuccess: () => reconcile(queryClient, "storage.pool"),
  });
}

// Storage Volumes Hooks
export function useStorageVolumes(
  pool?: string,
  host?: string,
  opts?: { enabled?: boolean; requestEpoch?: number },
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const [fleetInterval] = useState(() => jitter(POLL_MS.fleet));
  return useRefreshableQuery({
    queryKey: ["storageVolumes", scopeKey, pool, host, opts?.requestEpoch ?? 0],
    enabled: isAuthenticated && (opts?.enabled ?? true),
    queryFn: (read) => api.storageVolumes.list(host, pool, read),
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: fleetInterval,
    refetchIntervalInBackground: false,
    // Always enabled - fetches all volumes when pool/host is undefined
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useStorageVolume(pool: string, type: string, name: string, host: string) {
  return useQuery({
    queryKey: ["storageVolume", pool, type, name, host],
    // important: the API takes (volumeType, name, host, pool) and this used to forward
    // (pool, type, name, host), so every argument landed one slot early and the request went
    // to /storage/volumes/{pool}/{type}?host={name}&pool={host}. It never fired because
    // nothing called this hook. Adding the first caller is what would have shipped it.
    queryFn: () => api.storageVolumes.get(type, name, host, pool),
    enabled: !!pool && !!type && !!name && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useCreateStorageVolume() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host }: { data: any; host: string }) => api.storageVolumes.create(data, host),
    onSuccess: () => reconcile(queryClient, "storage.volume"),
  });
}

export function useDeleteStorageVolume() {
  const queryClient = useQueryClient();

  return useMutation({
    // important: same scrambled argument order as useStorageVolume above. See the note there.
    mutationFn: ({ pool, type, name, host }: { pool: string; type: string; name: string; host: string }) =>
      api.storageVolumes.delete(type, name, host, pool),
    onSuccess: () => reconcile(queryClient, "storage.volume"),
  });
}

/**
 * Pin or release a NIC's fixed IPv4 on an instance that already exists.
 *
 * Invalidates the instance queries rather than patching them locally: the write can override a
 * profile-inherited device onto the instance, which changes `devices` as well as the address,
 * and only a re-read shows that honestly.
 */
export function useSetNicAddress() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      instanceName,
      deviceName,
      host,
      project,
      ipv4Address,
    }: {
      instanceName: string;
      deviceName: string;
      host: string;
      project?: string;
      ipv4Address: string | null;
    }) =>
      api.instanceDevices.setNicAddress(instanceName, deviceName, {
        host,
        project,
        ipv4_address: ipv4Address,
      }),
    onSuccess: () => reconcile(queryClient, "instance.nic"),
  });
}

// Networks Hooks
/**
 * @param project omit for every project, each row naming the one that holds it. Pass one to
 *   get the set that project can actually use, which is what a picker needs: a project only
 *   holds its own networks when it sets features.networks, otherwise it sees default's.
 */
export function useNetworks(
  host?: string,
  project?: string,
  opts?: { enabled?: boolean; requestEpoch?: number },
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const [fleetInterval] = useState(() => jitter(POLL_MS.fleet));
  return useRefreshableQuery({
    // important: the project belongs in the key. Without it React Query serves one project's
    // rows to another project's caller from cache.
    queryKey: [
      "networks",
      scopeKey,
      host,
      project ?? null,
      opts?.requestEpoch ?? 0,
    ],
    enabled: isAuthenticated && (opts?.enabled ?? true),
    queryFn: (read) => api.networks.list(host, project, read),
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: fleetInterval,
    refetchIntervalInBackground: false,
    // Always enabled - fetches all networks when host is undefined
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useNetwork(name: string, host: string, project?: string) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: ["network", scopeKey, name, host, project ?? null],
    queryFn: () => api.networks.get(name, host, project),
    enabled: isAuthenticated && !!name && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useNetworkState(name: string, host: string, project?: string) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: ["networkState", scopeKey, name, host, project ?? null],
    queryFn: () => api.networks.getState(name, host, project),
    enabled: isAuthenticated && !!name && !!host,
    ...DYNAMIC_DATA_CONFIG,
  });
}

export function useCreateNetwork() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host, project }: { data: any; host: string; project?: string }) =>
      api.networks.create(data, host, project),
    onSuccess: () => reconcile(queryClient, "network.write"),
  });
}

export function useDeleteNetwork() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host, project }: { name: string; host: string; project?: string }) =>
      api.networks.delete(name, host, project),
    onSuccess: () => reconcile(queryClient, "network.write"),
  });
}

// Network ACLs Hooks
/** @param project omit for every project. See useNetworks. */
export function useNetworkACLs(host?: string, project?: string) {
  return useRefreshableQuery({
    queryKey: ["networkACLs", host, project ?? null],
    queryFn: (read) => api.networkACLs.list(host, project, read),
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useNetworkACL(name: string, host: string, project?: string) {
  return useQuery({
    queryKey: ["networkACL", name, host, project ?? null],
    queryFn: () => api.networkACLs.get(name, host, project),
    enabled: !!name && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

/**
 * The read an edit dialog seeds from: always fresh, never retried, keyed by a nonce so a
 * Reload re-fetches rather than handing back the copy it is meant to replace.
 *
 * important: not useNetworkACL. That read is two minutes stale by design, and an edit that
 * seeds from a stale record carries a stale etag, so every save would be refused as a 412.
 */
export function useNetworkACLForEdit(
  name: string,
  host: string,
  project: string | undefined,
  nonce: number,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["networkACL", "edit", name, host, project ?? null, nonce],
    queryFn: () => api.networkACLs.get(name, host, project),
    enabled: enabled && !!name && !!host,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    // a reconnect must not re-read underneath an edit in progress: the re-seed would drop the
    // operator's changes without the stale notice this dialog exists to show
    refetchOnReconnect: false,
  });
}

export function useCreateNetworkACL() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host, project }: { data: any; host: string; project?: string }) =>
      api.networkACLs.create(data, host, project),
    onSuccess: () => reconcile(queryClient, "network-acl.write"),
  });
}

export function useDeleteNetworkACL() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host, project }: { name: string; host: string; project?: string }) =>
      api.networkACLs.delete(name, host, project),
    onSuccess: () => reconcile(queryClient, "network-acl.write"),
  });
}

/**
 * The update half of the ACL surface, which had a route, an API client and no hook, so the
 * page could create and delete a policy but never correct one.
 */
export function useUpdateNetworkACL() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      name,
      host,
      data,
      project,
      etag,
    }: {
      name: string;
      host: string;
      data: any;
      project?: string;
      /** From useNetworkACLForEdit; the write is refused with 412 if the ACL changed since. */
      etag?: string | null;
    }) => api.networkACLs.update(name, host, data, project, etag),
    onSuccess: () => reconcile(queryClient, "network-acl.write"),
  });
}

// Profiles Hooks
/** @param project omit for every project. See useNetworks. */
export function useProfiles(host?: string, project?: string, opts?: { enabled?: boolean }) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: ["profiles", scopeKey, host, project ?? null],
    enabled: isAuthenticated && (opts?.enabled ?? true),
    queryFn: (read) => api.profiles.list(host, project, read),
    // Always enabled - fetches all profiles when host is undefined
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useProfile(
  name: string,
  host: string,
  project?: string,
  opts?: { enabled?: boolean; requestEpoch?: number },
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: [
      "profile",
      scopeKey,
      name,
      host,
      project ?? null,
      opts?.requestEpoch ?? 0,
    ],
    queryFn: ({ signal }) => api.profiles.get(name, host, project, { signal }),
    enabled: isAuthenticated && !!name && !!host && (opts?.enabled ?? true),
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

/**
 * The read an edit dialog seeds from: always fresh, never retried, keyed by a nonce so a
 * Reload re-fetches rather than handing back the copy it is meant to replace. Same reasoning
 * as useNetworkACLForEdit: the stale read above would carry a stale etag into every save.
 */
export function useProfileForEdit(
  name: string,
  host: string,
  project: string | undefined,
  nonce: number,
  enabled: boolean,
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: ["profile", scopeKey, "edit", name, host, project ?? null, nonce],
    queryFn: ({ signal }) => api.profiles.get(name, host, project, { signal }),
    enabled: isAuthenticated && enabled && !!name && !!host,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    // a reconnect must not re-read underneath an edit in progress: the re-seed would drop the
    // operator's changes without the stale notice this dialog exists to show
    refetchOnReconnect: false,
  });
}

export function useCreateProfile() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host, project }: { data: ProfileCreateRequest; host: string; project?: string }) =>
      api.profiles.create(data, host, project),
    onSuccess: () => reconcile(queryClient, "profile.write"),
  });
}

export function useUpdateProfile() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      name,
      host,
      project,
      data,
      etag,
    }: {
      name: string;
      host: string;
      project?: string;
      data: ProfileUpdateRequest;
      /** From useProfileForEdit; the write is refused with 412 if the profile changed since. */
      etag?: string | null;
    }) => api.profiles.update(name, host, data, project, etag),
    onSuccess: () => reconcile(queryClient, "profile.write"),
  });
}

export function useDeleteProfile() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host, project }: { name: string; host: string; project?: string }) =>
      api.profiles.delete(name, host, project),
    onSuccess: () => reconcile(queryClient, "profile.write"),
  });
}

// Instances Hooks (alternative to VMs for LXD terminology)
export function useInstances(
  host?: string,
  opts?: {
    realtime?: boolean;
    enabled?: boolean;
    project?: string;
    verifyFresh?: boolean;
    transientRetries?: number;
  },
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: queryKeys.instancesFor(scopeKey, host, opts?.project),
    queryFn: (read) =>
      api.instances.list(host, undefined, undefined, undefined, undefined, {
        ...read,
        project: opts?.project,
      }),
    // Always enabled - fetches all instances when host is undefined
    ...DYNAMIC_DATA_CONFIG,
    enabled: isAuthenticated && (opts?.enabled ?? true),
    ...(opts?.verifyFresh ? { staleTime: 0, refetchOnMount: "always" as const } : {}),
    ...(opts?.transientRetries !== undefined
      ? {
          // Picker reads can briefly race process startup while the connection healer fills
          // this worker's in-memory client cache. Retry only transient failures and keep auth,
          // validation, missing-target and rate-limit responses terminal.
          retry: (failureCount: number, error: unknown) =>
            !(
              error instanceof APIError &&
              [400, 401, 403, 404, 409, 422, 429].includes(error.status)
            ) && failureCount < opts.transientRetries!,
          retryDelay: (attemptIndex: number) => Math.min(500 * 2 ** attemptIndex, 2000),
        }
      : {}),
    // Default: poll so newly-created instances and status transitions show
    // without a manual refresh. When `realtime` is set (instances page), polling
    // is disabled and the list is kept current via the LXD events stream
    // (see useInstanceStream), and the query just provides the initial snapshot.
    // note: was 10000 against a 30s backend TTL, so two of every three polls returned the
    // identical cached body. false when the events WebSocket is driving this key instead.
    refetchInterval: opts?.realtime ? false : POLL_MS.active,
    refetchIntervalInBackground: false,
  });
}

// note: ON_DEMAND_CONFIG rather than DYNAMIC_DATA_CONFIG, because this is opened by a person
// rather than polled. The only caller is the Monitoring Top Consumers drill-down: that summary
// deliberately carries just enough data for its chart, so the detail dialog explicitly hydrates
// it before rendering the full configuration. Normal instance rows already carry those fields.
//
// important: `enabled` is re-asserted AFTER the spread. ON_DEMAND_CONFIG sets enabled false by
// design, so spreading it last would silently disable every caller.
export function useInstance(name: string, host: string, enabled = true, project?: string) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const effectiveProject = project || "default";
  return useQuery({
    queryKey: queryKeys.instanceDetail(scopeKey, host, effectiveProject, name),
    queryFn: ({ signal }) => api.instances.get(name, host, effectiveProject, { signal }),
    ...ON_DEMAND_CONFIG,
    enabled: isAuthenticated && enabled && !!name && !!host,
    // important: both re-asserted after the spread. This payload completes a partial summary,
    // so every opening revalidates it while the cached detail remains available immediately.
    staleTime: 0,
    refetchOnMount: "always",
  });
}

export function useCreateInstance() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      data,
      idempotencyKey,
    }: {
      data: InstanceCreateRequest;
      idempotencyKey: string;
    }) => api.instances.create(data, idempotencyKey),
    onSuccess: () => reconcile(queryClient, "instance.create"),
  });
}

export function usePreflightInstanceCreate() {
  return useMutation({
    mutationFn: (data: InstanceCreateRequest) =>
      api.instances.preflightCreate(data),
  });
}

/** Server-resolved shared/exclusive write policy. Advanced controls fail closed without it. */
export function useInstanceWritePolicy(
  host: string,
  opts?: { enabled?: boolean; requestEpoch?: number },
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: [
      ...queryKeys.instanceWritePolicy(scopeKey, host),
      opts?.requestEpoch ?? 0,
    ],
    queryFn: ({ signal }) => api.instances.getWritePolicy(host, { signal }),
    enabled: isAuthenticated && !!host && (opts?.enabled ?? true),
    staleTime: 0,
    retry: 1,
    refetchOnMount: "always",
    refetchOnReconnect: true,
    refetchOnWindowFocus: false,
  });
}

/** Fresh, uncached specification read used to seed the edit dialog. */
export function useInstanceSpecificationForEdit(
  name: string,
  host: string,
  project: string,
  nonce: number,
  enabled: boolean,
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: queryKeys.instanceSpecification(
      scopeKey,
      host,
      project,
      name,
      nonce,
    ),
    queryFn: ({ signal }) =>
      api.instances.getSpecification(name, host, project, { signal }),
    enabled: isAuthenticated && enabled && !!name && !!host,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchOnReconnect: false,
    refetchOnWindowFocus: false,
  });
}

export function usePreflightInstanceSpecification() {
  return useMutation({
    mutationFn: ({
      name,
      data,
    }: {
      name: string;
      data: InstanceSpecificationPreflightRequest;
    }) => api.instances.preflightSpecification(name, data),
  });
}

export function useUpdateInstanceSpecification() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      name,
      data,
      etag,
      idempotencyKey,
    }: {
      name: string;
      data: InstanceSpecificationUpdateRequest;
      etag: string;
      idempotencyKey: string;
    }) => api.instances.updateSpecification(name, data, etag, idempotencyKey),
    onSuccess: () => reconcile(queryClient, "instance.specification"),
  });
}

const TERMINAL_INSTANCE_OPERATION = new Set([
  "success",
  "failure",
  "partial",
]);

export function useInstanceOperationJob(jobId: string | null, enabled = true) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const queryClient = useQueryClient();
  const reconciledTerminal = useRef<string | null>(null);
  const query = useQuery({
    queryKey: queryKeys.instanceOperation(scopeKey, jobId ?? ""),
    queryFn: ({ signal }) => api.instances.getOperation(jobId!, { signal }),
    enabled: isAuthenticated && enabled && !!jobId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status && TERMINAL_INSTANCE_OPERATION.has(status)) return false;
      return status === "unknown" ? 10_000 : 2_000;
    },
    refetchIntervalInBackground: false,
    retry: (failureCount, error) => {
      if (error instanceof APIError && (error.status === 401 || error.status === 403)) {
        return false;
      }
      return failureCount < 2;
    },
    refetchOnReconnect: true,
    refetchOnWindowFocus: true,
  });
  const status = query.data?.status;
  useEffect(() => {
    if (!jobId || !status || !TERMINAL_INSTANCE_OPERATION.has(status)) return;
    const terminalKey = `${scopeKey}:${jobId}:${status}`;
    if (reconciledTerminal.current === terminalKey) return;
    reconciledTerminal.current = terminalKey;
    const impact = query.data?.kind === "create"
      ? "instance.create"
      : "instance.specification";
    void (async () => {
      await reconcileMutationImpact(queryClient, impact).catch(() => undefined);
      if (!isAuthActorSynchronized(scopeKey)) return;
      // This authoritative read must be the last list write after invalidation-triggered refetches.
      await reconcileInstanceFleet(queryClient, scopeKey, { fresh: true }).catch(() => undefined);
    })();
  }, [jobId, query.data?.kind, queryClient, scopeKey, status]);
  return query;
}

// Instance Action Mutations (using vmAPI)
export function useStartInstance() {
  const queryClient = useQueryClient();
  
  return useMutation({
    // important: the project travels with the request. A name is unique per project, not per
    // host, so a power action that omits it is asking the backend to guess. It used to guess by
    // searching every project and taking the first hit, which meant the ACL was checked against
    // `default` while the action ran somewhere else. The backend is now strict, so omitting the
    // project here would 404 on any instance outside `default`. delete and console already sent it.
    mutationFn: ({ name, host, project }: { name: string; host: string; project?: string }) =>
      api.vms.start(name, { host, project }),
    onSuccess: () => {
      refreshInstanceQueries(queryClient);
    },
  });
}

export function useStopInstance() {
  const queryClient = useQueryClient();

  return useMutation({
    // force power-cycles instead of a graceful ACPI shutdown (needed for a VM
    // with no OS installed / sitting at a boot menu, which can't answer ACPI).
    mutationFn: ({ name, host, force, project }: { name: string; host: string; force?: boolean; project?: string }) =>
      api.vms.stop(name, { host, force, timeout: 30, project }),
    onSuccess: () => {
      refreshInstanceQueries(queryClient);
    },
  });
}

export function useRestartInstance() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ name, host, force, project }: { name: string; host: string; force?: boolean; project?: string }) =>
      api.vms.restart(name, { host, force, timeout: 30, project }),
    onSuccess: () => {
      refreshInstanceQueries(queryClient);
    },
  });
}

export function useDeleteInstance() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  
  return useMutation({
    mutationFn: ({ name, host, force, project }: { name: string; host: string; force?: boolean; project?: string }) =>
      api.vms.delete(name, host, force, project),
    onMutate: (variables) => optimisticallyRemoveExactInstance(queryClient, {
      actorScope: scopeKey,
      name: variables.name,
      host: variables.host,
      project: normalizeProject(variables.project),
    }).then((rollback) => ({ rollback })),
    onError: (_error, _variables, context) => {
      if (!isAuthActorSynchronized(scopeKey)) return;
      restoreExactInstanceRollback(queryClient, context?.rollback);
      void reconcileInstanceFleet(queryClient, scopeKey, { fresh: true }).catch(
        () => undefined,
      );
    },
    onSuccess: async (_data, variables) => {
      removeInstanceDetails(
        queryClient,
        scopeKey,
        variables.name,
        variables.host,
        variables.project,
      );
      await reconcile(queryClient, "instance.delete");
    },
  });
}

// Server Info Hooks
export function useServerInfo(host?: string, enabled = true) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: ["serverInfo", scopeKey, host],
    queryFn: (read) => api.server.list(host, read),
    ...STATIC_DATA_CONFIG,
    enabled: isAuthenticated && enabled,
  });
}

export function useServerResources(host: string) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: ["serverResources", scopeKey, host],
    queryFn: () => api.server.getResources(host),
    enabled: isAuthenticated && !!host,
    ...DYNAMIC_DATA_CONFIG,
  });
}

// Identities Hooks
export function useIdentities(host?: string) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: ["identities", scopeKey, host],
    queryFn: (read) => api.identities.list(host, read),
    enabled: isAuthenticated,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useIdentity(id: string, host: string) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: ["identity", scopeKey, id, host],
    queryFn: () => api.identities.get(id, host),
    enabled: isAuthenticated && !!id && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useDeleteIdentity() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ authType, identityId, host }: { authType: string; identityId: string; host: string }) => 
      api.identities.delete(authType, identityId, host),
    onSuccess: () => reconcile(queryClient, "identity.write"),
  });
}

// Groups Hooks
export function useGroups(host?: string) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: ["groups", scopeKey, host],
    queryFn: (read) => api.groups.list(host, read),
    enabled: isAuthenticated,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useGroup(name: string, host: string) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: ["group", scopeKey, name, host],
    queryFn: () => api.groups.get(name, host),
    enabled: isAuthenticated && !!name && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useCreateGroup() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host }: { data: any; host: string }) => 
      api.groups.create(data, host),
    onSuccess: () => reconcile(queryClient, "group.write"),
  });
}

export function useUpdateGroup() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, data, host }: { name: string; data: any; host: string }) => 
      api.groups.update(name, data, host),
    onSuccess: () => reconcile(queryClient, "group.write"),
  });
}

export function useAddIdentityToGroup() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ groupName, authType, identityId, host }: { 
      groupName: string; 
      authType: "tls" | "oidc"; 
      identityId: string; 
      host: string 
    }) => api.groups.addIdentity(groupName, authType, identityId, host),
    onSuccess: () => reconcile(queryClient, "group.write"),
  });
}

export function useRemoveIdentityFromGroup() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ groupName, authType, identityId, host }: { 
      groupName: string; 
      authType: string; 
      identityId: string; 
      host: string 
    }) => api.groups.removeIdentity(groupName, authType, identityId, host),
    onSuccess: () => reconcile(queryClient, "group.write"),
  });
}

export function useDeleteGroup() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.groups.delete(name, host),
    onSuccess: () => reconcile(queryClient, "group.write"),
  });
}

// Cluster Members Hooks
export function useClusterMembers(node: string) {
  const [fleetInterval] = useState(() => jitter(POLL_MS.fleet));
  return useRefreshableQuery({
    queryKey: ["clusterMembers", node],
    queryFn: (read) => api.clusterMembers.list(node, read),
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: fleetInterval,
    refetchIntervalInBackground: false,
    enabled: !!node,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useClusterMember(node: string, memberName: string) {
  return useQuery({
    queryKey: ["clusterMember", node, memberName],
    queryFn: () => api.clusterMembers.get(node, memberName),
    enabled: !!node && !!memberName,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useMemberResources(
  node: string,
  memberName: string,
  opts?: { enabled?: boolean },
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: queryKeys.memberResources(scopeKey, node, memberName),
    // note: getResources routes through the extended fan-out budget, which was unreachable
    // from the UI while this hook never passed a fresh flag.
    queryFn: (read) => api.clusterMembers.getResources(node, memberName, read),
    // note: gated so a closed modal stops polling. The backend caches this for 60s, so the
    // interval matches: anything faster returns the same body.
    enabled: isAuthenticated && (opts?.enabled ?? true) && !!node && !!memberName,
    staleTime: 30 * 1000, // 30 seconds
    gcTime: 5 * 60 * 1000, // 5 minutes
    refetchInterval: backingOffInterval(POLL_MS.attended),
    refetchIntervalInBackground: false,
  });
}

// Clusters Hooks
export function useClusters(fresh: boolean = false) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const [fleetInterval] = useState(() => jitter(POLL_MS.fleet));
  return useRefreshableQuery({
    queryKey: queryKeys.clustersFor(scopeKey, fresh),
    queryFn: ({ fresh: manual, signal }) =>
      api.clusters.list(fresh || manual, { signal }),
    enabled: isAuthenticated,
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: fleetInterval,
    refetchIntervalInBackground: false,
    ...SEMI_STATIC_DATA_CONFIG,
    staleTime: 0, // Always consider stale to ensure fresh data after deletion
    refetchOnMount: 'always', // Always refetch on mount to ensure fresh data after registration/deletion
  });
}

export function useCluster(clusterId: string) {
  return useQuery({
    queryKey: ["cluster", clusterId],
    queryFn: () => api.clusters.get(clusterId),
    enabled: !!clusterId,
    ...SEMI_STATIC_DATA_CONFIG,
    refetchOnMount: true, // Always refetch on mount to ensure fresh data
  });
}

export function useRegisterCluster() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (data: any) => api.clusters.register(data),
    onSuccess: () => reconcile(queryClient, "cluster.register"),
  });
}

export function useCheckClusterAuth() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (clusterId: string) => api.clusters.checkAuth(clusterId),
    onSuccess: () => reconcile(queryClient, "cluster.auth-check"),
  });
}

export function useDeleteCluster() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (clusterId: string) => api.clusters.delete(clusterId),
    onSuccess: () => reconcile(queryClient, "cluster.delete"),
  });
}

export function useRegenerateCert() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (clusterId: string) => api.clusters.regenerateCert(clusterId),
    onSuccess: () => reconcile(queryClient, "cluster.certificate"),
  });
}

export function useUpdateClusterLocation() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ clusterId, location }: { clusterId: string; location: any }) => 
      api.clusters.updateLocation(clusterId, location),
    onSuccess: () => reconcile(queryClient, "cluster.location"),
  });
}

export function useFacilitySuggestions() {
  return useQuery({
    queryKey: ["facilities"],
    queryFn: () => api.clusters.getFacilities(),
    staleTime: 300000, // 5 min - facility data rarely changes
  });
}

// ========== Monitoring Hooks ==========

/**
 * The monitoring dashboard. `polling` is false while the admin WebSocket pushes each collection.
 *
 * note: 30 s, the server's cadence. The response is cached for 35 s and rebuilt by a collector
 * every 30 s, so a 5 s poll only fetched the same body six times.
 */
export function useMonitoringDashboard(enabled: boolean = true, polling: boolean = true) {
  const scopeKey = useMonitoringScopeKey();
  return useRefreshableQuery({
    queryKey: ["monitoring", "dashboard", scopeKey],
    queryFn: (read) => api.monitoring.getDashboard(read),
    enabled,
    refetchInterval: polling ? POLL_MS.active : false,
    staleTime: 5000, // Consider fresh for 5 seconds to avoid redundant fetches
    gcTime: 60 * 1000,
    refetchOnWindowFocus: polling, // Only refetch on focus when polling
    refetchIntervalInBackground: false, // Stop polling when tab is hidden
    retry: 3,
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 10000),
  });
}

export function useMonitoringNodeMetrics(nodeName: string, enabled: boolean = true) {
  const scopeKey = useMonitoringScopeKey();
  return useQuery({
    queryKey: ["monitoring", "node", scopeKey, nodeName],
    queryFn: () => api.monitoring.getNodeMetrics(nodeName),
    enabled: enabled && !!nodeName,
    refetchInterval: 10000,
    refetchIntervalInBackground: false,
    staleTime: 5000,
  });
}

/**
 * Chart history for the last `windowSeconds`.
 *
 * note: no prefetch of the other ranges. Each history read parses the stored history on the
 * server, so warming five ranges on every page load cost five parses nobody had asked for.
 */
export function useMonitoringHistory(
  windowSeconds: number = DEFAULT_HISTORY_WINDOW_SECONDS,
  enabled: boolean = true,
) {
  const queryClient = useQueryClient();
  const scopeKey = useMonitoringScopeKey();

  return useQuery({
    queryKey: ["monitoring", "history", scopeKey, windowSeconds],
    queryFn: () => api.monitoring.getHistory(windowSeconds),
    enabled,
    refetchInterval: POLL_MS.active, // one new point per collection: the sweep, then a 30 s pause
    staleTime: 30000,
    gcTime: 600000, // keep visited ranges for fast switching back
    refetchIntervalInBackground: false, // Stop polling when tab is hidden
    retry: 2, // Retry up to 2 times on failure
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 5000),
    placeholderData: (previousData) => {
      // Keep previous data for seamless display while fetching
      if (previousData) return previousData;
      // Cross-range fallback: the closest range already cached
      const nearest = HISTORY_WINDOWS.map((range) => range.seconds)
        .filter((seconds) => seconds !== windowSeconds)
        .sort((a, b) => Math.abs(a - windowSeconds) - Math.abs(b - windowSeconds));
      for (const seconds of nearest) {
        const cached = queryClient.getQueryData<MetricsHistoryPoint[]>([
          "monitoring",
          "history",
          scopeKey,
          seconds,
        ]);
        if (cached && cached.length > 0) return cached;
      }
      return undefined;
    },
  });
}

// ========== Certificate Management Hooks ==========

export function useCertificates() {
  return useRefreshableQuery({
    queryKey: ["certificates"],
    queryFn: (read) => api.certificates.list(read),
    refetchInterval: 60000, // Refresh every minute
    staleTime: 30000,
  });
}

export function useCertificate(clusterId: string) {
  return useQuery({
    queryKey: ["certificate", clusterId],
    queryFn: () => api.certificates.get(clusterId),
    enabled: !!clusterId,
    staleTime: 30000,
  });
}

export function useInitiateCertRenewal() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (request: { cluster_id: string; ttl: string; trust_password?: string }) => 
      api.certificates.initiateRenewal(request as any),
    onSuccess: () => reconcile(queryClient, "cluster.certificate"),
  });
}

export function useVerifyCertRenewal() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (request: { cluster_id: string; pending_cert_id?: string }) => 
      api.certificates.verifyRenewal(request),
    onSuccess: () => reconcile(queryClient, "cluster.certificate"),
  });
}

export function useCancelPendingCert() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (clusterId: string) => api.certificates.cancelPending(clusterId),
    onSuccess: () => reconcile(queryClient, "cluster.certificate"),
  });
}

/** The pending certificate behind a row's Verify, fetched only while the dialog shows it. */
export function usePendingCertificate(clusterId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ["certificate", clusterId, "pending"],
    queryFn: () => api.certificates.getPending(clusterId as string),
    enabled: enabled && !!clusterId,
    staleTime: 0,
    retry: false,
  });
}

export function useRemoveSupersededTrust() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ clusterId, fingerprint }: { clusterId: string; fingerprint?: string }) =>
      api.certificates.removeTrust(clusterId, fingerprint),
    onSuccess: () => reconcile(queryClient, "cluster.certificate"),
  });
}

export function useSystemWarnings(options: { enabled?: boolean } = {}) {
  // the same 403 as useWarnings: /warnings/system/all sits behind require_partner()
  const { isPartner } = usePermissions();
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    enabled: isAuthenticated && isPartner && (options.enabled ?? true),
    queryKey: ["systemWarnings", scopeKey],
    // note: every sibling destructures { fresh }; this one did not, so the second half of
    // "Refresh all" on the warnings page silently reused whatever was already in flight.
    queryFn: (read) => api.certificates.getSystemWarnings(read),
    refetchInterval: 60000, // Refresh every minute
    staleTime: 30000,
  });
}

// Regions Hooks
export function useRegionsOverview() {
  return useRefreshableQuery({
    queryKey: ["regions", "overview"],
    queryFn: (read) => api.regions.getOverview(read),
    refetchInterval: 120000, // Refresh every 2 minutes
    staleTime: 60000, // Data considered fresh for 1 minute
  });
}

export function useRegionDetails(regionId: string) {
  return useQuery({
    queryKey: ["regions", regionId],
    queryFn: () => api.regions.getRegion(regionId),
    enabled: !!regionId,
    staleTime: 60000,
  });
}

// Snapshot Hooks
//
// Snapshots change only when someone acts on them or LXD's own schedule fires, so they read on
// the semi-static cadence rather than the dynamic one. `enabled` is the caller's, because the
// per-instance list lives behind a dialog tab and polling it while the operator reads a
// different panel is wasted fan-out against the cluster.
export function useInstanceSnapshots(
  instanceName: string,
  host: string,
  project = "default",
  enabled = true,
  verifyFresh = false,
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: queryKeys.instanceSnapshots(scopeKey, host, project, instanceName),
    // note: `fresh` is deliberately NOT forwarded. It exists to bypass a backend response cache,
    // and the snapshot routes have none: they read the cluster on every call. Sending it anyway
    // spent the shared 12-per-minute cache-bypass budget on requests that had nothing to bypass,
    // so an operator watching a restore and clicking Refresh could exhaust the allowance for the
    // surfaces that really are cached. The refetch still happens; only the pointless parameter is
    // gone. Cancellation still propagates so changing actor or target cannot land a stale row.
    queryFn: ({ signal }) => api.snapshots.list(instanceName, host, project, { signal }),
    ...SEMI_STATIC_DATA_CONFIG,
    ...(verifyFresh ? { staleTime: 0, refetchOnMount: "always" as const } : {}),
    // After the spread on purpose: the preset carries its own `enabled` and would otherwise
    // re-enable a query the caller gated on the active tab.
    enabled: isAuthenticated && enabled && !!instanceName && !!host,
  });
}

/**
 * Snapshots across the fleet, or narrowed to one cluster and project.
 *
 * Undefined for either argument means "all", which is the page's default. Narrowing to BOTH is
 * what makes the backend measure sizes, so it is the only scope change that costs a request.
 *
 * note: no `enabled` gate. This used to be `enabled: !!host`, which was what stopped a tenant's
 * deep link firing a request before the redirect landed. The page now gates structurally instead,
 * returning before it mounts the component that calls this at all, so an `enabled` option here
 * would be a second mechanism for one job and the one nobody reads.
 */
export function useFleetSnapshots(host?: string, project?: string) {
  return useRefreshableQuery({
    queryKey: ["fleetSnapshots", host ?? null, project ?? null],
    // note: the read options are forwarded. This used to ignore them, which was harmless while
    // /snapshots had no response cache and became a real defect the moment it gained one: the
    // Refresh button would report success having read the cache it was pressed to bypass.
    queryFn: (opts) => api.snapshots.listFleet(host, project, opts),
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

// ── snapshot schedules ─────────────────────────────────────────────────────────────────────────

/** Every schedule the caller's organization can see, optionally narrowed to one instance. */
export function useSnapshotSchedules(
  filters: { host?: string; project?: string; instance?: string } = {},
  enabled = true,
) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: queryKeys.snapshotSchedules(
      scopeKey,
      filters.host,
      filters.project,
      filters.instance,
    ),
    queryFn: (opts) => api.snapshotSchedules.list(filters, opts),
    ...SEMI_STATIC_DATA_CONFIG,
    // After the spread on purpose: the preset carries its own `enabled` and its own `retry`.
    enabled: isAuthenticated && enabled,
    // important: a 404 here is the deployment saying the schedule routes are not mounted, which is
    // the documented default (SNAPSHOT_SCHEDULES_ENABLED is off unless set). The shared preset only
    // declines to retry 401, 403 and 429, so without this every mount of the snapshots page and of
    // every instance Snapshots tab costs three requests to learn the same permanent answer.
    retry: (failureCount: number, error: unknown) =>
      error instanceof APIError && error.status === 404
        ? false
        : SEMI_STATIC_DATA_CONFIG.retry(failureCount, error),
  });
}

export function useCreateSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: import("@/types/api").SnapshotScheduleCreateRequest) =>
      api.snapshotSchedules.create(body),
    onSuccess: () => reconcile(queryClient, "snapshot.schedule"),
  });
}

export function useUpdateSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: number;
      body: import("@/types/api").SnapshotScheduleUpdateRequest;
    }) =>
      api.snapshotSchedules.update(id, body),
    onSuccess: () => reconcile(queryClient, "snapshot.schedule"),
  });
}

export function usePauseSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.snapshotSchedules.pause(id),
    onSuccess: () => reconcile(queryClient, "snapshot.schedule"),
  });
}

export function useResumeSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.snapshotSchedules.resume(id),
    onSuccess: () => reconcile(queryClient, "snapshot.schedule"),
  });
}

export function useRunSnapshotScheduleNow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.snapshotSchedules.runNow(id),
    retry: false,
    onSuccess: () => reconcile(queryClient, "snapshot.schedule-run"),
  });
}

export function useDeleteSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.snapshotSchedules.remove(id),
    onSuccess: () => reconcile(queryClient, "snapshot.schedule"),
  });
}

export function useCreateSnapshot() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  return useMutation({
    mutationFn: ({ instanceName, body }: { instanceName: string; body: SnapshotCreateRequest }) =>
      api.snapshots.create(instanceName, body),
    // A timed-out POST can still have reached LXD. Retrying it can create a second restore point.
    retry: false,
    onSuccess: (result, { body }) => {
      void (async () => {
        await reconcileSnapshotState(queryClient, scopeKey);
        if (result.operation_id) {
          await reconcileSnapshotOperation(queryClient, scopeKey, result.operation_id, body.host);
        }
      })();
    },
  });
}

export function useDeleteSnapshot() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  return useMutation({
    mutationFn: ({
      instanceName,
      snapshotName,
      host,
      project,
      reason,
      acknowledgeBillingImpact,
      acknowledgeUpstreamSnapshot,
      confirmSnapshot,
    }: {
      instanceName: string;
      snapshotName: string;
      host: string;
      project?: string;
      reason?: string;
      acknowledgeBillingImpact?: boolean;
      acknowledgeUpstreamSnapshot?: boolean;
      confirmSnapshot?: string;
    }) =>
      api.snapshots.remove(instanceName, snapshotName, host, project, {
        reason,
        acknowledgeBillingImpact,
        acknowledgeUpstreamSnapshot,
        confirmSnapshot,
      }),
    retry: false,
    onSuccess: (result, { host }) => {
      void (async () => {
        await reconcileSnapshotState(queryClient, scopeKey);
        if (result.operation_id) {
          await reconcileSnapshotOperation(queryClient, scopeKey, result.operation_id, host);
        }
      })();
    },
  });
}

export function useRestoreSnapshot() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  return useMutation({
    mutationFn: ({
      instanceName,
      snapshotName,
      body,
    }: {
      instanceName: string;
      snapshotName: string;
      body: SnapshotRestoreRequest;
    }) => api.snapshots.restore(instanceName, snapshotName, body),
    retry: false,
    onSuccess: (result, { body }) => {
      void (async () => {
        await reconcileSnapshotState(queryClient, scopeKey);
        if (result.operation_id) {
          await reconcileSnapshotOperation(queryClient, scopeKey, result.operation_id, body.host);
        }
      })();
    },
  });
}

// Instance Mobility Hooks
type CopyOperationInfo = {
  status?: string | null;
  status_code?: number | null;
  err?: string | null;
};

type CopyOperationVerdict = "running" | "success" | "failure" | "partial" | "unknown";

const COPY_OPERATION_POLL_MS = 2_000;
const COPY_OPERATION_MAX_POLL_MS = 30 * 60_000;
const COPY_OPERATION_MAX_TRANSIENT_FAILURES = 4;

function copyOperationVerdict(operation: CopyOperationInfo): CopyOperationVerdict {
  const status = (operation.status ?? "").trim().toLowerCase();
  const statusCode = operation.status_code ?? 0;

  if (status.includes("partial")) return "partial";
  if (statusCode === 200 || ["success", "succeeded", "completed", "done"].includes(status)) {
    return "success";
  }
  if (
    statusCode >= 400 ||
    ["failure", "failed", "cancelled", "canceled", "error", "aborted"].includes(status)
  ) {
    return "failure";
  }
  if (
    (statusCode >= 100 && statusCode < 200) ||
    [
      "created",
      "pending",
      "starting",
      "started",
      "running",
      "cancelling",
      "canceling",
      "stopping",
      "freezing",
      "frozen",
      "thawed",
    ].includes(status)
  ) {
    return "running";
  }
  return "unknown";
}

function waitForCopyPoll(delayMs: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, delayMs));
}

async function reconcileSnapshotState(
  queryClient: QueryClient,
  actorScope: string,
): Promise<void> {
  if (!isAuthActorSynchronized(actorScope)) return;
  await reconcile(queryClient, "snapshot.write").catch(() => undefined);
  if (!isAuthActorSynchronized(actorScope)) return;
  await reconcileInstanceFleet(queryClient, actorScope, { fresh: true }).catch(() => undefined);
}

/** Follow the accepted daemon operation without ever replaying the snapshot mutation. */
async function reconcileSnapshotOperation(
  queryClient: QueryClient,
  actorScope: string,
  operationId: string,
  host: string,
): Promise<void> {
  const startedAt = Date.now();
  let transientFailures = 0;
  let unknownReads = 0;

  while (
    isAuthActorSynchronized(actorScope) &&
    Date.now() - startedAt < COPY_OPERATION_MAX_POLL_MS
  ) {
    try {
      const operation = await fetchAPI<CopyOperationInfo>(
        `/operations/${encodeURIComponent(operationId)}?host=${encodeURIComponent(host)}`,
        {},
        30_000,
        false,
        { force: true },
      );
      if (!isAuthActorSynchronized(actorScope)) return;

      transientFailures = 0;
      const verdict = copyOperationVerdict(operation);
      if (verdict === "success" || verdict === "failure" || verdict === "partial") {
        await reconcileSnapshotState(queryClient, actorScope);
        return;
      }
      if (verdict === "unknown") {
        unknownReads += 1;
        if (unknownReads >= COPY_OPERATION_MAX_TRANSIENT_FAILURES) break;
      } else {
        unknownReads = 0;
      }
    } catch (error) {
      if (!isAuthActorSynchronized(actorScope)) return;
      if (error instanceof APIError && error.status === 429) {
        const remainingMs = Math.max(
          0,
          COPY_OPERATION_MAX_POLL_MS - (Date.now() - startedAt),
        );
        await waitForCopyPoll(Math.min(
          remainingMs,
          Math.max(COPY_OPERATION_POLL_MS, (error.retryAfter ?? 30) * 1_000),
        ));
        continue;
      }
      if (error instanceof APIError && [401, 403, 404].includes(error.status)) break;
      transientFailures += 1;
      if (transientFailures >= COPY_OPERATION_MAX_TRANSIENT_FAILURES) break;
    }

    await waitForCopyPoll(COPY_OPERATION_POLL_MS);
  }

  // Unknown outcomes are read-reconciled once and never cause a second create/delete/restore.
  if (isAuthActorSynchronized(actorScope)) {
    await reconcileSnapshotState(queryClient, actorScope);
  }
}

async function reconcileCopyState(
  queryClient: QueryClient,
  actorScope: string,
): Promise<void> {
  if (!isAuthActorSynchronized(actorScope)) return;
  await reconcile(queryClient, "instance.copy").catch(() => undefined);
  if (!isAuthActorSynchronized(actorScope)) return;
  await reconcileInstanceFleet(queryClient, actorScope, { fresh: true }).catch(() => undefined);
}

/**
 * Follow only the operation returned by the accepted copy. This function never repeats the
 * mutation: a missing, expired, or unreadable operation becomes an unknown outcome and relies on
 * the same authoritative fleet reconciliation as every explicit terminal verdict.
 */
async function reconcileCopyOperation(
  queryClient: QueryClient,
  actorScope: string,
  operationId: string,
  targetHost: string,
): Promise<void> {
  const startedAt = Date.now();
  let transientFailures = 0;
  let unknownReads = 0;

  while (
    isAuthActorSynchronized(actorScope) &&
    Date.now() - startedAt < COPY_OPERATION_MAX_POLL_MS
  ) {
    try {
      const operation = await fetchAPI<CopyOperationInfo>(
        `/operations/${encodeURIComponent(operationId)}?host=${encodeURIComponent(targetHost)}`,
        {},
        30_000,
        false,
        { force: true },
      );
      if (!isAuthActorSynchronized(actorScope)) return;

      transientFailures = 0;
      const verdict = copyOperationVerdict(operation);
      if (verdict === "success" || verdict === "failure" || verdict === "partial") {
        await reconcileCopyState(queryClient, actorScope);
        return;
      }
      if (verdict === "unknown") {
        unknownReads += 1;
        if (unknownReads >= COPY_OPERATION_MAX_TRANSIENT_FAILURES) break;
      } else {
        unknownReads = 0;
      }
    } catch (error) {
      if (!isAuthActorSynchronized(actorScope)) return;
      if (error instanceof APIError && error.status === 429) {
        const remainingMs = Math.max(
          0,
          COPY_OPERATION_MAX_POLL_MS - (Date.now() - startedAt),
        );
        await waitForCopyPoll(Math.min(
          remainingMs,
          Math.max(COPY_OPERATION_POLL_MS, (error.retryAfter ?? 30) * 1_000),
        ));
        continue;
      }
      if (error instanceof APIError && [401, 403, 404].includes(error.status)) break;
      transientFailures += 1;
      if (transientFailures >= COPY_OPERATION_MAX_TRANSIENT_FAILURES) break;
    }

    await waitForCopyPoll(COPY_OPERATION_POLL_MS);
  }

  // Unknown never replays the copy. One final read reconciliation is safe and converges if the
  // destination appeared while the operation record expired or the status endpoint degraded.
  if (isAuthActorSynchronized(actorScope)) {
    await reconcileCopyState(queryClient, actorScope);
  }
}

export function useMovePreflight(
  instanceName: string,
  host: string,
  project = "default",
  enabled = true,
) {
  return useQuery({
    queryKey: ["movePreflight", instanceName, host, project],
    queryFn: () => api.mobility.preflight(instanceName, host, project),
    ...ON_DEMAND_CONFIG,
    enabled: enabled && !!instanceName && !!host,
    // important: re-asserted after the spread, for the same reason useInstance does it above. A
    // preflight is a reachability reading taken when the dialog opens, and the preset holds one
    // for five minutes with refetchOnMount off. The dialog does not unmount when it closes, so
    // `enabled` flips while the observer stays alive and refetchOnMount alone never fires: a
    // destination that was down on the first open stayed marked unreachable for five minutes
    // after ops brought it back, with no control anywhere to say otherwise. The cached value
    // still paints immediately while the fresh one lands, because gcTime keeps it.
    staleTime: 0,
    refetchOnMount: "always" as const,
  });
}

export function useCopyInstance() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  return useMutation({
    mutationFn: ({
      instanceName,
      body,
      idempotencyKey,
    }: {
      instanceName: string;
      body: MoveRequest;
      idempotencyKey: string;
    }) => api.mobility.copy(instanceName, body, idempotencyKey),
    retry: false,
    onSuccess: (result) => {
      // Do not hold the accepted mutation open while a large transfer settles. The returned
      // operation id is retained by React Query and followed in the background without replaying
      // the copy request.
      void (async () => {
        if (result.migration_job_id) {
          await queryClient.invalidateQueries({
            queryKey: ["migrations", scopeKey],
          });
          return;
        }
        await reconcileCopyState(queryClient, scopeKey);
        if (result.operation_id) {
          await reconcileCopyOperation(
            queryClient,
            scopeKey,
            result.operation_id,
            result.target_host,
          );
        }
      })();
    },
  });
}
