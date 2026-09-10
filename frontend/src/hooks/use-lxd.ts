/**
 * Custom React Query hooks for Orcastra CMP
 */

import { useQuery, useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { usePermissions } from "@/hooks/use-permissions";
import { useEffect, useState } from "react";
import { useSession } from "next-auth/react";
import { APIError } from "@/types/api";
import { 
  vmAPI, 
  imageAPI, 
  nodeAPI, 
  projectAPI, 
  tagAPI, 
  healthCheck,
  api 
} from "@/lib/api";
import { VMCreateRequest, InstanceCreateRequest, VMActionRequest, ProjectCreateRequest, VMListResponse, SnapshotCreateRequest, SnapshotRestoreRequest, MoveRequest } from "@/types";
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

// Query Keys
export const queryKeys = {
  vms: ["vms"] as const,
  vm: (name: string, host: string) => ["vm", name, host] as const,
  instanceMetrics: (host: string, instanceName: string, project: string) => 
    ["instanceMetrics", host, instanceName, project] as const,
  images: ["images"] as const,
  imagesByHost: (host: string) => ["images", host] as const,
  nodes: ["nodes"] as const,
  nodeNames: ["nodeNames"] as const,
  nodeAvailability: ["nodeAvailability"] as const,
  projects: (host: string) => ["projects", host] as const,
  allProjects: ["projects", "all"] as const,
  health: ["health"] as const,
};

function useMonitoringScopeKey() {
  const { data: session } = useSession();
  const user = session?.user as any;
  const groups = Array.isArray(user?.groups) ? [...user.groups].sort().join(",") : "";
  return `${user?.id ?? user?.email ?? "anonymous"}:${groups}`;
}

function normalizeProject(project?: string | null) {
  return project || "default";
}

function refreshInstanceQueries(queryClient: QueryClient) {
  void queryClient.invalidateQueries({ queryKey: queryKeys.vms });
  void queryClient.invalidateQueries({ queryKey: ["instances"] });
}

function removeInstanceFromList(
  data: VMListResponse | undefined,
  name: string,
  host: string,
  project?: string | null
) {
  if (!data?.vms) return data;

  const filteredVms = data.vms.filter((vm) => {
    const matchesInstance = vm.name === name && vm.host === host;
    const matchesProject = !project || normalizeProject(vm.project) === normalizeProject(project);
    return !(matchesInstance && matchesProject);
  });

  const removedCount = data.vms.length - filteredVms.length;
  if (removedCount === 0) return data;

  return {
    ...data,
    total: Math.max(0, data.total - removedCount),
    vms: filteredVms,
  };
}

function removeInstanceFromCaches(
  queryClient: QueryClient,
  name: string,
  host: string,
  project?: string | null
) {
  queryClient.setQueriesData<VMListResponse | undefined>(
    { queryKey: queryKeys.vms },
    (data) => removeInstanceFromList(data, name, host, project)
  );
  queryClient.setQueriesData<VMListResponse | undefined>(
    { queryKey: ["instances"] },
    (data) => removeInstanceFromList(data, name, host, project)
  );
}

// Default retry configuration with exponential backoff
const defaultRetryConfig = {
  retry: 3,
  retryDelay: (attemptIndex: number) => Math.min(1000 * 2 ** attemptIndex, 30000),
};

// VMs Hooks
export function useVMs(host?: string, status?: string) {
  return useQuery({
    queryKey: [...queryKeys.vms, { host, status }],
    queryFn: () => vmAPI.list(host, status),
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: jitter(POLL_MS.fleet),
    refetchIntervalInBackground: false,
    ...STATIC_DATA_CONFIG, // Cache for 3min; manual refresh via button
  });
}

export function useVM(name: string, host: string) {
  return useQuery({
    queryKey: queryKeys.vm(name, host),
    queryFn: () => vmAPI.get(name, host),
    enabled: !!name && !!host,
  });
}

export function useCreateVM() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (data: VMCreateRequest) => vmAPI.create(data),
    onSuccess: () => {
      refreshInstanceQueries(queryClient);
    },
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
    onSuccess: (_data, variables) => {
      removeInstanceFromCaches(queryClient, variables.name, variables.host, variables.project);
      refreshInstanceQueries(queryClient);
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
  return useQuery({
    queryKey: queryKeys.instanceMetrics(host, instanceName, project),
    queryFn: () => vmAPI.getMetrics(host, instanceName, project),
    enabled: enabled && !!host && !!instanceName,
    refetchInterval: 10000, // Poll every 10 seconds (balanced freshness vs load)
    refetchIntervalInBackground: false, // Stop polling when tab is hidden
    staleTime: 5000, // Consider fresh for 5 seconds
    retry: 2,
    refetchOnWindowFocus: false,
  });
}

// Images Hooks
export function useImages(host?: string) {
  return useRefreshableQuery({
    queryKey: host ? queryKeys.imagesByHost(host) : queryKeys.images,
    queryFn: async ({ fresh }) => {
      if (host) {
        const result = await imageAPI.listByHost(host);
        return [result]; // Normalize to array
      }
      return imageAPI.listAll({ fresh });
    },
    // Always enabled - fetches all images when host is undefined, or filtered when host is provided
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: jitter(POLL_MS.fleet),
    refetchIntervalInBackground: false,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useImagesByHost(host: string) {
  return useQuery({
    queryKey: queryKeys.imagesByHost(host),
    queryFn: () => imageAPI.listByHost(host),
    enabled: !!host,
    staleTime: 60000,
  });
}

export function useDeleteImage() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ fingerprint, host, force }: { fingerprint: string; host: string; force?: boolean }) =>
      imageAPI.delete(fingerprint, host, force),
    onSuccess: () => {
      // queryKeys.images is ["images"] and imagesByHost is ["images", host], so one
      // invalidation covers the fleet list and every per-host list the create form holds.
      queryClient.invalidateQueries({ queryKey: queryKeys.images });
    },
  });
}

