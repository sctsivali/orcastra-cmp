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

/**
 * A stored key timestamp in epoch milliseconds, NaN when it cannot be read.
 *
 * note: a value without an offset is UTC, as the backend writes it. Parsed bare, the browser reads
 * it as local time and the date lands a day out either side of midnight.
 */
export function parseKeyTimestamp(value: string): number {
  return Date.parse(/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`);
}

// How close to its expiry an active key reads as expiring: EXPIRY_WARNING_DAYS in
// backend/app/core/api_key_expiry.py, the window in which its organizations can still be moved.
const EXPIRY_WARNING_DAYS = 14;
const DAY_MS = 24 * 60 * 60 * 1000;

export type APIKeyExpiryState = "never" | "active" | "expiring" | "expired";

/**
 * How a key's expiry stands, as backend/app/core/api_key_expiry.py expiry_state judges it.
 *
 * important: this line's key list carries no expiry_state, so the card computes it. An expiry that
 * cannot be read counts as expired, as authentication counts it.
 */
export function getAPIKeyExpiryState(expiresAt: string | null | undefined, now: number = Date.now()): APIKeyExpiryState {
  if (!expiresAt) return "never";
  const expires = parseKeyTimestamp(expiresAt);
  if (Number.isNaN(expires) || expires < now) return "expired";
  return Math.floor((expires - now) / DAY_MS) <= EXPIRY_WARNING_DAYS ? "expiring" : "active";
}

/**
 * How a key stands as a credential, in one word.
 *
 * note: the order is the backend's classify_key precedence. A revocation outranks a date, and an
 * expiry outranks is_active, which nothing sets false when a key lapses.
 */
export type APIKeyStatusLabel = "Active" | "Expired" | "Revoked" | "Inactive";

export function getAPIKeyStatus(key: {
  is_revoked?: boolean;
  is_active?: boolean;
  expires_at?: string | null;
}): APIKeyStatusLabel {
  if (key.is_revoked) return "Revoked";
  if (getAPIKeyExpiryState(key.expires_at) === "expired") return "Expired";
  return key.is_active ? "Active" : "Inactive";
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
    };
  });
}
