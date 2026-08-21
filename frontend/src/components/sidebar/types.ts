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

// Shared rendering context threaded to nav items. `intent` is already guarded against the
// active route and already resolves the React Query warm map, so a renderer never sees either
// concern.
// note: there is deliberately no onLinkClick. The mobile drawer and the collapsed flyout used
// to close on click, which unmounted the <Link> and killed its pending spinner. Both now close
// when the new route commits, which is what the operator is actually waiting for.
export interface NavRenderContext {
  activeHref: string | null;
  intent: RouteIntent;
  /** Resolves the React Query warm-up for a prefetch key, so renderers stay data-agnostic. */
  warmFor: (key: string) => () => void;
}