export function useRemoteImages() {
  return useRefreshableQuery({
    queryKey: ["remoteImages", "all"],
    queryFn: ({ fresh }) => imageAPI.listRemote({ fresh }),
    // Remote catalog changes ~daily and the backend caches it for 1h.
    staleTime: 1000 * 60 * 30,
    gcTime: 1000 * 60 * 60,
    refetchOnWindowFocus: false,
  });
}

// Nodes Hooks
export function useNodesStatus() {
  // note: lazy initialiser, so the cadence is fixed per hook instance rather than wandering
  // on every render. Jitter keeps a page of cards from missing an expired TTL in one burst.
  const [fleetInterval] = useState(() => jitter(POLL_MS.fleet));
  return useRefreshableQuery({
    queryKey: queryKeys.nodes,
    queryFn: ({ fresh }) => nodeAPI.status({ fresh }),
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
  return useQuery({
    queryKey: queryKeys.nodeNames,
    queryFn: () => nodeAPI.names(),
    // note: this was staleTime: Infinity with refetchOnMount: false. Membership does change,
    // and registering a cluster then opening a create form showed a stale list until a hard
    // reload, because the register mutation invalidates queryKeys.nodes and not this key.
    // Three minutes plus the invalidation below is the fix.
    staleTime: 1000 * 60 * 3,
    gcTime: 1000 * 60 * 60 * 24, // keep in cache for 24 hours
    refetchOnWindowFocus: false,
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
  return useRefreshableQuery({
    queryKey: queryKeys.nodeAvailability,
    queryFn: ({ fresh }) => nodeAPI.availability({ fresh }),
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.nodes });
    },
  });
}

export function useInvalidateNodeCache() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (nodeName: string) => nodeAPI.invalidateCache(nodeName),
    onSuccess: () => {
      // Also invalidate React Query cache to force re-fetch
      queryClient.invalidateQueries({ queryKey: queryKeys.nodes });
    },
  });
}

export function useInvalidateAllNodesCache() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: () => nodeAPI.invalidateAllCache(),
    onSuccess: () => {
      // Invalidate all node-related React Query caches
      queryClient.invalidateQueries({ queryKey: queryKeys.nodes });
      queryClient.invalidateQueries({ queryKey: queryKeys.vms });
    },
  });
}

/**
 * Live resources for one cluster, for a surface someone is looking at right now.
 *
 * important: GET /nodes/{node}/resources is uncached on the backend, so every poll is a real
 * call into that one cluster. Gate it on the panel being open, and back off geometrically once
 * it starts failing rather than holding a dead host on a connect timeout forever. The query key
 * is per node, which is the only place a per-cluster back-off is even expressible: /nodes is
 * one fan-out and cannot back off for a single member of it.
 */
export function useNodeResources(nodeName: string, opts?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ["nodeResources", nodeName],
    queryFn: () => nodeAPI.resources(nodeName),
    ...DYNAMIC_DATA_CONFIG,
    enabled: (opts?.enabled ?? true) && !!nodeName,
    refetchInterval: backingOffInterval(POLL_MS.active),
    refetchIntervalInBackground: false,
  });
}

export function useNodeGpus(nodeName: string) {
  return useQuery({
    queryKey: ["nodeGpus", nodeName],
    queryFn: () => nodeAPI.gpus(nodeName),
    enabled: !!nodeName,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

// Projects Hooks
export function useAllProjects(options: { enabled?: boolean } = {}) {
  return useRefreshableQuery({
    queryKey: queryKeys.allProjects,
    // note: opts carries {fresh} through to fetchRead, which appends the cache-bypass
    // parameter. Without it a Retry inside the API-key project picker would re-read the same
    // 30s server cache and report success on the identical unreachable cluster.
    queryFn: (opts) => projectAPI.listAll(opts),
    enabled: options.enabled ?? true,
    refetchInterval: 300000, // refresh every 5 minutes (300 seconds)
    staleTime: 30000, // Consider data stale after 30 seconds
    retry: (failureCount: number, error: unknown) => {
      // don't retry auth/rate-limit errors
      if (error instanceof Error && /401|403|429/.test(error.message)) return false;
      return failureCount < 2;
    },
    retryDelay: (attemptIndex: number) => Math.min(1000 * 2 ** attemptIndex, 10000),
    refetchOnWindowFocus: false,
  });
}

export function useProjects(host: string) {
  return useQuery({
    queryKey: queryKeys.projects(host),
    queryFn: () => projectAPI.list(host),
    enabled: !!host,
    staleTime: 30000,
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (data: ProjectCreateRequest) => projectAPI.create(data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projects(variables.host) });
      queryClient.invalidateQueries({ queryKey: queryKeys.allProjects });
    },
  });
}

export function useDeleteProject() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ host, projectName }: { host: string; projectName: string }) => 
      projectAPI.delete(host, projectName),
    onSuccess: async (_, variables) => {
      // Invalidate and refetch to ensure UI updates immediately
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects(variables.host) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.allProjects });
      // Force refetch to update the UI immediately
      await queryClient.refetchQueries({ queryKey: queryKeys.allProjects });
    },
  });
}

