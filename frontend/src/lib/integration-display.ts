import type { AvailableCluster, ClusterPermission } from "@/types";
import { formatHypervisorType, formatLabel } from "@/lib/utils";

export interface ClusterAccessDisplay {
  id: string;
  name: string;
  typeLabel: string;
  permissionLabel: string;
  projectsLabel: string;
  /** The registration this grant was given for has ended, so it reaches nothing. */
  paused: boolean;
}

export function formatClusterPermissionLabel(permissions: string[] = []): string {
  if (permissions.includes("admin")) return "Admin";
  if (permissions.includes("write")) return "Read & Write";
  if (permissions.includes("read")) return "Read Only";
  return "None";
}

const LEVEL_RANK: Record<string, number> = {
  None: 0,
  "Read Only": 1,
  "Read & Write": 2,
  Admin: 3,
};

// important: absent and empty are opposites. Every enforcement site reads
// `perm.get("projects", ["default"])` and denies unless the project or "*" is listed, so a
// missing key means the single project `default` while an empty list denies every
// project-scoped call. Showing "All projects" for the empty case would overstate what the
// credential can reach.
function formatProjectsLabel(projects: string[] | null | undefined): string {
  if (projects == null) return "Project: default";
  const projectNames = projects.filter(Boolean);
  if (projectNames.includes("*")) return "All projects";
  if (projectNames.length === 0) return "No projects";
  if (projectNames.length === 1) return `Project: ${projectNames[0]}`;
  return `${projectNames.length} projects`;
}

// The one access level a key carries, or its ceiling plus a spread marker when its clusters
// disagree.
//
// important: never a bare "Mixed". A key holding `read` on twelve clusters and `admin` on one
// would hide the admin grant from whoever audits the key, and on a credentials surface
// under-reporting is the unsafe direction. It reads the rendered rows rather than the raw
// permission arrays, so the header cannot disagree with the list beneath it.
export function summarizeClusterAccessLevel(clusters: ClusterAccessDisplay[]): string {
  if (clusters.length === 0) return "None";
  const levels = new Set(clusters.map((cluster) => cluster.permissionLabel));
  const ceiling = [...levels].reduce((best, level) =>
    (LEVEL_RANK[level] ?? 0) > (LEVEL_RANK[best] ?? 0) ? level : best
  );
  return levels.size === 1 ? ceiling : `${ceiling}, mixed`;
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
      // No `|| []` here: it would turn absent into empty before the formatter can tell them
      // apart.
      projectsLabel: formatProjectsLabel(permission.projects),
      paused: permission.registered === false,
    };
  });
}

// Operator-facing names for the application types the API serves. formatLabel would render
// "mcp" as "Mcp", so the known ones are spelled out; any other type falls back to formatLabel.
const APP_TYPE_LABELS: Record<string, string> = {
  orcahub: "Orcahub",
  mcp: "MCP Server",
  automation: "Automation / CI",
  agent: "Agent",
  custom: "Custom Application",
};

export function formatApplicationType(appType: string | null | undefined): string {
  if (!appType) return "Unknown";
  return APP_TYPE_LABELS[appType] ?? formatLabel(appType);
}

// important: null and empty are opposites, as they are for `projects` above. A key without a
// `capabilities` field predates capabilities and keeps unrestricted access to the external
// router; showing it as "no capabilities" would understate what the credential can do.
export function isLegacyUnscopedKey(capabilities: string[] | null | undefined): boolean {
  return capabilities == null;
}

/**
 * What a key's status badge says, as a value.
 *
 * The badge and the status facet both read this, so a filter can never rule out a key whose
 * chip says otherwise.
 */
export type APIKeyStatusLabel = "Active" | "Expired" | "Revoked" | "Inactive";

export function getAPIKeyStatus(key: {
  is_revoked?: boolean;
  is_active?: boolean;
  expires_at?: string | null;
  expiry_state?: "never" | "active" | "expiring" | "expired" | null;
}): APIKeyStatusLabel {
  if (key.is_revoked) return "Revoked";
  const expiryState = key.expiry_state ?? (key.expires_at ? "active" : "never");
  if (expiryState === "expired") return "Expired";
  return key.is_active ? "Active" : "Inactive";
}

/** Fixed order for the status facet: working first, then the two ways a key stops working. */
export const API_KEY_STATUS_ORDER: readonly APIKeyStatusLabel[] = [
  "Active",
  "Inactive",
  "Expired",
  "Revoked",
];
