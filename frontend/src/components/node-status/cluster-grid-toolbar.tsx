"use client";

import { TagFilter } from "@/components/tag-manager";
import { SortSelect } from "@/components/ui/sort-select";
import { TableToolbar } from "@/components/ui/table-toolbar";
import type { TableToolbarState } from "@/hooks/use-table-filters";
import { defineTable } from "@/lib/table-filter";
import type { SortDirection } from "@/lib/sort";
import { NodeStatus } from "@/types";

// The Overview Clusters card grid on the shared filter engine, the same way the Projects grid
// beside it is wired. A .tsx file rather than .ts because check-table-filters.mjs walks only
// .tsx/.jsx, so a definition in a .ts file would sit outside the guard.
//
// important: `id: "clusters"` namespaces the query keys (clusters.q, clusters.tags). PROJECTS on
// the same page declares no id and writes bare q/tags/show, so without this id the two toolbars
// would silently share keys and a search typed into one would narrow the other.
//
// note: the name column is search-only, no `filter:`. On a two-column definition a text filter
// would duplicate the search box, and a filterable column labelled Cluster with no role trips
// check-table-filters.mjs Rule 2 unless this file is added to ROLE_EXEMPT, which is not worth a
// guard edit for a redundant control. The card row IS the node, so no column declares a role.
export const CLUSTER_CARDS = defineTable<NodeStatus>()({
  id: "clusters",
  searchPlaceholder: "Search clusters by name, id, or tag",
  // One value per element so a query cannot match across the id and a tag.
  searchExtra: (n) => [n.name, ...(n.tags ?? [])],
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    {
      field: "name",
      label: "Cluster",
      sort: (n) => n.display_name || n.name,
      search: (n) => n.display_name || n.name,
    },
    {
      field: "tags",
      label: "Tags",
      facet: (n) => n.tags ?? [],
      facetSpec: { emptyLabel: "(untagged)" },
    },
  ],
});

export type ClusterField = "name" | "tags";
export type ClusterSortField = "name";

// Name only. CPU and RAM would reorder the cards under the reader on every poll, a host that has
// not answered carries null resources and would pin to the bottom, and the two ratios measure
// different things (allocation vs usage) so one "usage" label would lie for one of them.
export const CLUSTER_SORT_OPTIONS: readonly { value: ClusterSortField; label: string }[] = [
  { value: "name", label: CLUSTER_CARDS.byField.name.label },
];

interface ClusterGridToolbarProps {
  filters: TableToolbarState<ClusterField>;
  sortField: ClusterSortField;
  sortDirection: SortDirection;
  onSort: (field: ClusterSortField) => void;
  availableTags: string[];
  selectedTags: string[];
}

export function ClusterGridToolbar({
  filters,
  sortField,
  sortDirection,
  onSort,
  availableTags,
  selectedTags,
}: ClusterGridToolbarProps) {
  return (
    <>
      {/* "online clusters", not "clusters": the toolbar counts the rows it was handed, which
          are the online subset, while the summary above it counts the whole registered fleet.
          "4 of 20 online clusters" beside "20 of 23 connected" cannot contradict each other. */}
      <TableToolbar
        filters={filters}
        noun="online clusters"
        actions={
          <SortSelect
            options={CLUSTER_SORT_OPTIONS}
            value={sortField}
            direction={sortDirection}
            onSort={onSort}
          />
        }
      />

      {/* An alternative renderer for the Tags facet, not a second control: its colours come from
          the tag prefix, which the generic popover cannot show. */}
      {availableTags.length > 0 && (
        <TagFilter
          availableTags={availableTags}
          selectedTags={selectedTags}
          onTagsChange={(tags) => filters.setFacet("tags", tags)}
        />
      )}
    </>
  );
}
