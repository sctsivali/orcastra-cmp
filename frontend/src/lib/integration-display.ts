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

// important: absent and empty are opposites here, and one line used to print the same words for
// both. Every enforcement site reads `perm.get("projects", ["default"])` and then denies unless
// the project is listed or "*" is, so a missing key means the single project named `default`
// while an empty list matches nothing and denies every project-scoped call. Printing
// "All projects" for the empty case was wrong in the permissive direction on a surface whose
// whole job is to say what a credential can reach.
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
// important: not a bare "Mixed". A key holding `read` on twelve clusters and `admin` on one
// would then advertise "Mixed" and hide the admin grant from whoever is auditing the key. On a
// credentials surface under-reporting is the unsafe direction, so the ceiling stays visible and
// the marker says the rows below it are not uniform. It replaces a flatten-and-take-the-max that
// dropped the spread entirely.
//
// note: it reads the rendered rows rather than the raw permission arrays, so the header cannot
// disagree with the list two inches beneath it.
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
      // note: no `|| []` here. That collapsed absent into empty before the formatter could tell
      // them apart, so fixing only the formatter would have left the second half of the bug.
      projectsLabel: formatProjectsLabel(permission.projects),
      paused: permission.registered === false,
    };
  });
}

// The application type identifiers the API serves, in operator language. formatLabel would
// render "mcp" as "Mcp", so the known ones are spelled out here and anything the API adds later
// falls back to a readable form rather than showing a raw identifier.
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

// important: null and empty are opposites, exactly as they are for `projects` above. A key whose
// `capabilities` field is absent predates capabilities and retains unrestricted access to the
// external router, so rendering it as "no capabilities" would understate a credential on the one
// surface that exists to say what a credential can do.
export function isLegacyUnscopedKey(capabilities: string[] | null | undefined): boolean {
  return capabilities == null;
}

/**
 * What a key's status badge says, as a value.
 *
 * important: the branch order mirrors the badge exactly, and both read this rather than each
 * deciding for themselves. A status facet that disagreed with the chip beside it would send an
 * operator looking for a key the filter had already ruled out.
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
