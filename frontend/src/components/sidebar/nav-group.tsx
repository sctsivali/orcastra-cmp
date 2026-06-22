"use client";

import type { NavGroup, NavRenderContext } from "./types";
import { NavItemRow } from "./nav-item";

interface NavSectionProps {
  group: NavGroup;
  ctx: NavRenderContext;
}

// A titled section in the expanded sidebar: a persistent (non-collapsible) heading
// followed by its items. Pinned groups have no label and render items directly.
export function NavSection({ group, ctx }: NavSectionProps) {
  return (
    <div className="space-y-1">
      {group.label && (
        <p className="px-2 pt-3 pb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {group.label}
        </p>
      )}
      {group.items.map((item) => (
        <NavItemRow key={item.id} item={item} ctx={ctx} />
      ))}
    </div>
  );
}