export function useUpdateProject() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ host, projectName, data }: { host: string; projectName: string; data: any }) => 
      projectAPI.update(host, projectName, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projects(variables.host) });
      queryClient.invalidateQueries({ queryKey: queryKeys.allProjects });
    },
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.vms });
    },
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
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projects(variables.host) });
      queryClient.invalidateQueries({ queryKey: queryKeys.allProjects });
    },
  });
}

export function useUpdateNodeTags() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ nodeName, tags }: { nodeName: string; tags: string[] }) => 
      tagAPI.updateNodeTags(nodeName, tags),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.nodes });
    },
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.categories });
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.tags });
    },
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.categories });
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.tags });
    },
  });
}

/**
 * Hook to create a new tag.
 */
export function useCreateTag() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: import("@/types/api").TagCreate) => tagAPI.createTag(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.tags });
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.tagStats });
    },
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.tags });
    },
  });
}

/**
 * Hook to delete a tag.
 */
export function useDeleteTag() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (tagId: number) => tagAPI.deleteTag(tagId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.tags });
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.tagStats });
    },
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
    onSuccess: (_, variables) => {
      // Invalidate resource-specific tag cache
      queryClient.invalidateQueries({
        queryKey: tagQueryKeys.resourceTags(
          variables.resourceType,
          variables.resourceId,
          variables.host,
          variables.project
        ),
      });
      // Invalidate related data
      if (variables.resourceType === "node") {
        queryClient.invalidateQueries({ queryKey: queryKeys.nodes });
      } else if (variables.resourceType === "project") {
        queryClient.invalidateQueries({ queryKey: queryKeys.allProjects });
        if (variables.host) {
          queryClient.invalidateQueries({ queryKey: queryKeys.projects(variables.host) });
        }
      } else if (variables.resourceType === "instance") {
        queryClient.invalidateQueries({ queryKey: queryKeys.vms });
      }
      // Update tag stats
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.tagStats });
    },
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
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({
        queryKey: tagQueryKeys.resourceTags(
          variables.resourceType,
          variables.resourceId,
          variables.host,
          variables.project
        ),
      });
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.tagStats });
    },
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
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({
        queryKey: tagQueryKeys.resourceTags(
          variables.resourceType,
          variables.resourceId,
          variables.host,
          variables.project
        ),
      });
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.tagStats });
    },
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.userVisibility });
      // Also invalidate tag list as visibility affects what user sees
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.tags });
    },
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: tagQueryKeys.categories });
    },
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
export function useWarnings(host?: string) {
  const { isPartner } = usePermissions();
  return useRefreshableQuery({
    queryKey: ["warnings", host],
    queryFn: ({ fresh }) => api.warnings.list(host, { fresh }),
    ...DYNAMIC_DATA_CONFIG,
    enabled: isPartner,
    refetchInterval: 60000, // Check warnings every minute
  });
}

// Operations Hooks
export function useOperations(host?: string) {
  return useRefreshableQuery({
    queryKey: ["operations", host],
    queryFn: ({ fresh }) => api.operations.list(host, { fresh }),
    ...DYNAMIC_DATA_CONFIG,
    refetchInterval: 15000, // Poll every 15s (balanced freshness vs load)
    refetchIntervalInBackground: false,
  });
}

export function useOperation(id: string, host: string) {
  return useQuery({
    queryKey: ["operation", id, host],
    queryFn: () => api.operations.get(id, host),
    enabled: !!id && !!host,
    ...DYNAMIC_DATA_CONFIG,
  });
}

