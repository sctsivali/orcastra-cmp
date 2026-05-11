/**
 * Custom React Query hooks for Orcastra Dashboard
 */

import { useQuery, useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { useSession } from "next-auth/react";
import { 
  vmAPI, 
  imageAPI, 
  nodeAPI, 
  projectAPI, 
  tagAPI, 
  healthCheck,
  api 
} from "@/lib/api";
import { VMCreateRequest, VMActionRequest, ProjectCreateRequest, VMListResponse } from "@/types";
import { STATIC_DATA_CONFIG, SEMI_STATIC_DATA_CONFIG, DYNAMIC_DATA_CONFIG } from "@/lib/query-config";

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
  return useQuery({
    queryKey: host ? queryKeys.imagesByHost(host) : queryKeys.images,
    queryFn: async () => {
      if (host) {
        const result = await imageAPI.listByHost(host);
        return [result]; // Normalize to array
      }
      return imageAPI.listAll();
    },
    // Always enabled - fetches all images when host is undefined, or filtered when host is provided
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

// Nodes Hooks
export function useNodesStatus() {
  return useQuery({
    queryKey: queryKeys.nodes,
    queryFn: () => nodeAPI.status(),
    refetchInterval: 3600000, // Check node status every 1 hour (3600 seconds)
    staleTime: 60000, // Consider data stale after 1 minute
    retry: 3,
    refetchOnWindowFocus: false,
    refetchOnMount: 'always', // Always refetch on mount to ensure fresh data after registration
  });
}

export function useNodeNames() {
  return useQuery({
    queryKey: queryKeys.nodeNames,
    queryFn: () => nodeAPI.names(),
    staleTime: Infinity, // node names rarely change, cache indefinitely
    gcTime: 1000 * 60 * 60 * 24, // keep in cache for 24 hours
    refetchOnMount: false,
    refetchOnWindowFocus: false,
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

export function useNodeResources(nodeName: string) {
  return useQuery({
    queryKey: ["nodeResources", nodeName],
    queryFn: () => nodeAPI.resources(nodeName),
    enabled: !!nodeName,
    ...STATIC_DATA_CONFIG,
  });
}

// Projects Hooks
export function useAllProjects() {
  return useQuery({
    queryKey: queryKeys.allProjects,
    queryFn: () => projectAPI.listAll(),
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
export function useWarnings(host?: string) {
  return useQuery({
    queryKey: ["warnings", host],
    queryFn: () => api.warnings.list(host),
    ...DYNAMIC_DATA_CONFIG,
    refetchInterval: 60000, // Check warnings every minute
  });
}

// Operations Hooks
export function useOperations(host?: string) {
  return useQuery({
    queryKey: ["operations", host],
    queryFn: () => api.operations.list(host),
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
  return useQuery({
    queryKey: ["ipam", host],
    queryFn: () => api.ipam.list(host),
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

// Storage Pools Hooks
export function useStoragePools(host?: string) {
  return useQuery({
    queryKey: ["storagePools", host],
    queryFn: async () => {
      const [poolsData, resourcesData] = await Promise.all([
        api.storagePools.list(host) as Promise<Record<string, unknown>>,
        api.storagePools.getResourcesAll() as Promise<Record<string, unknown> | null>,
      ]);
      return {
        ...poolsData,
        resources: (resourcesData as Record<string, unknown>)?.resources || {},
      };
    },
    // Always enabled - fetches all pools when host is undefined
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
      queryClient.invalidateQueries({ queryKey: ["storagePools"] });
    },
  });
}

export function useDeleteStoragePool() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.storagePools.delete(name, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["storagePools"] });
    },
  });
}

// Storage Volumes Hooks
export function useStorageVolumes(pool?: string, host?: string) {
  return useQuery({
    queryKey: ["storageVolumes", pool, host],
    queryFn: () => api.storageVolumes.list(host, pool),
    // Always enabled - fetches all volumes when pool/host is undefined
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useStorageVolume(pool: string, type: string, name: string, host: string) {
  return useQuery({
    queryKey: ["storageVolume", pool, type, name, host],
    queryFn: () => api.storageVolumes.get(pool, type, name, host),
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
    mutationFn: ({ pool, type, name, host }: { pool: string; type: string; name: string; host: string }) => 
      api.storageVolumes.delete(pool, type, name, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["storageVolumes"] });
    },
  });
}

// Networks Hooks
export function useNetworks(host?: string) {
  return useQuery({
    queryKey: ["networks", host],
    queryFn: () => api.networks.list(host),
    // Always enabled - fetches all networks when host is undefined
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useNetwork(name: string, host: string) {
  return useQuery({
    queryKey: ["network", name, host],
    queryFn: () => api.networks.get(name, host),
    enabled: !!name && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useNetworkState(name: string, host: string) {
  return useQuery({
    queryKey: ["networkState", name, host],
    queryFn: () => api.networks.getState(name, host),
    enabled: !!name && !!host,
    ...DYNAMIC_DATA_CONFIG,
  });
}

export function useCreateNetwork() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host }: { data: any; host: string }) => api.networks.create(data, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["networks"] });
    },
  });
}

export function useDeleteNetwork() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.networks.delete(name, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["networks"] });
    },
  });
}

// Network ACLs Hooks
export function useNetworkACLs(host?: string) {
  return useQuery({
    queryKey: ["networkACLs", host],
    queryFn: () => api.networkACLs.list(host),
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useNetworkACL(name: string, host: string) {
  return useQuery({
    queryKey: ["networkACL", name, host],
    queryFn: () => api.networkACLs.get(name, host),
    enabled: !!name && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useCreateNetworkACL() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host }: { data: any; host: string }) => api.networkACLs.create(data, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["networkACLs"] });
    },
  });
}

export function useDeleteNetworkACL() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.networkACLs.delete(name, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["networkACLs"] });
    },
  });
}

