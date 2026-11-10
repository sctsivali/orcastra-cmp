import { Box, FolderKanban, Server, type LucideIcon } from "lucide-react";
import type { PermissionFlags, RoleGate } from "@/components/sidebar/types";

// The global create menu in the header. Gates are data, the same shape the sidebar uses in
// nav-config.tsx, so the renderer never sees role logic: it receives an already-filtered list.

/** Sidebar IA section the action belongs to, so ordering stays anchored to NAV_GROUPS. */
export type CreateSection = "compute" | "networking" | "cluster";

export interface CreateAction {
  /** Stable key: the React key, and the anchor browser tests match on. */
  id: string;
  label: string;
  icon: LucideIcon;
  href: string;
  section: CreateSection;
  /** Minimum role, same vocabulary as the sidebar. */
  role: RoleGate;
  /** Draw a separator above this item when something precedes it. */
  startsGroup?: boolean;
}

// Ordered as NAV_GROUPS is: Compute, then Networking, then Cluster, with the icons of the
// matching sidebar entries. A create that lives in a dialog on its own list page (networks,
// network ACLs, ISO and image uploads) is not offered: the menu lists destinations, links an
// operator can open in a new tab.
export const CREATE_ACTIONS: readonly CreateAction[] = [
  {
    id: "instance",
    label: "Create Instance",
    icon: Box,
    href: "/inventory/instances/create",
    section: "compute",
    role: "partner",
  },
  {
    id: "project",
    label: "Create Project",
    icon: FolderKanban,
    href: "/create",
    section: "compute",
    role: "partner",
  },
  {
    id: "node",
    label: "Register Cluster",
    icon: Server,
    href: "/register-node",
    section: "cluster",
    role: "partner",
    // Infrastructure rather than workload, so it sits below the boundary.
    startsGroup: true,
  },
];

function roleAllowed(role: RoleGate, perms: PermissionFlags): boolean {
  if (role === "admin") return perms.isAdmin;
  if (role === "partner") return perms.isPartner;
  return perms.isTenant;
}

/**
 * The actions to offer right now, without the one for the route the operator is already on.
 *
 * usePathname() returns the pathname alone, with no query or hash, so a plain comparison is
 * sound for these static destinations.
 */
export function visibleCreateActions(
  pathname: string,
  perms: PermissionFlags
): CreateAction[] {
  return CREATE_ACTIONS.filter(
    (action) => roleAllowed(action.role, perms) && action.href !== pathname
  );
}