// IPAM Hooks
export function useIPAM(host?: string) {
  return useRefreshableQuery({
    queryKey: ["ipam", host],
    queryFn: ({ fresh }) => api.ipam.list(host, { fresh }),
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
const STORAGE_POOL_KEYS = ["storagePools", STORAGE_POOL_LIST_KEY] as const;

function invalidateStoragePools(queryClient: ReturnType<typeof useQueryClient>) {
  STORAGE_POOL_KEYS.forEach((key) => queryClient.invalidateQueries({ queryKey: [key] }));
}

// important: `useStoragePools` used to resolve the per-node list and `resources-all` in one
// unguarded Promise.all, so a slow or failing fan-out across every cluster took the pool list
// down with it, and TanStack reported that error state identically to "this node has no pools".
// The create form builds `devices.root` from the resolved pool, so it read an unrelated
// cluster's timeout as "no storage here" and refused a create it could have made. Anything that
// only needs the list should still use this hook, which never pays for the capacity read at
// all; the combined hook below now degrades instead of failing.
export function useStoragePoolList(host?: string) {
  return useRefreshableQuery({
    queryKey: [STORAGE_POOL_LIST_KEY, host],
    queryFn: ({ fresh }) => api.storagePools.list(host, { fresh }),
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useStoragePools(host?: string) {
  return useRefreshableQuery({
    queryKey: ["storagePools", host],
    queryFn: async ({ fresh }) => {
      // important: the capacity read is secondary and must not be able to fail the list. It
      // fans out to every cluster, so it is the likelier of the two to time out, and losing it
      // used to blank the whole page under "No storage pools found" while the list itself had
      // answered. An operator reading that would look for pools that were never gone.
      // `capacityFailed` is what lets the surface say which of the two happened, instead of
      // rendering an absent reading the same way as a reading of nothing.
      const [poolsData, resourcesData] = await Promise.all([
        api.storagePools.list(host, { fresh }) as Promise<Record<string, unknown>>,
        (api.storagePools.getResourcesAll({ fresh }) as Promise<Record<string, unknown> | null>)
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
    refetchInterval: jitter(POLL_MS.fleet),
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
    onSuccess: () => {
      invalidateStoragePools(queryClient);
    },
  });
}

export function useDeleteStoragePool() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.storagePools.delete(name, host),
    onSuccess: () => {
      invalidateStoragePools(queryClient);
    },
  });
}

// Storage Volumes Hooks
export function useStorageVolumes(pool?: string, host?: string) {
  return useRefreshableQuery({
    queryKey: ["storageVolumes", pool, host],
    queryFn: ({ fresh }) => api.storageVolumes.list(host, pool, { fresh }),
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: jitter(POLL_MS.fleet),
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["storageVolumes"] });
    },
  });
}

export function useDeleteStorageVolume() {
  const queryClient = useQueryClient();

  return useMutation({
    // important: same scrambled argument order as useStorageVolume above. See the note there.
    mutationFn: ({ pool, type, name, host }: { pool: string; type: string; name: string; host: string }) =>
      api.storageVolumes.delete(type, name, host, pool),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["storageVolumes"] });
    },
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["instances"] });
      queryClient.invalidateQueries({ queryKey: ["instance"] });
    },
  });
}

// Networks Hooks
/**
 * @param project omit for every project, each row naming the one that holds it. Pass one to
 *   get the set that project can actually use, which is what a picker needs: a project only
 *   holds its own networks when it sets features.networks, otherwise it sees default's.
 */
export function useNetworks(host?: string, project?: string) {
  return useRefreshableQuery({
    // important: the project belongs in the key. Without it React Query serves one project's
    // rows to another project's caller from cache.
    queryKey: ["networks", host, project ?? null],
    queryFn: ({ fresh }) => api.networks.list(host, project, { fresh }),
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: jitter(POLL_MS.fleet),
    refetchIntervalInBackground: false,
    // Always enabled - fetches all networks when host is undefined
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useNetwork(name: string, host: string, project?: string) {
  return useQuery({
    queryKey: ["network", name, host, project ?? null],
    queryFn: () => api.networks.get(name, host, project),
    enabled: !!name && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useNetworkState(name: string, host: string, project?: string) {
  return useQuery({
    queryKey: ["networkState", name, host, project ?? null],
    queryFn: () => api.networks.getState(name, host, project),
    enabled: !!name && !!host,
    ...DYNAMIC_DATA_CONFIG,
  });
}

export function useCreateNetwork() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host, project }: { data: any; host: string; project?: string }) =>
      api.networks.create(data, host, project),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["networks"] });
    },
  });
}

export function useDeleteNetwork() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host, project }: { name: string; host: string; project?: string }) =>
      api.networks.delete(name, host, project),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["networks"] });
    },
  });
}

// Network ACLs Hooks
/** @param project omit for every project. See useNetworks. */
export function useNetworkACLs(host?: string, project?: string) {
  return useRefreshableQuery({
    queryKey: ["networkACLs", host, project ?? null],
    queryFn: ({ fresh }) => api.networkACLs.list(host, project, { fresh }),
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

export function useCreateNetworkACL() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host, project }: { data: any; host: string; project?: string }) =>
      api.networkACLs.create(data, host, project),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["networkACLs"] });
    },
  });
}

export function useDeleteNetworkACL() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host, project }: { name: string; host: string; project?: string }) =>
      api.networkACLs.delete(name, host, project),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["networkACLs"] });
    },
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
    }: {
      name: string;
      host: string;
      data: any;
      project?: string;
    }) => api.networkACLs.update(name, host, data, project),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["networkACLs"] });
      queryClient.invalidateQueries({ queryKey: ["networkACL"] });
    },
  });
}

