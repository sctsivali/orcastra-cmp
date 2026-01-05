/**
 * Custom React Query hooks for Orcastra Dashboard
 */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { 
  vmAPI, 
  imageAPI, 
  nodeAPI, 
  projectAPI, 
  tagAPI, 
  healthCheck,
  api 
} from "@/lib/api";
import { VMCreateRequest, VMActionRequest, ProjectCreateRequest } from "@/types";
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

// Default retry configuration with exponential backoff
const defaultRetryConfig = {
  retry: 3,
  retryDelay: (attemptIndex: number) => Math.min(1000 * 2 ** attemptIndex, 30000),
};

// VMs Hooks
export function useVMs(host?: string, status?: string) {
  return useQuery({
    queryKey: [...queryKeys.vms, { host, status }],
    queryFn: async () => {
      try {
        return await vmAPI.list(host, status);
      } catch (error) {
        console.error("Failed to fetch VMs:", error);
        // Return empty response instead of throwing to prevent UI breaking
        return { total: 0, vms: [], nodes: [], lxd_count: 0, microcloud_count: 0 };
      }
    },
    refetchInterval: 300000, // Auto-refresh every 5 minutes
    ...STATIC_DATA_CONFIG, // Use static config for better initial load performance
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
      queryClient.invalidateQueries({ queryKey: queryKeys.vms });
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
      queryClient.invalidateQueries({ queryKey: queryKeys.vms });
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
      queryClient.invalidateQueries({ queryKey: queryKeys.vms });
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
      queryClient.invalidateQueries({ queryKey: queryKeys.vms });
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
    }: {
      name: string;
      host: string;
      force?: boolean;
    }) => vmAPI.delete(name, host, force),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.vms });
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
    refetchInterval: 5000, // Poll every 5 seconds (reduced from 3s to reduce load)
    staleTime: 0, // Always fetch fresh data for real-time metrics
    retry: 2, // Add retry for transient failures
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
    queryFn: async () => {
      try {
        return await projectAPI.listAll();
      } catch (error) {
        console.error("Failed to fetch all projects:", error);
        // Return empty array instead of throwing to prevent UI breaking
        return [];
      }
    },
    refetchInterval: 300000, // refresh every 5 minutes (300 seconds)
    staleTime: 30000, // Consider data stale after 30 seconds
    retry: 2, // Reduce retries to avoid long wait times
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
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projects(variables.host) });
      queryClient.invalidateQueries({ queryKey: queryKeys.allProjects });
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

// Health Hook
export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: healthCheck,
    refetchInterval: 30000,
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
    refetchInterval: 5000, // Operations change frequently, poll every 5s
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
    queryFn: () => api.storagePools.list(host),
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
      queryClient.invalidateQueries({ queryKey: ["instances"] });
    },
  });
}

export function useStopInstance() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.vms.stop(name, { host }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["instances"] });
    },
  });
}

export function useRestartInstance() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host }: { name: string; host: string }) => 
      api.vms.restart(name, { host }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["instances"] });
    },
  });
}

export function useDeleteInstance() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: ({ name, host, force }: { name: string; host: string; force?: boolean }) => 
      api.vms.delete(name, host, force),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["instances"] });
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
export function useClusters() {
  return useQuery({
    queryKey: ["clusters"],
    queryFn: () => api.clusters.list(),
    ...SEMI_STATIC_DATA_CONFIG,
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
    },
  });
}

export function useCheckClusterAuth() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (clusterId: string) => api.clusters.checkAuth(clusterId),
    onSuccess: async () => {
      // Use refetchQueries to ensure data is actually refreshed before redirect
      // This is more reliable than invalidateQueries which only marks as stale
      await Promise.all([
        queryClient.refetchQueries({ queryKey: ["clusters"], type: 'active' }),
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
      queryClient.invalidateQueries({ queryKey: queryKeys.nodes });
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

// ========== Monitoring Hooks ==========

export function useMonitoringDashboard(enabled: boolean = true, refetchInterval: number = 5000) {
  return useQuery({
    queryKey: ["monitoring", "dashboard"],
    queryFn: () => api.monitoring.getDashboard(),
    enabled,
    refetchInterval, // Default: poll every 5 seconds for real-time updates
    staleTime: 0, // Always consider data stale for real-time updates
    gcTime: 60 * 1000, // Keep in cache for 60 seconds
    refetchOnWindowFocus: true,
    refetchIntervalInBackground: true, // Keep refetching even when tab is not focused
    retry: 3,
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 10000),
  });
}

export function useMonitoringNodeMetrics(nodeName: string, enabled: boolean = true) {
  return useQuery({
    queryKey: ["monitoring", "node", nodeName],
    queryFn: () => api.monitoring.getNodeMetrics(nodeName),
    enabled: enabled && !!nodeName,
    refetchInterval: 5000,
    staleTime: 0,
  });
}

export function useMonitoringHistory(points: number = 60, enabled: boolean = true) {
  return useQuery({
    queryKey: ["monitoring", "history", points],
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
    refetchInterval: 10000, // Update history every 10 seconds
    staleTime: 5000, // Consider data fresh for 5 seconds
    gcTime: 60000, // Keep unused data for 1 minute (helps with time range switching)
    refetchIntervalInBackground: true, // Keep refetching even when tab is not focused
    retry: 3, // Retry up to 3 times on failure
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 10000), // Exponential backoff
    placeholderData: (previousData) => previousData, // Keep previous data while fetching new range
  });
}

export function useMonitoringNetwork(enabled: boolean = true) {
  return useQuery({
    queryKey: ["monitoring", "network"],
    queryFn: () => api.monitoring.getNetworkMetrics(),
    enabled,
    refetchInterval: 5000,
    staleTime: 0,
  });
}
