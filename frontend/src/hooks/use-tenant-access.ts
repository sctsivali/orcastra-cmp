"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { usePermissions } from "@/hooks/use-permissions";
import type { MyAccessResponse, TenantPermission, ResolvedAccess } from "@/types";

/**
 * Hook for tenant-level access control.
 *
 * - Admin/Partner: bypass all ACL checks (full access).
 * - Tenant: fetches resolved policy rules from the backend and provides
 *   helpers to check cluster/project/instance permissions.
 *
 * Usage:
 *   const { canAccessCluster, canAccessProject, hasPermission } = useTenantAccess();
 *   if (!canAccessCluster("my-cluster")) return <NoAccess />;
 *   if (!hasPermission("my-cluster", "default", "vm1", "console")) return null;
 */

export interface UseTenantAccessResult {
  /** true while the access data is being fetched */
  isLoading: boolean;
  /** raw resolved access data */
  access: ResolvedAccess[];
  /** total clusters the tenant can see */
  totalClusters: number;
  /** total rules resolved for the tenant */
  totalRules: number;
  /** check if user can access a given cluster (any permission) */
  canAccessCluster: (clusterId: string) => boolean;
  /** check if user can access a given project within a cluster */
  canAccessProject: (clusterId: string, projectName: string) => boolean;
  /** check if user can access a given instance */
  canAccessInstance: (clusterId: string, projectName: string, instanceName: string) => boolean;
  /** check a specific permission for a resource path */
  hasPermission: (
    clusterId: string,
    projectName: string,
    instanceName: string,
    permission: TenantPermission,
  ) => boolean;
  /** get all allowed cluster IDs */
  allowedClusterIds: string[];
  /** filter a list of cluster IDs to only those allowed */
  filterClusters: <T extends { cluster_id: string }>(clusters: T[]) => T[];
  /** filter projects for a cluster */
  filterProjects: (clusterId: string, projectNames: string[]) => string[];
}

export function useTenantAccess(): UseTenantAccessResult {
  const { isPartner, isLoading: permLoading } = usePermissions();

  // only fetch for tenant users (non-partner, non-admin)
  const {
    data,
    isLoading: queryLoading,
  } = useQuery({
    queryKey: ["tenant-my-access"],
    queryFn: () => api.tenantAccess.myAccess(),
    enabled: !isPartner && !permLoading,
    staleTime: 60_000, // cache for 1 min
  });

  const access = data?.access || [];
  const totalClusters = data?.total_clusters || 0;
  const totalRules = data?.total_rules || 0;
  const isLoading = permLoading || (!isPartner && queryLoading);

  // build lookup maps for fast ACL checks
  const aclMap = useMemo(() => {
    if (isPartner) return null; // bypass for admin/partner
    const map = new Map<string, Set<TenantPermission>>();
    for (const rule of access) {
      // key format: clusterId/project/instance
      const key = `${rule.cluster_id}/${rule.project_name}/${rule.instance_name}`;
      const existing = map.get(key) || new Set<TenantPermission>();
      for (const p of rule.permissions) {
        existing.add(p as TenantPermission);
      }
      map.set(key, existing);
    }
    return map;
  }, [access, isPartner]);

  // unique cluster IDs the tenant can access
  const allowedClusterIds = useMemo(() => {
    if (isPartner) return []; // not used for admin/partner
    const ids = new Set(access.map((r) => r.cluster_id));
    return Array.from(ids);
  }, [access, isPartner]);

  // wildcard-aware permission check
  const hasPermission = useMemo(() => {
    return (
      clusterId: string,
      projectName: string,
      instanceName: string,
      permission: TenantPermission,
    ): boolean => {
      // admin/partner always has access
      if (isPartner) return true;
      if (!aclMap) return false;

      // check from most specific to most general
      const paths = [
        `${clusterId}/${projectName}/${instanceName}`,
        `${clusterId}/${projectName}/*`,
        `${clusterId}/*/${instanceName}`,
        `${clusterId}/*/*`,
      ];

      for (const path of paths) {
        const perms = aclMap.get(path);
        if (perms?.has(permission)) return true;
      }
      return false;
    };
  }, [isPartner, aclMap]);

  const canAccessCluster = useMemo(() => {
    return (clusterId: string): boolean => {
      if (isPartner) return true;
      return allowedClusterIds.includes(clusterId);
    };
  }, [isPartner, allowedClusterIds]);

  const canAccessProject = useMemo(() => {
    return (clusterId: string, projectName: string): boolean => {
      if (isPartner) return true;
      if (!aclMap) return false;
      // check if any rule matches this cluster+project
      for (const [key] of aclMap) {
        const [c, p] = key.split("/");
        if ((c === clusterId) && (p === projectName || p === "*")) return true;
      }
      return false;
    };
  }, [isPartner, aclMap]);

  const canAccessInstance = useMemo(() => {
    return (clusterId: string, projectName: string, instanceName: string): boolean => {
      // any permission = can see the instance
      return hasPermission(clusterId, projectName, instanceName, "view");
    };
  }, [hasPermission]);

  const filterClusters = useMemo(() => {
    return <T extends { cluster_id: string }>(clusters: T[]): T[] => {
      if (isPartner) return clusters;
      return clusters.filter((c) => allowedClusterIds.includes(c.cluster_id));
    };
  }, [isPartner, allowedClusterIds]);

  const filterProjects = useMemo(() => {
    return (clusterId: string, projectNames: string[]): string[] => {
      if (isPartner) return projectNames;
      return projectNames.filter((p) => canAccessProject(clusterId, p));
    };
  }, [isPartner, canAccessProject]);

  return {
    isLoading,
    access,
    totalClusters,
    totalRules,
    canAccessCluster,
    canAccessProject,
    canAccessInstance,
    hasPermission,
    allowedClusterIds,
    filterClusters,
    filterProjects,
  };
}