// Profiles Hooks
/** @param project omit for every project. See useNetworks. */
export function useProfiles(host?: string, project?: string) {
  return useRefreshableQuery({
    queryKey: ["profiles", host, project ?? null],
    queryFn: ({ fresh }) => api.profiles.list(host, project, { fresh }),
    // Always enabled - fetches all profiles when host is undefined
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useProfile(name: string, host: string, project?: string) {
  return useQuery({
    queryKey: ["profile", name, host, project ?? null],
    queryFn: () => api.profiles.get(name, host, project),
    enabled: !!name && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useCreateProfile() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host, project }: { data: any; host: string; project?: string }) =>
      api.profiles.create(data, host, project),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["profiles"] });
    },
  });
}

export function useDeleteProfile() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host, project }: { name: string; host: string; project?: string }) =>
      api.profiles.delete(name, host, project),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["profiles"] });
    },
  });
}

// Instances Hooks (alternative to VMs for LXD terminology)
export function useInstances(host?: string, opts?: { realtime?: boolean }) {
  return useRefreshableQuery({
    queryKey: ["instances", host],
    queryFn: ({ fresh }) =>
      api.instances.list(host, undefined, undefined, undefined, undefined, { fresh }),
    // Always enabled - fetches all instances when host is undefined
    ...DYNAMIC_DATA_CONFIG,
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
// rather than polled. It is also the only way to get `resources`: the list path sets it to None
// (services/lxd/instance.py:427) while the single-get path populates it (:266).
//
// important: `enabled` is re-asserted AFTER the spread. ON_DEMAND_CONFIG sets enabled false by
// design, so spreading it last would silently disable every caller.
export function useInstance(name: string, host: string, enabled = true, project?: string) {
  return useQuery({
    queryKey: ["instance", name, host, project ?? null],
    queryFn: () => api.instances.get(name, host, project),
    ...ON_DEMAND_CONFIG,
    enabled: enabled && !!name && !!host,
    // important: both re-asserted after the spread, and staleTime is the one that does the work.
    // ON_DEMAND_CONFIG holds a reading for five minutes, and the dialog does not unmount when it
    // closes: `enabled` flips while the observer stays alive, so refetchOnMount alone never
    // fires and reopening redrew the bar from a five-minute-old sample. A stale reading is
    // tolerable as a static number and misleading as a bar, and the section tells the operator
    // it is a reading taken on open, so it has to actually be one. The cached value still paints
    // immediately while the fresh one lands, because gcTime keeps it.
    staleTime: 0,
    refetchOnMount: "always",
  });
}

export function useCreateInstance() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: InstanceCreateRequest) => api.instances.create(data),
    onSuccess: () => {
      refreshInstanceQueries(queryClient);
    },
  });
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
  
  return useMutation({
    mutationFn: ({ name, host, force, project }: { name: string; host: string; force?: boolean; project?: string }) =>
      api.vms.delete(name, host, force, project),
    onSuccess: (_data, variables) => {
      removeInstanceFromCaches(queryClient, variables.name, variables.host, variables.project);
      refreshInstanceQueries(queryClient);
    },
  });
}

// Server Info Hooks
export function useServerInfo(host?: string) {
  return useRefreshableQuery({
    queryKey: ["serverInfo", host],
    queryFn: ({ fresh }) => api.server.list(host, { fresh }),
    ...STATIC_DATA_CONFIG,
  });
}

export function useServerResources(host: string) {
  return useQuery({
    queryKey: ["serverResources", host],
    queryFn: () => api.server.getResources(host),
    enabled: !!host,
    ...DYNAMIC_DATA_CONFIG,
  });
}

// Identities Hooks
export function useIdentities(host?: string) {
  return useRefreshableQuery({
    queryKey: ["identities", host],
    queryFn: ({ fresh }) => api.identities.list(host, { fresh }),
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useIdentity(id: string, host: string) {
  return useQuery({
    queryKey: ["identity", id, host],
    queryFn: () => api.identities.get(id, host),
    enabled: !!id && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useDeleteIdentity() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ authType, identityId, host }: { authType: string; identityId: string; host: string }) => 
      api.identities.delete(authType, identityId, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["identities"] });
      queryClient.invalidateQueries({ queryKey: ["groups"] });
    },
  });
}

// Groups Hooks
export function useGroups(host?: string) {
  return useRefreshableQuery({
    queryKey: ["groups", host],
    queryFn: ({ fresh }) => api.groups.list(host, { fresh }),
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useGroup(name: string, host: string) {
  return useQuery({
    queryKey: ["group", name, host],
    queryFn: () => api.groups.get(name, host),
    enabled: !!name && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useCreateGroup() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host }: { data: any; host: string }) => 
      api.groups.create(data, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["groups"] });
    },
  });
}

export function useUpdateGroup() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, data, host }: { name: string; data: any; host: string }) => 
      api.groups.update(name, data, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["groups"] });
      queryClient.invalidateQueries({ queryKey: ["group"] });
    },
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["groups"] });
      queryClient.invalidateQueries({ queryKey: ["group"] });
    },
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["groups"] });
      queryClient.invalidateQueries({ queryKey: ["group"] });
    },
  });
}

export function useDeleteGroup() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.groups.delete(name, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["groups"] });
    },
  });
}

