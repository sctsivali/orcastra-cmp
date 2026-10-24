"use client";

import * as React from "react";
import type { ClusterAccessDisplay } from "@/lib/integration-display";
import { Badge } from "@/components/ui/badge";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { OverflowText } from "@/components/ui/overflow-text";
import { SearchInput } from "@/components/ui/search-input";
import { useDebouncedValue } from "@/hooks/use-debounced-value";

// The panel shows at most this many rows, and every control appears exactly when a cluster can
// be off-screen. One constant rather than two: the house search threshold of six beside a page
// of five would give a six-cluster key a pager and no way to search for the row it had hidden.
const PAGE_SIZE = 5;

interface IntegrationClusterAccessListProps {
  clusters: ClusterAccessDisplay[];
  /** The key this panel belongs to. Names its controls among the several on one screen. */
  keyName: string;
}

function haystack(cluster: ClusterAccessDisplay): string {
  // note: the id is searchable, not only the name. list_api_keys never filters a key's own
  // permission array while available-clusters drops what the caller cannot reach, so a row whose
  // name has fallen back to the raw cluster id is the ordinary case here, not an edge one, and
  // searching by name alone would miss exactly the rows an operator is hunting for.
  return `${cluster.name} ${cluster.id} ${cluster.typeLabel} ${cluster.permissionLabel} ${cluster.projectsLabel}`.toLowerCase();
}

export function IntegrationClusterAccessList({
  clusters,
  keyName,
}: IntegrationClusterAccessListProps) {
  const [searchInput, setSearchInput] = React.useState("");
  const [page, setPage] = React.useState(1);
  const query = useDebouncedValue(searchInput, 200);

  // note: the index comes from the full array and is part of the row key. Two permission entries
  // can carry the same cluster_id, because the permissions endpoint writes the array verbatim
  // with no dedupe, and a page-local index would let row three of one filtered set reconcile
  // against row three of another, carrying OverflowText's expanded state onto a different value.
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
  // new array on every parent render and useRefreshableQuery re-renders on each isFetching
  // transition, so an effect keyed on `clusters` would reset every card twice per refresh while
  // the operator was mid-task, and one keyed on the overflow would still paint an empty frame
  // before it caught up. Without this the pager reads "Showing 11 to 2 of 2" over no rows.
  const currentPage = Math.min(page, totalPages);
  const rows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  if (clusters.length === 0) {
    return (
      <div className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
        No cluster access configured for this key.
      </div>
    );
  }

  // note: gated on the full count, never the filtered one. A field that disappears once the
  // search narrows past the threshold strands a query nobody can then clear, which is the
  // inverted guard documentation/ui/value-reveal.md records for SearchableSelect.
  const showControls = clusters.length > PAGE_SIZE;

  const countPart =
    filtered.length === clusters.length
      ? `${clusters.length} clusters`
      : `${filtered.length} of ${clusters.length} clusters`;
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
            // The reset-to-first-page contract every table page here follows, applied at the
            // source of the change so no effect is needed to notice it.
            setPage(1);
          }}
          placeholder="Search clusters"
          label={`Search cluster access for ${keyName}`}
          clearLabel={`Clear cluster access search for ${keyName}`}
        />
      )}

      {/* note: outside the pager's gate on purpose. The count sentence left DataTablePagination
          because that component only renders when totalPages > 1, so the count vanished at
          exactly the moment a filter made it worth reading. */}
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
