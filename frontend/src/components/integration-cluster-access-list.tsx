"use client";

import * as React from "react";
import type { ClusterAccessDisplay } from "@/lib/integration-display";
import { Badge } from "@/components/ui/badge";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { OverflowText } from "@/components/ui/overflow-text";
import { SearchInput } from "@/components/ui/search-input";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { plural } from "@/lib/utils";

// Page size and the threshold for search and pager, as one constant: with separate values a key
// could get a pager without a way to search for the rows it hides.
const PAGE_SIZE = 5;

interface IntegrationClusterAccessListProps {
  clusters: ClusterAccessDisplay[];
  /** The key this panel belongs to. Names its controls among the several on one screen. */
  keyName: string;
}

function haystack(cluster: ClusterAccessDisplay): string {
  // The id is searchable too. list_api_keys never filters a key's own permission array while
  // available-clusters drops what the caller cannot reach, so a row named by its raw cluster id
  // is common here.
  return `${cluster.name} ${cluster.id} ${cluster.typeLabel} ${cluster.permissionLabel} ${cluster.projectsLabel}`.toLowerCase();
}

export function IntegrationClusterAccessList({
  clusters,
  keyName,
}: IntegrationClusterAccessListProps) {
  const [searchInput, setSearchInput] = React.useState("");
  const [page, setPage] = React.useState(1);
  const query = useDebouncedValue(searchInput, 200);

  // The row key includes the index in the full array. Two permission entries can carry the same
  // cluster_id (the permissions endpoint does not dedupe), and a page-local index would carry
  // OverflowText's expanded state onto a different row when the filter changes.
  const keyed = React.useMemo(
    () => clusters.map((cluster, index) => ({ cluster, key: `${cluster.id}-${index}` })),
    [clusters],
  );

  const filtered = React.useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return keyed;
    return keyed.filter((row) => haystack(row.cluster).includes(needle));
  }, [keyed, query]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));

  // important: clamped on read, never corrected in an effect. getClusterAccessDisplay builds a
  // new array on every parent render, so an effect keyed on `clusters` would reset the page on
  // every refresh, and one keyed on the overflow would paint an empty frame first.
  const currentPage = Math.min(page, totalPages);
  const rows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  if (clusters.length === 0) {
    return (
      <div className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
        No cluster access configured for this key.
      </div>
    );
  }

  // Gated on the full count, never the filtered one: a search field that disappears once the
  // query narrows the list would strand a query nobody can clear.
  const showControls = clusters.length > PAGE_SIZE;

  const countPart =
    filtered.length === clusters.length
      ? plural(clusters.length, "cluster")
      : `${filtered.length} of ${plural(clusters.length, "cluster")}`;
  const firstRow = (currentPage - 1) * PAGE_SIZE + 1;
  const lastRow = Math.min(currentPage * PAGE_SIZE, filtered.length);
  const countSentence =
    totalPages > 1 ? `${countPart}, showing ${firstRow} to ${lastRow}` : countPart;

  return (
    <div data-slot="cluster-access" className="space-y-2 rounded-md border bg-muted/30 p-3">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Cluster Access
      </p>

      {showControls && (
        <SearchInput
          value={searchInput}
          onChange={(value) => {
            setSearchInput(value);
            // Back to the first page on a new search, set here so no effect is needed.
            setPage(1);
          }}
          placeholder="Search clusters"
          label={`Search cluster access for ${keyName}`}
          clearLabel={`Clear cluster access search for ${keyName}`}
        />
      )}

      {/* Outside the pager's gate: DataTablePagination renders only when totalPages > 1, and the
          count matters most when a filter has narrowed the list to one page. */}
      {showControls && (
        <p
          role="status"
          aria-live="polite"
          aria-atomic="true"
          className="text-xs text-muted-foreground"
        >
          {countSentence}
        </p>
      )}

      {rows.length === 0 ? (
        <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
          No clusters match this search.
        </p>
      ) : (
        <ul className="space-y-2">
          {rows.map(({ cluster, key }) => (
            <li
              key={key}
              data-slot="cluster-access-row"
              className="flex flex-col gap-2 rounded-md bg-background/70 px-3 py-2 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0">
                <p className="text-sm font-medium">
                  <OverflowText value={cluster.name} label="cluster name" />
                </p>
                <p className="text-xs text-muted-foreground">
                  <OverflowText
                    value={`${cluster.typeLabel}${cluster.name !== cluster.id ? ` - ${cluster.id}` : ""}`}
                    label={`id for ${cluster.name}`}
                  />
                </p>
              </div>
              <div className="min-w-0 text-xs sm:text-right">
                {cluster.paused && (
                  <Badge variant="caution" className="mb-1">
                    Access paused
                  </Badge>
                )}
                <p className="font-medium text-foreground">{cluster.permissionLabel}</p>
                <p className="text-muted-foreground">
                  <OverflowText
                    value={cluster.projectsLabel}
                    label={`projects for ${cluster.name}`}
                  />
                </p>
              </div>
            </li>
          ))}
        </ul>
      )}

      {totalPages > 1 && (
        <DataTablePagination
          variant="compact"
          currentPage={currentPage}
          totalPages={totalPages}
          onPageChange={setPage}
          label={`cluster access for ${keyName}`}
        />
      )}
    </div>
  );
}
