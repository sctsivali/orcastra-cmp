"use client";

import { Suspense, useState } from "react";
import { PauseCircle, PlayCircle, Puzzle, RotateCcw, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { OverflowCell } from "@/components/ui/overflow-cell";
import { RowActions, type RowAction } from "@/components/ui/row-actions";
import { Skeleton } from "@/components/ui/skeleton";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { INSTALL_STATUS } from "@/lib/extension-display";
import { formatLabel } from "@/lib/utils";
import { defineTable, fromSort } from "@/lib/table-filter";
import type { ExtensionInstall } from "@/types/api";

const COLUMN_COUNT = 6;

const INSTALLED_ADDONS = defineTable<ExtensionInstall>()({
  // See the note on the catalogue's id: the two lists share one query string, so each namespaces
  // its own parameters.
  id: "installed",
  searchPlaceholder: "Search installations",
  defaultSort: { field: "name", direction: "asc" },
  searchExtra: (row) => [
    row.extension_slug,
    row.api_key_id,
    ...row.granted_capabilities,
    ...row.granted_scope.map((grant) => grant.cluster_id),
  ],
  columns: [
    {
      field: "name",
      label: "Add-on",
      sort: (row) => row.extension_name,
      search: (row) => row.extension_name,
      filter: fromSort("text"),
    },
    {
      field: "organization",
      label: "Organization",
      sort: (row) => row.organization_slug ?? String(row.organization_id),
      facet: (row) => row.organization_slug ?? String(row.organization_id),
      facetSpec: { primary: true },
    },
    {
      field: "status",
      label: "Status",
      // Raw for the sort and the facet value, because the value travels in the shared filter URL.
      // Only the label is title-cased.
      sort: (row) => row.status,
      facet: (row) => row.status,
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      field: "version",
      label: "Version",
      sort: (row) => row.extension_version,
      search: (row) => row.extension_version,
      filter: fromSort("text"),
    },
    {
      field: "capabilities",
      label: "Capabilities",
      sort: (row) => row.granted_capabilities.length,
      filter: fromSort("number", { unit: "count" }),
    },
    {
      field: "clusters",
      label: "Clusters",
      // No sort accessor on purpose. Ordering installations by how many clusters they reach
      // answers nothing an operator asks, while narrowing to the ones on a given cluster does.
      facet: (row) => row.granted_scope.map((grant) => grant.cluster_id),
      facetSpec: { emptyLabel: "(no clusters)" },
    },
  ],
});

interface InstalledTableProps {
  rows: ExtensionInstall[];
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
  onSetStatus: (row: ExtensionInstall, action: "suspend" | "resume") => void;
  onUninstall: (row: ExtensionInstall) => void;
  actionPending: boolean;
}

function InstalledTableContent({
  rows,
  loading,
  error,
  onRetry,
  onSetStatus,
  onUninstall,
  actionPending,
}: InstalledTableProps) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const resetPage = () => setPage(1);

  const filters = useTableFilters(INSTALLED_ADDONS, rows, { onFilterChange: resetPage });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    INSTALLED_ADDONS.sortAccessors,
    INSTALLED_ADDONS.defaultSort.field,
    INSTALLED_ADDONS.defaultSort.direction,
    resetPage,
  );

  const sorted = sortItems(filters.filteredRows);
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const paginated = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const actionsFor = (row: ExtensionInstall): RowAction[] => {
    const actions: RowAction[] = [];
    if (row.status === "active") {
      actions.push({
        id: "suspend",
        label: "Suspend",
        icon: PauseCircle,
        onSelect: () => onSetStatus(row, "suspend"),
        unavailableReason: actionPending ? "Another change is still in flight" : undefined,
      });
    } else if (row.status === "suspended") {
      actions.push({
        id: "resume",
        label: "Resume",
        icon: PlayCircle,
        onSelect: () => onSetStatus(row, "resume"),
        unavailableReason: actionPending ? "Another change is still in flight" : undefined,
      });
    }
    actions.push({
      id: "uninstall",
      label: "Uninstall",
      icon: Trash2,
      destructive: true,
      onSelect: () => onUninstall(row),
    });
    return actions;
  };

  if (loading) return <Skeleton className="h-40 rounded-lg" />;

  // Distinct from "nothing is installed": a failed read used to render the empty state, which told
  // an operator their installations were gone when the dashboard simply got no answer.
  if (error) {
    return (
      <EmptyState
        icon={RotateCcw}
        title="Could not load installations"
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
        title="Nothing is installed yet"
        description="Installing an add-on issues it a credential scoped to one organization."
      />
    );
  }

  return (
    <div className="space-y-4">
      <TableToolbar filters={filters} noun="installations" />

      <Table>
        <TableHeader>
          <TableRow>
            <SortableTableHead
              label="Add-on"
              field="name"
              currentField={sortField}
              direction={sortDirection}
              onSort={handleSort}
            />
            <SortableTableHead
              label="Organization"
              field="organization"
              currentField={sortField}
              direction={sortDirection}
              onSort={handleSort}
            />
            <SortableTableHead
              label="Status"
              field="status"
              currentField={sortField}
              direction={sortDirection}
              onSort={handleSort}
            />
            <SortableTableHead
              label="Capabilities"
              field="capabilities"
              currentField={sortField}
              direction={sortDirection}
              onSort={handleSort}
            />
            <TableHead>Clusters</TableHead>
            {/* Named for a screen reader rather than labelled, so it does not read as data. */}
            <TableHead className="w-12 text-right">
              <span className="sr-only">Actions</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {paginated.length === 0 ? (
            <TableEmptyRow
              colSpan={COLUMN_COUNT}
              icon={Puzzle}
              title="No installation matches the current filters"
              description={`${filters.activeFilterCount} filter${filters.activeFilterCount === 1 ? "" : "s"} active`}
            />
          ) : (
            paginated.map((row) => (
              <TableRow key={row.id}>
                <TableCell className="font-medium">
                  <TableLink href={`/extensions/${row.id}`}>{row.extension_name}</TableLink>
                  <TableSubText>v{row.extension_version}</TableSubText>
                </TableCell>
                <TableCell>{row.organization_slug ?? `organization ${row.organization_id}`}</TableCell>
                <TableCell>
                  <Badge variant={INSTALL_STATUS[row.status].tone}>
                    {INSTALL_STATUS[row.status].label}
                  </Badge>
                </TableCell>
                <TableCell>
                  <OverflowCell
                    items={row.granted_capabilities}
                    preview={2}
                    noun="capabilities"
                    ownerLabel={row.extension_name}
                    emptyLabel="No capabilities"
                    itemLabel="capability"
                    renderInline={(capability) => capability}
                  />
                </TableCell>
                <TableCell>
                  <OverflowCell
                    items={row.granted_scope.map((grant) => grant.cluster_id)}
                    preview={2}
                    noun="clusters"
                    ownerLabel={row.extension_name}
                    emptyLabel="No clusters"
                    itemLabel="cluster"
                    renderInline={(cluster) => cluster}
                  />
                </TableCell>
                <TableCell className="text-right">
                  <RowActions
                    triggerLabel={`Actions for ${row.extension_name}`}
                    actions={actionsFor(row)}
                  />
                </TableCell>
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>

      {totalPages > 1 && (
        <DataTablePagination
          currentPage={currentPage}
          totalPages={totalPages}
          totalItems={sorted.length}
          pageSize={pageSize}
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
 * The boundary the query string requires.
 *
 * Reading the query string opts a client component out of prerendering, and Next then fails
 * `next build` on the route unless a boundary sits above it. Carrying its own means this can be
 * dropped anywhere without the caller having to know that about it.
 */
export function InstalledTable(props: InstalledTableProps) {
  return (
    <Suspense fallback={<Skeleton className="h-40 rounded-lg" />}>
      <InstalledTableContent {...props} />
    </Suspense>
  );
}
