"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { usePermissions } from "@/hooks/use-permissions";
import { formatDateTime } from "@/lib/utils";
import type { MyAccessResponse, TenantPermission, ResolvedAccess } from "@/types";

/**
 * Tenant-level access checks for UI gating.
 *
 * Admin and Partner bypass every ACL check. A Tenant reads its resolved policy rules from the
 * backend, and the helpers match cluster, project and instance paths with `*` wildcards.
 */

export interface UseTenantAccessResult {
  /** True while the access data is being fetched. */
  isLoading: boolean;
  /** The resolved access rules. */
  access: ResolvedAccess[];
  /** Clusters the tenant can see. */
  totalClusters: number;
  /** Rules resolved for the tenant. */
  totalRules: number;
  /** Whether the tenant holds any permission on a cluster. */
  canAccessCluster: (clusterId: string) => boolean;
  /** Whether the tenant holds any rule for a project within a cluster. */
  canAccessProject: (clusterId: string, projectName: string) => boolean;
  /** Whether the tenant can see an instance. */
  canAccessInstance: (clusterId: string, projectName: string, instanceName: string) => boolean;
  /** Checks one permission for a resource path. */
  hasPermission: (
    clusterId: string,
    projectName: string,
    instanceName: string,
    permission: TenantPermission,
  ) => boolean;
  /** Cluster IDs the tenant can access. */
  allowedClusterIds: string[];
  /** Keeps the rows whose cluster_id the tenant can access. */
  filterClusters: <T extends { cluster_id: string }>(clusters: T[]) => T[];
  /** Keeps the project names the tenant can access on a cluster. */
  filterProjects: (clusterId: string, projectNames: string[]) => string[];
  /**
   * Why an instance is view only because its paid term ended, or undefined. A rule from another
   * policy that still grants `operate` on it wins: then it is not read-only.
   */
  termReadOnlyReason: (clusterId: string, projectName: string, instanceName: string) => string | undefined;
}

function covers(rule: ResolvedAccess, clusterId: string, projectName: string, instanceName: string): boolean {
  return (
    rule.cluster_id === clusterId &&
    (rule.project_name === projectName || rule.project_name === "*") &&
    (rule.instance_name === instanceName || rule.instance_name === "*")
  );
}

export function useTenantAccess(): UseTenantAccessResult {
  const { isPartner, isLoading: permLoading } = usePermissions();

  // Only a Tenant fetches; Admin and Partner bypass the ACL.
  const {
    data,
    isLoading: queryLoading,
  } = useQuery({
    queryKey: ["tenant-my-access"],
    queryFn: () => api.tenantAccess.myAccess(),
    enabled: !isPartner && !permLoading,
    staleTime: 60_000,
  });

  const access = data?.access || [];
  const totalClusters = data?.total_clusters || 0;
  const totalRules = data?.total_rules || 0;
  const isLoading = permLoading || (!isPartner && queryLoading);

  // Lookup map for the ACL checks.
  const aclMap = useMemo(() => {
    if (isPartner) return null; // Admin and Partner bypass
    const map = new Map<string, Set<TenantPermission>>();
    for (const rule of access) {
      // Key format: clusterId/project/instance.
      const key = `${rule.cluster_id}/${rule.project_name}/${rule.instance_name}`;
      const existing = map.get(key) || new Set<TenantPermission>();
      for (const p of rule.permissions) {
        existing.add(p as TenantPermission);
      }
      map.set(key, existing);
    }
    return map;
  }, [access, isPartner]);

  const allowedClusterIds = useMemo(() => {
    if (isPartner) return []; // Not used for Admin or Partner
    const ids = new Set(access.map((r) => r.cluster_id));
    return Array.from(ids);
  }, [access, isPartner]);

  // Wildcard-aware permission check.
  const hasPermission = useMemo(() => {
    return (
      clusterId: string,
      projectName: string,
      instanceName: string,
      permission: TenantPermission,
    ): boolean => {
      // Admin and Partner always have access.
      if (isPartner) return true;
      if (!aclMap) return false;

      // Most specific path first.
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
      // Any rule for this cluster and project, or for every project on it.
      for (const [key] of aclMap) {
        const [c, p] = key.split("/");
        if ((c === clusterId) && (p === projectName || p === "*")) return true;
      }
      return false;
    };
  }, [isPartner, aclMap]);

  const canAccessInstance = useMemo(() => {
    return (clusterId: string, projectName: string, instanceName: string): boolean => {
      // Seeing an instance means holding `view` on it.
      return hasPermission(clusterId, projectName, instanceName, "view");
    };
  }, [hasPermission]);

  const filterClusters = useMemo(() => {
    return <T extends { cluster_id: string }>(clusters: T[]): T[] => {
      if (isPartner) return clusters;
      return clusters.filter((c) => allowedClusterIds.includes(c.cluster_id));
    };
  }, [isPartner, allowedClusterIds]);

  const termReadOnlyReason = useMemo(() => {
    return (clusterId: string, projectName: string, instanceName: string): string | undefined => {
      if (isPartner) return undefined;
      const rules = access.filter((rule) => covers(rule, clusterId, projectName, instanceName));
      if (rules.some((rule) => rule.permissions.includes("operate"))) return undefined;
      const ended = rules.find((rule) => rule.read_only);
      if (!ended) return undefined;
      return ended.access_ends_at
        ? `Subscription ended: read-only until ${formatDateTime(ended.access_ends_at)}`
        : "Subscription ended: read-only";
    };
  }, [access, isPartner]);

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
    termReadOnlyReason,
    hasPermission,
    allowedClusterIds,
    filterClusters,
    filterProjects,
  };
}