// Cluster Members Hooks
export function useClusterMembers(node: string) {
  return useRefreshableQuery({
    queryKey: ["clusterMembers", node],
    queryFn: ({ fresh }) => api.clusterMembers.list(node, { fresh }),
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: jitter(POLL_MS.fleet),
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
  return useRefreshableQuery({
    queryKey: ["memberResources", node, memberName],
    // note: getResources routes through the extended fan-out budget, which was unreachable
    // from the UI while this hook never passed a fresh flag.
    queryFn: ({ fresh }) => api.clusterMembers.getResources(node, memberName, { fresh }),
    // note: gated so a closed modal stops polling. The backend caches this for 60s, so the
    // interval matches: anything faster returns the same body.
    enabled: (opts?.enabled ?? true) && !!node && !!memberName,
    staleTime: 30 * 1000, // 30 seconds
    gcTime: 5 * 60 * 1000, // 5 minutes
    refetchInterval: backingOffInterval(POLL_MS.attended),
    refetchIntervalInBackground: false,
  });
}

// Clusters Hooks
export function useClusters(fresh: boolean = false) {
  return useRefreshableQuery({
    queryKey: ["clusters", { fresh }],
    queryFn: ({ fresh: manual }) => api.clusters.list(fresh || manual),
    // Matched to the 180s backend response cache: capacity and state move without
    // anyone acting in this console, so a manual button alone was not enough.
    refetchInterval: jitter(POLL_MS.fleet),
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
    onSuccess: async () => {
      // Use refetchQueries to ensure data is actually refreshed
      await Promise.all([
        queryClient.refetchQueries({ queryKey: ["clusters"], type: 'active' }),
        queryClient.refetchQueries({ queryKey: queryKeys.nodes, type: 'active' }),
      ]);
      // Also invalidate for inactive queries
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
      queryClient.invalidateQueries({ queryKey: queryKeys.nodes });
      // the picker's membership and status keys, which used to be left stale here: a newly
      // registered cluster did not appear in any create form until a hard reload
      queryClient.invalidateQueries({ queryKey: queryKeys.nodeNames });
      queryClient.invalidateQueries({ queryKey: queryKeys.nodeAvailability });
      queryClient.invalidateQueries({ queryKey: queryKeys.vms });
      queryClient.invalidateQueries({ queryKey: queryKeys.allProjects });
      queryClient.invalidateQueries({ queryKey: ["instances"] });
      queryClient.invalidateQueries({ queryKey: ["facilities"] });
      queryClient.invalidateQueries({ queryKey: ["regions"] });
    },
  });
}

export function useCheckClusterAuth() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (clusterId: string) => api.clusters.checkAuth(clusterId),
    onSuccess: async () => {
      // First pass: immediate refetch so clusters list updates
      await queryClient.refetchQueries({ queryKey: ["clusters"], type: 'active' });
      await queryClient.invalidateQueries({ queryKey: queryKeys.nodeAvailability });
      
      // Remove stale cached data for instances/VMs/projects
      // so the next fetch pulls fresh data including the new cluster
      queryClient.removeQueries({ queryKey: queryKeys.vms });
      queryClient.removeQueries({ queryKey: ["instances"] });
      queryClient.removeQueries({ queryKey: queryKeys.allProjects });
      
      // Small delay to let the backend's LXD connection stabilize
      await new Promise(resolve => setTimeout(resolve, 1500));
      
      // Second pass: refetch instance data from scratch
      await Promise.all([
        queryClient.refetchQueries({ queryKey: queryKeys.nodes, type: 'active' }),
        queryClient.refetchQueries({ queryKey: queryKeys.vms, type: 'active' }),
        queryClient.refetchQueries({ queryKey: queryKeys.allProjects, type: 'active' }),
        queryClient.refetchQueries({ queryKey: ["instances"], type: 'active' }),
        queryClient.refetchQueries({ queryKey: ["regions"], type: 'active' }),
        queryClient.refetchQueries({ queryKey: ["groups"], type: 'active' }),
        queryClient.refetchQueries({ queryKey: ["identities"], type: 'active' }),
      ]);
      // Also invalidate to ensure any inactive queries will refetch when they become active
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
      queryClient.invalidateQueries({ queryKey: queryKeys.nodes });
      queryClient.invalidateQueries({ queryKey: ["regions"] });
    },
  });
}

export function useDeleteCluster() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (clusterId: string) => api.clusters.delete(clusterId),
    onSuccess: async () => {
      // Remove cached data and force fresh fetch
      await queryClient.invalidateQueries({ queryKey: ["clusters"], refetchType: 'all' });
      await queryClient.invalidateQueries({ queryKey: queryKeys.nodes, refetchType: 'all' });
      await queryClient.invalidateQueries({ queryKey: queryKeys.nodeNames, refetchType: 'all' });
      await queryClient.invalidateQueries({ queryKey: queryKeys.nodeAvailability, refetchType: 'all' });
      await queryClient.invalidateQueries({ queryKey: ["certificates"], refetchType: 'all' });
    },
  });
}

export function useRegenerateCert() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (clusterId: string) => api.clusters.regenerateCert(clusterId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
    },
  });
}

export function useUpdateClusterLocation() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ clusterId, location }: { clusterId: string; location: any }) => 
      api.clusters.updateLocation(clusterId, location),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
      queryClient.invalidateQueries({ queryKey: ["regions"] });
      queryClient.invalidateQueries({ queryKey: ["facilities"] });
    },
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

