"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { usePermissions } from "@/hooks/use-permissions";
import { useLayout } from "../layout-provider";
import { NAV_GROUPS, filterGroups } from "./nav-config";

// Cmd/Ctrl+K quick-nav. Sourced from the same role-filtered NAV_GROUPS as the sidebar
// (single source of truth), so the palette and the rail can never drift. Mounted
// globally so it works even on full-page views where the sidebar is hidden.
export function CommandPalette() {
  const { commandPaletteOpen, setCommandPaletteOpen } = useLayout();
  const { isAdmin, isPartner, isTenant } = usePermissions();
  const router = useRouter();

  const groups = React.useMemo(
    () => filterGroups(NAV_GROUPS, { isAdmin, isPartner, isTenant }),
    [isAdmin, isPartner, isTenant]
  );

  const go = (href: string) => {
    setCommandPaletteOpen(false);
    router.push(href);
  };

  return (
    <CommandDialog open={commandPaletteOpen} onOpenChange={setCommandPaletteOpen}>
      <CommandInput placeholder="Search pages..." />
      <CommandList>
        <CommandEmpty>No results found.</CommandEmpty>
        {groups.map((group) => {
          const items = group.items.filter(
            (i) => i.href && !i.render && !i.hideFromCommandPalette
          );
          if (items.length === 0) return null;
          return (
            <CommandGroup key={group.id} heading={group.label ?? "General"}>
              {items.map((item) => {
                const Icon = item.icon;
                return (
                  <CommandItem
                    key={item.id}
                    value={`${group.label ?? ""} ${item.label}`}
                    onSelect={() => go(item.href!)}
                  >
                    <Icon className="mr-2 h-4 w-4" />
                    {item.label}
                  </CommandItem>
                );
              })}
            </CommandGroup>
          );
        })}
      </CommandList>
    </CommandDialog>
  );
}
