"use client";

import { Suspense, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { RefreshButton } from "@/components/ui/refresh-button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import {
  Table,
  TableBody,
  TableCell,
  
  TableHeader,
  TableRow } from "@/components/ui/table";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { formatLabel } from "@/lib/utils";
import { useIPAM } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";


const IPAM = defineTable<any>()({
  searchPlaceholder: "Search IP allocations",
  defaultSort: { field: "address", direction: "asc" },
  columns: [
    { field: "address", label: "IP Address", sort: (a) => a.address, search: (a) => a.address, filter: fromSort("text") },
    {
      field: "type",
      label: "Type",
      sort: (a) => a.type,
      facet: (a) => a.type,
      facetSpec: { primary: true, format: formatLabel } },
    { field: "used_by", label: "Used By", sort: (a) => a.used_by, search: (a) => a.used_by, filter: fromSort("text") },
    {
      field: "hardware_address",
      label: "MAC Address",
      sort: (a) => a.hardware_address,
      search: (a) => a.hardware_address, filter: fromSort("text") },
    {
      field: "network",
      label: "Network",
      sort: (a) => a.network,
      facet: (a) => a.network,
      facetSpec: { primary: true } },
    {
      field: "node",
      label: "Node",
      sort: (a) => a.node,
      facet: (a) => a.node,
      facetSpec: { role: "node", primary: true } },
    {
      field: "project",
      label: "Project",
      // The column was rendered and sortable with no way to filter by it, on a page that
      // already offered three other dropdowns.
      sort: (a) => a.project || "default",
      facet: (a) => a.project || "default",
      facetSpec: { role: "project",} },
  ] });

function IPAMContent() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useIPAM();
  const allocations = (data as any)?.allocations || [];
  
  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  const resetPage = () => setCurrentPage(1);
  const filters = useTableFilters(IPAM, allocations as any[], { onFilterChange: resetPage });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    IPAM.sortAccessors,
    IPAM.defaultSort.field,
    IPAM.defaultSort.direction,
    resetPage,
  );



  const sortedAllocations = useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);

  const totalPages = Math.ceil(sortedAllocations.length / pageSize);
  const paginatedAllocations = sortedAllocations.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize
  );

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            IPAM
          </h1>
          <p className="text-muted-foreground mt-2">
            IP Address Management - View and manage IP allocations across networks
          </p>
        </div>
        <RefreshButton
          label="Refresh IP allocations"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner noun="IP allocations" nodes={unreachableNodes} />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>IP Address Allocations</CardTitle>
          <TableToolbar filters={filters} noun="IP allocations" />
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="IP Address" field="address" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="MAC Address" field="hardware_address" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Network" field="network" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedAllocations.length === 0 ? (
                <TableEmptyRow colSpan={7} title="No IP allocations found" />
              ) : (
                paginatedAllocations.map((alloc: any, idx: number) => (
                  <TableRow key={`${alloc.node}-${alloc.address}-${idx}`}>
                    <TableCell className="font-mono">
                      {alloc.address}
                    </TableCell>
                    <TableCell>
                      {formatLabel(alloc.type)}
                    </TableCell>
                    <TableCell className="font-medium">
                      {alloc.type === "instance" && alloc.used_by ? (
                        <TableLink
                          href={`/inventory/instances?q=${encodeURIComponent(alloc.used_by)}&project=${encodeURIComponent(alloc.project || "default")}&host=${encodeURIComponent(alloc.node)}`}
                          title={`Open ${alloc.used_by} in Instances`}
                        >
                          {alloc.used_by}
                        </TableLink>
                      ) : (
                        alloc.used_by || <TableEmptyValue label="Not used" />
                      )}
                    </TableCell>
                    <TableCell>
                      {/* note: font-mono, not <code>. One mono idiom in a table cell. */}
                      {alloc.hardware_address ? (
                        <span className="font-mono">{alloc.hardware_address}</span>
                      ) : (
                        <TableEmptyValue label="No MAC address" />
                      )}
                    </TableCell>
                    <TableCell>
                      {alloc.network ? (
                        <TableLink
                          href={`/inventory/networks/${encodeURIComponent(alloc.network)}?host=${encodeURIComponent(alloc.node)}`}
                          title={`Open the detail page for ${alloc.network}`}
                        >
                          {alloc.network}
                        </TableLink>
                      ) : (
                        <TableEmptyValue label="No network" />
                      )}
                    </TableCell>
                    <TableCell>
                      {alloc.node}
                    </TableCell>
                    <TableCell>
                      {alloc.project || 'default'}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>

          {totalPages > 1 && (
            <div className="mt-4">
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                pageSize={pageSize}
                totalItems={sortedAllocations.length}
                onPageChange={setCurrentPage}
                onPageSizeChange={(size) => {
                  setPageSize(size);
                  setCurrentPage(1);
                }}
              />
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// Suspense is required because useTableFilters reads useSearchParams, which opts the
// component out of prerendering.
export default function IPAMPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <IPAMContent />
    </Suspense>
  );
}