export function useMonitoringDashboard(enabled: boolean = true, refetchInterval: number = 10000) {
  const scopeKey = useMonitoringScopeKey();
  return useRefreshableQuery({
    queryKey: ["monitoring", "dashboard", scopeKey],
    queryFn: ({ fresh }) => api.monitoring.getDashboard({ fresh }),
    enabled,
    refetchInterval: refetchInterval > 0 ? refetchInterval : false, // 0 = disabled (WS push active)
    staleTime: 5000, // Consider fresh for 5 seconds to avoid redundant fetches
    gcTime: 60 * 1000,
    refetchOnWindowFocus: refetchInterval > 0, // Only refetch on focus when polling
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

export function useMonitoringHistory(points: number = 60, enabled: boolean = true) {
  const queryClient = useQueryClient();
  const scopeKey = useMonitoringScopeKey();

  // Prefetch all time ranges on mount for instant switching
  // Backend reads from the same in-memory cache, so these are very fast
  useEffect(() => {
    if (!enabled) return;
    const allRanges = [60, 180, 360, 720, 4320, 8640];
    // Sort by proximity to current range (prefetch nearest first)
    const sortedRanges = allRanges
      .filter(r => r !== points)
      .sort((a, b) => Math.abs(a - points) - Math.abs(b - points));

    sortedRanges.forEach((range, idx) => {
      // Stagger prefetches slightly to avoid burst
      setTimeout(() => {
        queryClient.prefetchQuery({
          queryKey: ["monitoring", "history", scopeKey, range],
          queryFn: () => api.monitoring.getHistory(range),
          staleTime: 30000,
        });
      }, idx * 200);
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, scopeKey]); // Only on mount/enable change - not on points change

  return useQuery({
    queryKey: ["monitoring", "history", scopeKey, points],
    queryFn: async () => {
      try {
        const data = await api.monitoring.getHistory(points);
        return data;
      } catch (error) {
        console.error(`Failed to fetch history for ${points} points:`, error);
        throw error;
      }
    },
    enabled,
    refetchInterval: 15000, // Update history every 15 seconds
    staleTime: 30000, // 30 seconds - prevents refetch when switching back to a recently visited range
    gcTime: 600000, // 10 minutes - keep all time range caches alive for fast switching
    refetchIntervalInBackground: false, // Stop polling when tab is hidden
    retry: 2, // Retry up to 2 times on failure
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 5000),
    placeholderData: (previousData) => {
      // Keep previous data for seamless display while fetching
      if (previousData) return previousData;
      // Cross-range fallback: prefer closest cached range for best visual fit
      const cachedRanges = [60, 180, 360, 720, 4320, 8640]
        .filter(r => r !== points)
        .sort((a, b) => Math.abs(a - points) - Math.abs(b - points));
      for (const r of cachedRanges) {
        const cached = queryClient.getQueryData<any[]>(["monitoring", "history", scopeKey, r]);
        if (cached && cached.length > 0) return cached;
      }
      return undefined;
    },
  });
}

export function useMonitoringNetwork(enabled: boolean = true) {
  const scopeKey = useMonitoringScopeKey();
  return useQuery({
    queryKey: ["monitoring", "network", scopeKey],
    queryFn: () => api.monitoring.getNetworkMetrics(),
    enabled,
    refetchInterval: 10000,
    refetchIntervalInBackground: false,
    staleTime: 5000,
  });
}

// ========== Certificate Management Hooks ==========

export function useCertificates() {
  return useRefreshableQuery({
    queryKey: ["certificates"],
    queryFn: ({ fresh }) => api.certificates.list({ fresh }),
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
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ["certificates"] });
      queryClient.invalidateQueries({ queryKey: ["certificate", variables.cluster_id] });
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
    },
  });
}

export function useVerifyCertRenewal() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (request: { cluster_id: string; pending_cert_id?: string }) => 
      api.certificates.verifyRenewal(request),
    onSuccess: async (_, variables) => {
      await Promise.all([
        queryClient.refetchQueries({ queryKey: ["certificates"], type: 'active' }),
        queryClient.refetchQueries({ queryKey: ["clusters"], type: 'active' }),
        queryClient.refetchQueries({ queryKey: queryKeys.nodes, type: 'active' }),
      ]);
      queryClient.invalidateQueries({ queryKey: ["certificate", variables.cluster_id] });
    },
  });
}

export function useCancelPendingCert() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (clusterId: string) => api.certificates.cancelPending(clusterId),
    onSuccess: (_, clusterId) => {
      queryClient.invalidateQueries({ queryKey: ["certificates"] });
      queryClient.invalidateQueries({ queryKey: ["certificate", clusterId] });
    },
  });
}

export function useSystemWarnings() {
  // the same 403 as useWarnings: /warnings/system/all sits behind require_partner()
  const { isPartner } = usePermissions();
  return useRefreshableQuery({
    enabled: isPartner,
    queryKey: ["systemWarnings"],
    // note: every sibling destructures { fresh }; this one did not, so the second half of
    // "Refresh all" on the warnings page silently reused whatever was already in flight.
    queryFn: ({ fresh }) => api.certificates.getSystemWarnings({ fresh }),
    refetchInterval: 60000, // Refresh every minute
    staleTime: 30000,
  });
}

