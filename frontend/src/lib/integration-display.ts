import type { AvailableCluster, ClusterPermission } from "@/types";
import { formatHypervisorType } from "@/lib/utils";

export interface ClusterAccessDisplay {
  id: string;
  name: string;
  typeLabel: string;
  permissionLabel: string;
  projectsLabel: string;
}

export function formatClusterPermissionLabel(permissions: string[] = []): string {
  if (permissions.includes("admin")) return "Admin";
  if (permissions.includes("write")) return "Read & Write";
  if (permissions.includes("read")) return "Read Only";
  return "None";
}

function formatProjectsLabel(projects: string[] = []): string {
  const projectNames = projects.filter(Boolean);
  if (projectNames.length === 0 || projectNames.includes("*")) return "All projects";
  if (projectNames.length === 1) return `Project: ${projectNames[0]}`;
  return `${projectNames.length} projects`;
}

export function getClusterAccessDisplay(
  clusterPermissions: ClusterPermission[] | undefined,
  availableClusters: AvailableCluster[] = [],
): ClusterAccessDisplay[] {
  const clustersById = new Map(availableClusters.map((cluster) => [cluster.id, cluster]));

  return (clusterPermissions || []).map((permission) => {
    const cluster = clustersById.get(permission.cluster_id);

    return {
      id: permission.cluster_id,
      name: cluster?.name || permission.cluster_id,
      typeLabel: cluster?.server_type ? formatHypervisorType(cluster.server_type) : "Cluster",
      permissionLabel: formatClusterPermissionLabel(permission.permissions || []),
      projectsLabel: formatProjectsLabel(permission.projects || []),
    };
  });
}