import type { LucideIcon } from "lucide-react";

// Role gate: minimum role required. "tenant" = any authenticated user (everyone),
// "partner" = Partner or Admin, "admin" = Admin only. Maps to usePermissions() flags.
export type RoleGate = "admin" | "partner" | "tenant";

// Escape hatch for the two store-driven items (live Terminal/Console windows).
export type SpecialRender = "terminal-menu" | "console-menu";

export interface NavItem {
  id: string;
  label: string;
  href?: string; // leaf destination; omitted for store-driven render items
  icon: LucideIcon;
  role?: RoleGate;
  render?: SpecialRender; // when set, renders the live session menu instead of a link
  prefetchKey?: string; // key into the prefetch map; defaults to href
  // note: renders one level deeper in the expanded tree. Terminals and Consoles are live
  // instances of what /inventory/sessions lists, and that hierarchy is meaningful.
  indent?: boolean;
}

export interface NavGroup {
  id: string;
  label?: string; // section heading; undefined = pinned/top (no header)
  role?: RoleGate; // gate the whole group
  icon?: LucideIcon; // rail icon shown when collapsed (labeled groups become flyouts)
  items: NavItem[];
}

export interface PermissionFlags {
  isAdmin: boolean;
  isPartner: boolean;
  isTenant: boolean;
}

// Shared rendering context threaded to nav items so they keep mobile-close + hover-prefetch.
export interface NavRenderContext {
  activeHref: string | null;
  onLinkClick: () => void;
  prefetch: (key: string) => void;
}