// Profiles Hooks
export function useProfiles(host?: string) {
  return useQuery({
    queryKey: ["profiles", host],
    queryFn: () => api.profiles.list(host),
    // Always enabled - fetches all profiles when host is undefined
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useProfile(name: string, host: string) {
  return useQuery({
    queryKey: ["profile", name, host],
    queryFn: () => api.profiles.get(name, host),
    enabled: !!name && !!host,
    ...SEMI_STATIC_DATA_CONFIG,
  });
}

export function useCreateProfile() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ data, host }: { data: any; host: string }) => api.profiles.create(data, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["profiles"] });
    },
  });
}

export function useDeleteProfile() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.profiles.delete(name, host),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["profiles"] });
    },
  });
}

// Instances Hooks (alternative to VMs for LXD terminology)
export function useInstances(host?: string) {
  return useQuery({
    queryKey: ["instances", host],
    queryFn: () => api.instances.list(host),
    // Always enabled - fetches all instances when host is undefined
    ...DYNAMIC_DATA_CONFIG,
  });
}

export function useInstance(name: string, host: string) {
  return useQuery({
    queryKey: ["instance", name, host],
    queryFn: () => api.instances.get(name, host),
    enabled: !!name && !!host,
    ...DYNAMIC_DATA_CONFIG,
  });
}

// Instance Action Mutations (using vmAPI)
export function useStartInstance() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.vms.start(name, { host }),
    onSuccess: () => {
      refreshInstanceQueries(queryClient);
    },
  });
}

export function useStopInstance() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.vms.stop(name, { host }),
    onSuccess: () => {
      refreshInstanceQueries(queryClient);
    },
  });
}

export function useRestartInstance() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.vms.restart(name, { host }),
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
  return useQuery({
    queryKey: ["serverInfo", host],
    queryFn: () => api.server.list(host),
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
  return useQuery({
    queryKey: ["identities", host],
    queryFn: () => api.identities.list(host),
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
  return useQuery({
    queryKey: ["groups", host],
    queryFn: () => api.groups.list(host),
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
  return useQuery({
    queryKey: ["clusterMembers", node],
    queryFn: () => api.clusterMembers.list(node),
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

export function useMemberResources(node: string, memberName: string) {
  return useQuery({
    queryKey: ["memberResources", node, memberName],
    queryFn: () => api.clusterMembers.getResources(node, memberName),
    enabled: !!node && !!memberName,
    staleTime: 30 * 1000, // 30 seconds
    gcTime: 5 * 60 * 1000, // 5 minutes
  });
}

// Clusters Hooks
export function useClusters(fresh: boolean = false) {
  return useQuery({
    queryKey: ["clusters", { fresh }],
    queryFn: () => api.clusters.list(fresh),
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
      queryClient.invalidateQueries({ queryKey: queryKeys.vms });
      queryClient.invalidateQueries({ queryKey: queryKeys.allProjects });
      queryClient.invalidateQueries({ queryKey: ["instances"] });
      queryClient.invalidateQueries({ queryKey: ["facilities"] });
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
        queryClient.refetchQueries({ queryKey: ["groups"], type: 'active' }),
        queryClient.refetchQueries({ queryKey: ["identities"], type: 'active' }),
      ]);
      // Also invalidate to ensure any inactive queries will refetch when they become active
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
      queryClient.invalidateQueries({ queryKey: queryKeys.nodes });
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
  return useQuery({
    queryKey: ["monitoring", "dashboard", scopeKey],
    queryFn: () => api.monitoring.getDashboard(),
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
  return useQuery({
    queryKey: ["certificates"],
    queryFn: () => api.certificates.list(),
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
  return useQuery({
    queryKey: ["systemWarnings"],
    queryFn: () => api.certificates.getSystemWarnings(),
    refetchInterval: 60000, // Refresh every minute
    staleTime: 30000,
  });
}

// Regions Hooks
export function useRegionsOverview() {
  return useQuery({
    queryKey: ["regions", "overview"],
    queryFn: () => api.regions.getOverview(),
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
