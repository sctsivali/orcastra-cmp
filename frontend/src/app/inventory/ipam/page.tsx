"use client";

import { Suspense, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner, reachabilityOf } from "@/components/ui/unreachable-nodes-banner";
import { RefreshButton } from "@/components/ui/refresh-button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import {
  Table,
  TableBody,
  TableCell,
  
  TableHeader,
  TableRow } from "@/components/ui/table";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyRow, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { formatLabel } from "@/lib/utils";
import { useIPAM } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import {
  ALLOCATION_ORDER,
  UNKNOWN_ALLOCATION_REASON,
  allocationLabel,
  subnetText,
} from "@/components/ipam/allocation-labels";
import type { IPAMAllocation } from "@/types/api";
import { inventoryLinks } from "@/lib/inventory-links";
import { PageReadError } from "@/components/ui/read-error-state";
import { NodeLink } from "@/components/ui/node-link";

/**
 * The IPAM table: what is sortable, searchable and filterable.
 *
 * Allocation and network type are the two resting filters, because reconciling against an
 * IPAM catalogue starts with "show me the pinned addresses" and "show me the OVN ones". The
 * LXD row type and the network fall behind "Add filter" (declaration order decides).
 */
const IPAM = defineTable<IPAMAllocation>()({
  searchPlaceholder: "Search IP allocations",
  defaultSort: { field: "address", direction: "asc" },
  searchExtra: (a) => [allocationLabel(a.allocation), formatLabel(a.network_type || ""), a.network],
  columns: [
    { field: "address", label: "IP Address", sort: (a) => a.address, search: (a) => a.address, filter: fromSort("text") },
    {
      field: "allocation",
      label: "Allocation",
      sort: (a) => ALLOCATION_ORDER.indexOf(a.allocation ?? "unknown"),
      facet: (a) => a.allocation ?? "unknown",
      facetSpec: { primary: true, format: allocationLabel, order: ALLOCATION_ORDER },
    },
    {
      field: "type",
      label: "Type",
      sort: (a) => a.type,
      facet: (a) => a.type,
      facetSpec: { format: formatLabel },
    },
    {
      field: "network",
      label: "Network",
      sort: (a) => a.network,
      facet: (a) => a.network,
      facetSpec: {},
    },
    {
      field: "network_type",
      label: "Network Type",
      sort: (a) => a.network_type || "",
      facet: (a) => a.network_type || "",
      facetSpec: { primary: true, format: formatLabel, emptyLabel: "Not found" },
    },
    {
      field: "subnet",
      label: "Subnet",
      sort: (a) => a.subnet_ipv4 || a.subnet_ipv6 || "",
      search: (a) => [a.subnet_ipv4, a.subnet_ipv6].filter(Boolean).join(" "),
      filter: fromSort("text"),
    },
    { field: "used_by", label: "Used By", sort: (a) => a.used_by, search: (a) => a.used_by, filter: fromSort("text") },
    {
      field: "hardware_address",
      label: "MAC Address",
      sort: (a) => a.hardware_address,
      search: (a) => a.hardware_address,
      filter: fromSort("text"),
    },
    {
      field: "node",
      label: "Cluster",
      sort: (a) => a.node,
      facet: (a) => a.node,
      facetSpec: { role: "node", primary: true },
    },
    {
      field: "project",
      label: "Project",
      sort: (a) => a.project || "default",
      facet: (a) => a.project || "default",
      facetSpec: { role: "project" },
    },
  ],
});

function IPAMContent() {
  const { data, isLoading, refreshNow, dataUpdatedAt, isError, error, refetch } = useIPAM();
  const allocations: IPAMAllocation[] = data?.allocations ?? [];

  // Nodes the fan-out could not reach, and nodes that answered but whose instances or
  // networks could not be read (their rows say Unknown). Say so rather than let either read
  // as an empty or a complete list.
  const reach = reachabilityOf(data);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  const resetPage = () => setCurrentPage(1);
  const filters = useTableFilters(IPAM, allocations, { onFilterChange: resetPage });

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

  // A failed read is not an empty list. Rendered before the table so the filtered-empty row
  // cannot blame a filter for a request that did not return.
  if (isError && !data) {
    return (
      <PageReadError
        title="IPAM"
        description="Every address LXD has handed out or reserved, with whether it was pinned on the device, for reconciling against your IPAM catalogue."
        noun="IP allocations"
        error={error}
        onRetry={() => void refetch()}
      />
    );
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            IPAM
          </h1>
          <p className="text-muted-foreground mt-2">
            Every address LXD has handed out or reserved, with whether it was pinned on the
            device, for reconciling against your IPAM catalogue.
          </p>
        </div>
        <RefreshButton
          label="Refresh IP allocations"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner noun="IP allocations" nodes={reach.unreachable} reasons={reach.reasons} />
      <UnreachableNodesBanner noun="IP allocations" nodes={reach.partial} reasons={reach.reasons} partial />

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
                <SortableTableHead label="Allocation" field="allocation" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Network" field="network" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Subnet" field="subnet" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="MAC Address" field="hardware_address" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Cluster" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedAllocations.length === 0 ? (
                <TableEmptyRow colSpan={9} title="No IP allocations found" />
              ) : (
                paginatedAllocations.map((alloc, idx: number) => (
                  <TableRow key={`${alloc.node}-${alloc.project}-${alloc.address}-${idx}`}>
                    <TableCell className="font-mono">
                      {alloc.address}
                    </TableCell>
                    <TableCell>
                      {/* plain text, as the instances list prints Static / DHCP under an address */}
                      {alloc.allocation === "unknown" ? (
                        <span title={UNKNOWN_ALLOCATION_REASON}>{allocationLabel(alloc.allocation)}</span>
                      ) : (
                        allocationLabel(alloc.allocation)
                      )}
                    </TableCell>
                    <TableCell>
                      {formatLabel(alloc.type)}
                    </TableCell>
                    <TableCell>
                      {alloc.network ? (
                        <>
                          <TableLink
                            href={inventoryLinks.networkDetail(alloc.network, alloc.node, alloc.network_project || alloc.project)}
                            title={`Open the detail page for ${alloc.network}`}
                          >
                            {alloc.network}
                          </TableLink>
                          {alloc.network_type ? (
                            <TableSubText>{formatLabel(alloc.network_type)}</TableSubText>
                          ) : null}
                        </>
                      ) : (
                        <TableEmptyValue label="No network" />
                      )}
                    </TableCell>
                    <TableCell>
                      {(() => {
                        const subnet = subnetText(alloc);
                        return subnet.primary ? (
                          <>
                            <span className="font-mono">{subnet.primary}</span>
                            {subnet.secondary ? (
                              <TableSubText className="font-mono">{subnet.secondary}</TableSubText>
                            ) : null}
                          </>
                        ) : (
                          <TableEmptyValue label="No subnet" />
                        );
                      })()}
                    </TableCell>
                    <TableCell className="font-medium">
                      {alloc.type === "instance" && alloc.used_by ? (
                        <TableLink
                          href={inventoryLinks.instances({ q: alloc.used_by, project: alloc.project || "default", host: alloc.node })}
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
                    <TableCell><NodeLink clusterId={alloc.node} /></TableCell>
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
