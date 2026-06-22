import type { LucideIcon } from "lucide-react";

// Role gate: minimum role required. "tenant" = any authenticated user (everyone),
// "partner" = Partner or Admin, "admin" = Admin only. Maps to usePermissions() flags.
export type RoleGate = "admin" | "partner" | "tenant";

// Deployment gate compares against NEXT_PUBLIC_AUTH_MODE (baked at build time).
export type DeploymentGate = "client-cert" | "oidc";

// Escape hatch for the two store-driven items (live Terminal/Console windows).
export type SpecialRender = "terminal-menu" | "console-menu";

export interface NavItem {
  id: string;
  label: string;
  href?: string; // leaf destination; omitted for store-driven render items
  icon: LucideIcon;
  role?: RoleGate;
  deploymentMode?: DeploymentGate;
  render?: SpecialRender; // when set, renders the live session menu instead of a link
  prefetchKey?: string; // key into the prefetch map; defaults to href
  hideFromCommandPalette?: boolean;
}

export interface NavGroup {
  id: string;
  label?: string; // section heading; undefined = pinned/top (no header)
  role?: RoleGate; // gate the whole group
  deploymentMode?: DeploymentGate;
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
