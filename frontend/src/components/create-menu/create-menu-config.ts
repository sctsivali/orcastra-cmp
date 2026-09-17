import { Box, FolderKanban, Server, type LucideIcon } from "lucide-react";
import type { PermissionFlags, RoleGate } from "@/components/sidebar/types";

// Single source of truth for the global create menu. The header renders this list twice, as the
// desktop dropdown and as the mobile floating action button, so the two surfaces cannot drift
// apart. Gates are data, the same shape the sidebar uses in nav-config.tsx: the renderer never
// sees role logic, it receives an already-filtered list.

/** Sidebar IA section the action belongs to, so ordering stays anchored to NAV_GROUPS. */
export type CreateSection = "compute" | "networking" | "cluster";

export interface CreateAction {
  /** Stable key. React key, and the anchor the browser QA spec matches on. */
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

// Ordered as NAV_GROUPS is: Compute, then Networking, then Cluster. Icons are the ones the
// matching sidebar entries already use, so the menu and the nav read as one product.
// note: a create that lives in a dialog on its own list page (networks, network ACLs, ISO and
// image uploads) is not offered here. The menu is a list of destinations, real links an
// operator can open in a new tab, and a dialog is not a destination.
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
    label: "Register Node",
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
 * The actions to offer right now.
 *
 * important: the item for the route the operator is already standing on is dropped here rather
 * than by a hardcoded pathname list in the header. That list named exactly the two routes the
 * menu happened to offer and would have gone stale the moment a third action was added.
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
