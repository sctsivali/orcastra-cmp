"use client";

import { Suspense, useState } from "react";
import { Plus, Puzzle, RotateCcw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { ExtensionsIllustration } from "@/components/illustrations/scenes/catalog";
import { Skeleton } from "@/components/ui/skeleton";
import { SortSelect } from "@/components/ui/sort-select";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { defineTable, fromSort } from "@/lib/table-filter";
import type { ExtensionDetail } from "@/types/api";
import { plural } from "@/lib/utils";

/**
 * Catalogue page sizes. Each divides by one, two and three columns, so a full page of cards never
 * ends in a ragged row.
 */
const PAGE_SIZES = [12, 24, 48, 96];

const ADDON_CATALOGUE = defineTable<ExtensionDetail>()({
  // important: two filtered lists share this page. Without a distinct id both write `q` and the
  // facet keys to the same query string and fight over them on every keystroke.
  id: "addons",
  searchPlaceholder: "Search add-ons",
  defaultSort: { field: "name", direction: "asc" },
  searchExtra: (row) => [row.slug, row.description ?? "", ...row.requested_capabilities],
  columns: [
    {
      field: "name",
      label: "Name",
      sort: (row) => row.name,
      search: (row) => row.name,
      filter: fromSort("text"),
    },
    {
      field: "publisher",
      label: "Publisher",
      sort: (row) => row.publisher,
      facet: (row) => row.publisher,
      facetSpec: { primary: true },
    },
    {
      field: "version",
      label: "Version",
      sort: (row) => row.version,
      search: (row) => row.version,
      filter: fromSort("text"),
    },
    {
      field: "capabilities",
      label: "Capabilities",
      // Sorted and filtered by count; the names are already searchable through `searchExtra`.
      sort: (row) => row.requested_capabilities.length,
      filter: fromSort("number", { unit: "count" }),
    },
  ],
});

interface AvailableAddonsProps {
  rows: ExtensionDetail[];
  installedSlugs: Set<string>;
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
  /** Empty until an organization is chosen, which is what gates installing rather than browsing. */
  organizationId: string;
  onInstall: (slug: string) => void;
  installPending: boolean;
}

function AvailableAddonsContent({
  rows,
  installedSlugs,
  loading,
  error,
  onRetry,
  organizationId,
  onInstall,
  installPending,
}: AvailableAddonsProps) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);
  const resetPage = () => setPage(1);

  // Filter and sort changes reset the page, or a search can land past the last page.
  const filters = useTableFilters(ADDON_CATALOGUE, rows, { onFilterChange: resetPage });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    ADDON_CATALOGUE.sortAccessors,
    ADDON_CATALOGUE.defaultSort.field,
    ADDON_CATALOGUE.defaultSort.direction,
    resetPage,
  );

  // Filter, then sort, then slice. Sorting covers the whole filtered set rather than the page.
  const sorted = sortItems(filters.filteredRows);
  // Clamped on read, never corrected in an effect: an effect would snap the operator back a page
  // mid-task and still paint one wrong frame first.
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const paginated = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  if (loading) {
    return (
      <div className="grid gap-3 sm:grid-cols-2">
        <Skeleton className="h-32 rounded-xl" />
        <Skeleton className="h-32 rounded-xl" />
      </div>
    );
  }

  // A failed read gets its own message and a Retry, distinct from an empty catalogue.
  if (error) {
    return (
      <EmptyState
        icon={RotateCcw}
        title="Could not load the catalogue"
        description={error.message}
        action={
          <Button size="sm" onClick={onRetry}>
            Retry
          </Button>
        }
      />
    );
  }

  if (rows.length === 0) {
    return (
      <EmptyState
        icon={Puzzle}
        illustration={ExtensionsIllustration}
        title="No add-ons are published yet"
        description='An add-on is an external application built against the Orcastra API. Use "Publish Add-on" above to make one installable here.'
      />
    );
  }

  return (
    <div className="space-y-4">
      <TableToolbar
        filters={filters}
        noun="add-ons"
        actions={
          <SortSelect
            options={[
              { value: "name", label: "Name" },
              { value: "publisher", label: "Publisher" },
              { value: "version", label: "Version" },
              { value: "capabilities", label: "Capabilities" },
            ]}
            value={sortField}
            direction={sortDirection}
            onSort={handleSort}
          />
        }
      />

      {paginated.length === 0 ? (
        <EmptyState
          icon={Puzzle}
          title="No add-on matches the current filters"
          description={`${plural(filters.activeFilterCount, "filter")} active`}
          action={
            <Button size="sm" variant="outline" onClick={() => filters.clearAll()}>
              Clear filters
            </Button>
          }
        />
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2">
          {paginated.map((extension) => (
            <li key={extension.slug} className="rounded-xl border bg-card p-4">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="font-semibold">{extension.name}</h3>
                  <p className="text-xs text-muted-foreground">
                    {extension.publisher} · v{extension.version}
                  </p>
                </div>
                {installedSlugs.has(extension.slug) ? (
                  <Badge variant="muted">Installed</Badge>
                ) : null}
              </div>
              {extension.description ? (
                <p className="mt-2 text-sm text-muted-foreground">{extension.description}</p>
              ) : null}
              <p className="mt-2 text-xs text-muted-foreground">
                Asks for {plural(extension.requested_capabilities.length, "capability", "capabilities")}
              </p>
              <Button
                size="sm"
                className="mt-3"
                disabled={!organizationId || installPending}
                onClick={() => onInstall(extension.slug)}
              >
                Review And Install
              </Button>
            </li>
          ))}
        </ul>
      )}

      {/* Absent rather than disabled when everything fits. */}
      {totalPages > 1 && (
        <DataTablePagination
          currentPage={currentPage}
          totalPages={totalPages}
          totalItems={sorted.length}
          pageSize={pageSize}
          pageSizeOptions={PAGE_SIZES}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setPage(1);
          }}
        />
      )}
    </div>
  );
}

/**
 * Carries its own Suspense boundary. Reading the query string opts a client component out of
 * prerendering, and `next build` fails on the route unless a boundary sits above it.
 */
export function AvailableAddons(props: AvailableAddonsProps) {
  return (
    <Suspense fallback={<Skeleton className="h-32 rounded-xl" />}>
      <AvailableAddonsContent {...props} />
    </Suspense>
  );
}
