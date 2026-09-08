"use client";

import type { SnapshotInfo } from "@/types/api";

import { Suspense, useMemo, useState } from "react";
import { History } from "lucide-react";

import { LoadingLottiePage } from "@/components/loading-lottie";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { RefreshButton } from "@/components/ui/refresh-button";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { useFleetSnapshots, useProjects } from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { useRequireRole } from "@/hooks/use-require-role";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { defineTable, fromSort } from "@/lib/table-filter";
import { TABLE_PAGE_SIZE } from "@/lib/pagination";
import { formatBytes } from "@/lib/utils";
import { Role } from "@/types/rbac";

import { SnapshotScopePicker } from "./components/snapshot-scope-picker";
import { SnapshotRowActions } from "./components/snapshot-row-actions";
import { relativeAge, expiryLabel } from "./components/snapshot-format";

const SNAPSHOTS = defineTable<SnapshotInfo>()({
  searchPlaceholder: "Search snapshots",
  defaultSort: { field: "created_at", direction: "desc" },
  columns: [
    {
      field: "name",
      label: "Snapshot",
      sort: (s) => s.name,
      search: (s) => s.name,
      filter: fromSort("text"),
    },
    {
      field: "source_name",
      label: "Instance",
      sort: (s) => s.source_name,
      search: (s) => s.source_name,
      filter: fromSort("text"),
    },
    {
      field: "origin",
      label: "Origin",
      // The one distinction that changes what an operator may do with a row: this console can
      // only act on what it created. Everything else belongs to the metered product upstream.
      facet: (s) => (s.operator_managed ? "Operator" : "Managed elsewhere"),
      facetSpec: { primary: true },
    },
    {
      field: "size",
      label: "Size",
      sort: (s) => s.size ?? 0,
      filter: fromSort("number", { unit: "bytes" }),
    },
    {
      field: "created_at",
      label: "Created",
      sort: (s) => (s.created_at ? Date.parse(s.created_at) : null),
      filter: fromSort("date", { horizon: "past" }),
    },
    {
      field: "expires_at",
      label: "Expires",
      // A snapshot with no expiry is one this console did not create, so the empty label names
      // the thing rather than leaving a blank an operator has to interpret.
      sort: (s) => (s.expires_at ? Date.parse(s.expires_at) : null),
      filter: fromSort("date", { horizon: "future", emptyLabel: "No expiry" }),
    },
  ],
});

function SnapshotsContent() {
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  const nodePicker = useNodePickerOptions();
  const [host, setHost] = useState<string>("");
  const [project, setProject] = useState<string>("default");

  const projectsQuery = useProjects(host);
  // The picker labels a cluster by its display name, so the card must agree. Answering with the
  // raw id makes the operator map "NUC Industrial" onto "nuc2" themselves.
  const hostLabel = useMemo(
    () => nodePicker.options.find((o) => o.value === host)?.label ?? host,
    [nodePicker.options, host],
  );
  const { data, isLoading, isError, error, refreshNow, dataUpdatedAt } = useFleetSnapshots(
    host,
    project,
  );

  const snapshots: SnapshotInfo[] = useMemo(() => data?.items ?? [], [data]);
  const unreachableNodes: string[] = data?.unreachable_nodes ?? [];

  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const resetPage = () => setCurrentPage(1);

  const filters = useTableFilters(SNAPSHOTS, snapshots, {
    onFilterChange: resetPage,
    availableFields: SNAPSHOTS.facetFields,
  });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    SNAPSHOTS.sortAccessors,
    SNAPSHOTS.defaultSort.field,
    SNAPSHOTS.defaultSort.direction,
    resetPage,
  );

  const sorted = useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);
  const totalPages = Math.ceil(sorted.length / pageSize);
  const paginated = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold">Snapshots</h1>
          <p className="text-muted-foreground mt-2">
            Restore points for maintenance. Snapshots this console creates are named{" "}
            <span className="font-mono">ops-</span> and expire on their own.
          </p>
        </div>
        <RefreshButton
          label="Refresh snapshots"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <SnapshotScopePicker
        host={host}
        project={project}
        onHostChange={(next) => {
          setHost(next);
          setProject("default");
          resetPage();
        }}
        onProjectChange={(next) => {
          setProject(next);
          resetPage();
        }}
        nodeOptions={nodePicker.options}
        nodesLoading={nodePicker.isLoading}
        nodesError={nodePicker.isError}
        projects={projectsQuery.data?.projects ?? []}
        projectsLoading={projectsQuery.isLoading}
      />

      <UnreachableNodesBanner noun="snapshots" nodes={unreachableNodes} />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>
            {host ? `${hostLabel} / ${project}` : "No Cluster Selected"}
          </CardTitle>
          {host && <TableToolbar filters={filters} noun="snapshots" />}
        </CardHeader>
        <CardContent>
          {!host ? (
            <p className="text-sm text-muted-foreground py-8 text-center">
              Choose a cluster to list its snapshots.
            </p>
          ) : isLoading ? (
            <LoadingLottiePage />
          ) : isError ? (
            <p className="text-sm text-destructive py-8 text-center">
              {error instanceof Error ? error.message : "Could not read snapshots."}
            </p>
          ) : (
            <>
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="Snapshot" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Instance" field="source_name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Size" field="size" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Created" field="created_at" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Expires" field="expires_at" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <TableCell className="w-12" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paginated.length === 0 ? (
                    <TableEmptyRow
                      colSpan={6}
                      icon={History}
                      title="No snapshots here"
                      description={
                        filters.activeFilterCount > 0
                          ? "No snapshot matches the current filters."
                          : "Take one from an instance's Snapshots tab before risky maintenance."
                      }
                    />
                  ) : (
                    paginated.map((snapshot) => (
                      <TableRow key={`${snapshot.source_name}/${snapshot.name}`}>
                        <TableCell className="font-medium">
                          <span className="font-mono">{snapshot.name}</span>
                          {!snapshot.operator_managed && (
                            <TableSubText>Managed elsewhere</TableSubText>
                          )}
                        </TableCell>
                        <TableCell>{snapshot.source_name}</TableCell>
                        <TableCell>
                          {typeof snapshot.size === "number" ? (
                            formatBytes(snapshot.size)
                          ) : (
                            <TableEmptyValue label="Not reported" />
                          )}
                        </TableCell>
                        <TableCell>
                          {snapshot.created_at ? (
                            relativeAge(snapshot.created_at)
                          ) : (
                            <TableEmptyValue label="Unknown" />
                          )}
                        </TableCell>
                        <TableCell>{expiryLabel(snapshot.expires_at)}</TableCell>
                        <TableCell>
                          <SnapshotRowActions
                            snapshot={snapshot}
                            host={host}
                            project={project}
                            billingLinked={snapshot.source_externally_managed ?? false}
                          />
                        </TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>

              {sorted.length > 0 && (
                <div className="mt-4">
                  <DataTablePagination
                    currentPage={currentPage}
                    totalPages={totalPages}
                    pageSize={pageSize}
                    totalItems={sorted.length}
                    onPageChange={setCurrentPage}
                    onPageSizeChange={(size) => {
                      setPageSize(size);
                      resetPage();
                    }}
                  />
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// Suspense is required because useTableFilters reads useSearchParams, which opts the component
// out of prerendering.
export default function SnapshotsPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <SnapshotsContent />
    </Suspense>
  );
}
