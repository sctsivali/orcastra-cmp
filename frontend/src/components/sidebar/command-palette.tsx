"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  ArrowRight,
  Box,
  Database,
  FolderKanban,
  HardDrive,
  Image as ImageIcon,
  Loader2,
  Network,
  ScrollText,
  Search,
  SlidersHorizontal,
  Tag,
  UserPlus,
} from "lucide-react";
import {
  CommandDialog,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { usePermissions } from "@/hooks/use-permissions";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { MIN_SEARCH_LEN, useGlobalSearch } from "@/hooks/use-global-search";
import type { SearchResultItem } from "@/types";
import { useLayout } from "../layout-provider";
import { NAV_GROUPS, filterGroups } from "./nav-config";

// Icon per resource type returned by the backend search adapters.
const TYPE_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  instance: Box,
  project: FolderKanban,
  network: Network,
  "storage-pool": Database,
  "storage-volume": HardDrive,
  image: ImageIcon,
  profile: SlidersHorizontal,
  tag: Tag,
  identity: UserPlus,
  audit: ScrollText,
};

// Cmd/Ctrl+K palette. Two sources, one box: in-app PAGES (filtered client-side
// from the same role-aware NAV_GROUPS the sidebar uses) and live RESOURCES from
// the RBAC-scoped backend /search endpoint. shouldFilter is off so server results
// render verbatim; we filter the page list ourselves with the same query.
export function CommandPalette() {
  const { commandPaletteOpen, setCommandPaletteOpen } = useLayout();
  const { isAdmin, isPartner, isTenant } = usePermissions();
  const router = useRouter();

  const [query, setQuery] = React.useState("");
  const debounced = useDebouncedValue(query, 250);
  const { data, isFetching } = useGlobalSearch(debounced);

  // clear the box each time the palette closes
  React.useEffect(() => {
    if (!commandPaletteOpen) setQuery("");
  }, [commandPaletteOpen]);

  const pageGroups = React.useMemo(
    () => filterGroups(NAV_GROUPS, { isAdmin, isPartner, isTenant }),
    [isAdmin, isPartner, isTenant]
  );

  const q = debounced.trim().toLowerCase();
  const filteredPages = React.useMemo(
    () =>
      pageGroups
        .map((group) => ({
          id: group.id,
          label: group.label ?? "Pages",
          items: group.items.filter(
            (i) =>
              i.href &&
              !i.render &&
              !i.hideFromCommandPalette &&
              (!q || `${group.label ?? ""} ${i.label}`.toLowerCase().includes(q))
          ),
        }))
        .filter((g) => g.items.length > 0),
    [pageGroups, q]
  );

  const resourceGroups = data?.groups ?? [];
  const hasAny = filteredPages.length > 0 || resourceGroups.length > 0;
  const showHint = q.length > 0 && q.length < MIN_SEARCH_LEN;
  const trimmed = debounced.trim();

  const go = (href: string) => {
    setCommandPaletteOpen(false);
    router.push(href);
  };

  return (
    <CommandDialog open={commandPaletteOpen} onOpenChange={setCommandPaletteOpen} shouldFilter={false}>
      <CommandInput
        placeholder="Search resources and pages..."
        value={query}
        onValueChange={setQuery}
      />
      <CommandList>
        {!hasAny && !isFetching && (
          <div className="py-6 text-center text-sm text-muted-foreground">
            {showHint
              ? "Keep typing to search..."
              : q
              ? "No results found."
              : "Search instances, networks, storage, images, tags and pages."}
          </div>
        )}

        {filteredPages.map((group) => (
          <CommandGroup key={group.id} heading={group.label}>
            {group.items.map((item) => {
              const Icon = item.icon;
              return (
                <CommandItem
                  key={item.id}
                  value={`page:${item.id}`}
                  onSelect={() => go(item.href!)}
                >
                  <Icon className="mr-2 h-4 w-4 shrink-0" />
                  {item.label}
                </CommandItem>
              );
            })}
          </CommandGroup>
        ))}

        {resourceGroups.map((group) => {
          const heading =
            group.total > group.items.length ? `${group.label} (${group.total})` : group.label;
          return (
            <CommandGroup key={group.type} heading={heading}>
              {group.items.map((item: SearchResultItem) => {
                const Icon = TYPE_ICONS[item.type] ?? Search;
                return (
                  <CommandItem
                    key={`${item.type}:${item.id}`}
                    value={`${item.type}:${item.id}`}
                    onSelect={() => go(item.href)}
                  >
                    <Icon className="mr-2 h-4 w-4 shrink-0" />
                    <div className="flex min-w-0 flex-col">
                      <span className="truncate">{item.title}</span>
                      {item.subtitle && (
                        <span className="truncate text-xs text-muted-foreground">{item.subtitle}</span>
                      )}
                    </div>
                  </CommandItem>
                );
              })}
            </CommandGroup>
          );
        })}

        {trimmed.length >= MIN_SEARCH_LEN && (data?.total ?? 0) > 0 && (
          <CommandGroup heading="Actions">
            <CommandItem
              value="__view_all_results"
              onSelect={() => go(`/search?q=${encodeURIComponent(trimmed)}`)}
            >
              <ArrowRight className="mr-2 h-4 w-4 shrink-0" />
              View all results for &ldquo;{trimmed}&rdquo;
            </CommandItem>
          </CommandGroup>
        )}

        {isFetching && (
          <div className="flex items-center justify-center gap-2 py-3 text-xs text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" /> Searching...
          </div>
        )}
      </CommandList>
    </CommandDialog>
  );
}