// Regions Hooks
export function useRegionsOverview() {
  return useRefreshableQuery({
    queryKey: ["regions", "overview"],
    queryFn: ({ fresh }) => api.regions.getOverview({ fresh }),
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
) {
  return useRefreshableQuery({
    queryKey: ["instanceSnapshots", instanceName, host, project],
    // note: `fresh` is deliberately NOT forwarded. It exists to bypass a backend response cache,
    // and the snapshot routes have none: they read the cluster on every call. Sending it anyway
    // spent the shared 12-per-minute cache-bypass budget on requests that had nothing to bypass,
    // so an operator watching a restore and clicking Refresh could exhaust the allowance for the
    // surfaces that really are cached. The refetch still happens; only the pointless parameter is
    // gone.
    queryFn: () => api.snapshots.list(instanceName, host, project),
    ...SEMI_STATIC_DATA_CONFIG,
    // After the spread on purpose: the preset carries its own `enabled` and would otherwise
    // re-enable a query the caller gated on the active tab.
    enabled: enabled && !!instanceName && !!host,
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

/** Every snapshot query, invalidated together: a mutation changes both the per-instance and fleet views. */
function invalidateSnapshots(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: ["instanceSnapshots"] });
  queryClient.invalidateQueries({ queryKey: ["fleetSnapshots"] });
}

// ── snapshot schedules ─────────────────────────────────────────────────────────────────────────

/** Every schedule the caller's organization can see, optionally narrowed to one instance. */
export function useSnapshotSchedules(
  filters: { host?: string; project?: string; instance?: string } = {},
  enabled = true,
) {
  return useRefreshableQuery({
    queryKey: [
      "snapshotSchedules",
      filters.host ?? null,
      filters.project ?? null,
      filters.instance ?? null,
    ],
    queryFn: (opts) => api.snapshotSchedules.list(filters, opts),
    ...SEMI_STATIC_DATA_CONFIG,
    // After the spread on purpose: the preset carries its own `enabled` and its own `retry`.
    enabled,
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

function invalidateSchedules(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: ["snapshotSchedules"] });
}

/**
 * important: every schedule mutation invalidates the SNAPSHOT queries too, not only the schedule
 * ones. Running a schedule creates a snapshot, and deleting one changes what the list beside it
 * means. Invalidating only its own key would leave the two halves of one screen disagreeing.
 */
function invalidateBoth(queryClient: ReturnType<typeof useQueryClient>) {
  invalidateSchedules(queryClient);
  invalidateSnapshots(queryClient);
}

export function useCreateSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: import("@/types/api").SnapshotScheduleCreateRequest) =>
      api.snapshotSchedules.create(body),
    onSuccess: () => invalidateBoth(queryClient),
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
    onSuccess: () => invalidateBoth(queryClient),
  });
}

export function usePauseSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.snapshotSchedules.pause(id),
    onSuccess: () => invalidateSchedules(queryClient),
  });
}

export function useResumeSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.snapshotSchedules.resume(id),
    onSuccess: () => invalidateSchedules(queryClient),
  });
}

export function useRunSnapshotScheduleNow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.snapshotSchedules.runNow(id),
    onSuccess: () => invalidateBoth(queryClient),
  });
}

export function useDeleteSnapshotSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.snapshotSchedules.remove(id),
    onSuccess: () => invalidateBoth(queryClient),
  });
}

export function useCreateSnapshot() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ instanceName, body }: { instanceName: string; body: SnapshotCreateRequest }) =>
      api.snapshots.create(instanceName, body),
    onSuccess: () => invalidateSnapshots(queryClient),
  });
}

export function useDeleteSnapshot() {
  const queryClient = useQueryClient();
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
    onSuccess: () => invalidateSnapshots(queryClient),
  });
}

export function useRestoreSnapshot() {
  const queryClient = useQueryClient();
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
    onSuccess: () => {
      invalidateSnapshots(queryClient);
      // A restore rolls the instance back, so its own row is stale too.
      queryClient.invalidateQueries({ queryKey: ["instances"] });
      // important: and the DETAIL query, which is keyed ["instance", ...] singular. React Query
      // matches by prefix, so invalidating ["instances"] does not touch it: the list behind the
      // dialog refreshed while the dialog the operator was looking at kept painting the state the
      // restore had just discarded.
      queryClient.invalidateQueries({ queryKey: ["instance"] });
      queryClient.invalidateQueries({ queryKey: ["instanceMetrics"] });
    },
  });
}

// Instance Mobility Hooks
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
  return useMutation({
    mutationFn: ({ instanceName, body }: { instanceName: string; body: MoveRequest }) =>
      api.mobility.copy(instanceName, body),
    onSuccess: () => {
      // A copy creates an instance on the destination, so the list is behind everywhere.
      queryClient.invalidateQueries({ queryKey: ["instances"] });
      queryClient.invalidateQueries({ queryKey: ["vms"] });
    },
  });
}
