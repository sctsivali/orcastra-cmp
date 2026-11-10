import type { LucideIcon } from "lucide-react";
import type { RouteIntent } from "@/components/navigation/route-intent";

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
  // Renders one level deeper in the expanded tree: Terminals and Consoles are live instances
  // of what /inventory/sessions lists.
  indent?: boolean;
  // A live count beside the label, and on the collapsed rail's group icon. Admin-only items only:
  // the count's read is refused (and audited) for any other role.
  badge?: "host-health";
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

// Shared rendering context threaded to nav items. `intent` is already guarded against the
// active route and resolves the React Query warm map, so a renderer never sees either concern.
// There is no onLinkClick: the mobile drawer and the collapsed flyout close when the new route
// commits, since closing on click would unmount the <Link> and its pending spinner.
export interface NavRenderContext {
  activeHref: string | null;
  intent: RouteIntent;
  /** Resolves the React Query warm-up for a prefetch key, so renderers stay data-agnostic. */
  warmFor: (key: string) => () => void;
}
