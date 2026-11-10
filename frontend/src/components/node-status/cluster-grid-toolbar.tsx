"use client";

import { TagFilter } from "@/components/tag-manager";
import { SortSelect } from "@/components/ui/sort-select";
import { TableToolbar } from "@/components/ui/table-toolbar";
import type { TableToolbarState } from "@/hooks/use-table-filters";
import { defineTable } from "@/lib/table-filter";
import type { SortDirection } from "@/lib/sort";
import { NodeStatus } from "@/types";

// The Overview Clusters card grid on the shared filter engine, wired like the Projects grid
// beside it. A .tsx file because check-table-filters.mjs walks only .tsx/.jsx. The name column
// is search-only: a text filter would duplicate the search box, and a filterable column with no
// role trips check-table-filters.mjs Rule 2.
//
// important: `id: "clusters"` namespaces the query keys (clusters.q, clusters.tags). PROJECTS on
// the same page writes bare q/tags/show, so without it a search typed into one toolbar would
// narrow the other.
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

// Name only. CPU and RAM would reorder the cards on every poll, a host that has not answered
// carries null resources, and the two ratios measure different things (allocation vs usage).
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
      {/* The toolbar counts the online subset it was handed, while the summary above counts
          the whole registered fleet, so the noun says "online clusters". */}
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

      {/* Renders the Tags facet with colours from the tag prefix, which the generic popover
          cannot show. */}
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
