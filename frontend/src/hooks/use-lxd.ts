/**
 * Custom React Query hooks for Orcastra Dashboard
 */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { vmAPI, imageAPI, nodeAPI, projectAPI, tagAPI, healthCheck } from "@/lib/api";
import { VMCreateRequest, VMActionRequest, ProjectCreateRequest } from "@/types";

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
    queryFn: () => vmAPI.list(host, status),
    refetchInterval: 300000, // Auto-refresh every 5 minutes (300 seconds)
    staleTime: 30000, // Consider data stale after 30 seconds
    ...defaultRetryConfig,
    refetchOnWindowFocus: false, // Prevent excessive refetching when switching tabs
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
export function useImages() {
  return useQuery({
    queryKey: queryKeys.images,
    queryFn: () => imageAPI.listAll(),
    staleTime: 60000, // Cache for 1 minute
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

// Projects Hooks
export function useAllProjects() {
  return useQuery({
    queryKey: queryKeys.allProjects,
    queryFn: () => projectAPI.listAll(),
    refetchInterval: 300000, // refresh every 5 minutes (300 seconds)
    staleTime: 30000, // Consider data stale after 30 seconds
    retry: 3, // Add retry for connection issues
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
