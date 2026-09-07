"use client";

import type { StoragePoolInfo } from "@/types/api";

import { Suspense, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Badge } from "@/components/ui/badge";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import {
  ProjectsCell,
  UsedByCell,
  UsedByDialog,
  useUsedByDialog,
  type UsedBySubject,
} from "@/components/used-by";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyRow } from "@/components/ui/table-text";
import { formatLabel } from "@/lib/utils";
import { Database } from "lucide-react";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { projectsFromUsedBy } from "@/lib/used-by";
import { defineTable, fromSort } from "@/lib/table-filter";
import { useStoragePools } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { PoolCapacityCell, poolCapacity, type PoolCapacity } from "./components/pool-capacity-cell";

interface PoolInfo {
  name: string;
  description: string;
  driver: string;
  config: Record<string, string>;
  used_by: string[];
  status: string;
  node?: string;
}

// note: /storage/pools and /storage/pools/resources-all are two calls merged by useStoragePools,
// so usage reaches the page on a blob keyed "{node}-{name}" rather than on the pool. A sort
// accessor is only ever handed the row (see TableColumn.sort in lib/table-filter.ts), so the
// reading is folded onto the row before the table is built rather than looked up per cell.
interface PoolRow extends PoolInfo {
  capacity: PoolCapacity;
}

const POOLS = defineTable<PoolRow>()({
  searchPlaceholder: "Search storage pools",
  defaultSort: { field: "name", direction: "asc" },
  searchExtra: (p) => [p.description],
  columns: [
    { field: "name", label: "Name", sort: (p) => p.name, search: (p) => p.name, filter: fromSort("text") },
    {
      field: "driver",
      label: "Driver",
      sort: (p) => p.driver,
      facet: (p) => p.driver,
      // note: storage drivers are LXD enums, so formatLabel gives zfs its acronym casing.
      // GPU drivers elsewhere are kernel module names and must not go through it.
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      field: "node",
      label: "Node",
      sort: (p) => p.node,
      facet: (p) => p.node,
      facetSpec: { role: "node", primary: true },
    },
    {
      field: "status",
      label: "Status",
      sort: (p) => p.status,
      facet: (p) => p.status,
      facetSpec: { primary: true },
    },
    { field: "used_by", label: "Used By", sort: (p) => p.used_by?.length ?? 0, filter: fromSort("number", { unit: "count", presets: [{ label: "None", min: null, max: 0 }, { label: "Any", min: 1, max: null }] }) },
    {
      field: "projects",
      label: "Projects",
      // Was a copy of the used_by accessor, so the header sorted by volume count while the
      // cell displayed a project count. It now sorts by what it displays.
      sort: (p) => projectsFromUsedBy(p.used_by).length,
      facet: (p) => projectsFromUsedBy(p.used_by),
      facetSpec: { role: "project", emptyLabel: "(no project)" },
    },
    {
      // The size column was rendered but never declared, so it was the one column on this page
      // that could be neither sorted nor filtered. It shows used of total with a bar, so a click
      // on the header orders by how full a pool is rather than by how large it is: "which one is
      // about to fill up" is the question the column exists to answer.
      field: "utilisation",
      // note: "Capacity Used", not "Utilisation", because this page already carries a "Usage"
      // facet meaning "has volumes attached". Two controls one letter apart in the same menu,
      // measuring different things, is a worse answer than a longer label.
      label: "Capacity Used",
      sort: (p) => p.capacity.percent,
      filter: fromSort("number", {
        unit: "percent",
        presets: [
          { label: "Over 75%", min: 75, max: null },
          { label: "Over 90%", min: 90, max: null },
        ],
      }),
    },
    {
      field: "usage",
      label: "Usage",
      facet: (p) => (p.used_by?.length ? "In use" : "Unused"),
      facetSpec: {},
    },
  ],
});

function StoragePoolsContent() {
  const { data, isLoading, refreshNow, dataUpdatedAt } = useStoragePools();
  const pools = (data as any)?.pools || [];
  const poolResources = (data as any)?.resources || {};
  
  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const resetPage = () => setCurrentPage(1);

  // note: derived from the rows, not from the fleet. It is empty exactly when the scope
  // returns nothing, which is the state that used to leave the empty row one column too wide.
  const nodes = useMemo(() => {
    const nodeSet = new Set(pools.map((p: any) => p.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [pools]);

  // The column appears once any node is known, the facet once there are two to choose
  // between. The thresholds differ on purpose: one option filters nothing.
  const availableFields = useMemo(
    () => (nodes.length > 0 ? POOLS.facetFields : POOLS.facetFields.filter((f) => f !== "node")),
    [nodes.length],
  );

  // One dialog for the page, opened from either cell. See components/used-by.
  const usedBy = useUsedByDialog();

  const rows: PoolRow[] = useMemo(
    () =>
      (pools as PoolInfo[]).map((pool) => ({
        ...pool,
        capacity: poolCapacity(poolResources[`${pool.node}-${pool.name}`]?.space),
      })),
    [pools, poolResources],
  );

  const filters = useTableFilters(POOLS, rows, {
    onFilterChange: resetPage,
    availableFields,
  });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    POOLS.sortAccessors,
    POOLS.defaultSort.field,
    POOLS.defaultSort.direction,
    resetPage,
  );

  const sortedPools = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  const totalPages = Math.ceil(sortedPools.length / pageSize);
  const paginatedPools = sortedPools.slice(
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
            Storage Pools
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor storage pools
          </p>
        </div>
        <RefreshButton
          label="Refresh storage pools"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner noun="storage pools" nodes={unreachableNodes} />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>All Storage Pools</CardTitle>
          <TableToolbar filters={filters} noun="storage pools" />
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Driver" field="driver" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && (
                  <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                )}
                <SortableTableHead label="Size (Used / Total)" field="utilisation" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedPools.length === 0 ? (
                <TableEmptyRow
                  colSpan={nodes.length > 0 ? 7 : 6}
                  icon={Database}
                  title="No storage pools found"
                  description={
                    filters.activeFilterCount > 0
                      ? "No storage pool matches the current filters."
                      : undefined
                  }
                />
              ) : (
                paginatedPools.map((pool: PoolRow, idx: number) => {
                  const poolKey = `${pool.node}-${pool.name}`;

                  return (
                    <TableRow key={`${poolKey}-${idx}`}>
                      <TableCell className="font-medium">{pool.name}</TableCell>
                      <TableCell>
                        {formatLabel(pool.driver)}
                      </TableCell>
                      {nodes.length > 0 && (
                        <TableCell>
                          {pool.node}
                        </TableCell>
                      )}
                      <TableCell>
                        <PoolCapacityCell
                          capacity={pool.capacity}
                          poolName={pool.name}
                          onOpen={() => usedBy.openPanel(poolSubject(pool, poolResources), "capacity")}
                        />
                      </TableCell>
                      <TableCell>
                        <Badge variant={pool.status === "Created" ? "default" : "secondary"}>
                          {pool.status}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <UsedByCell
                          usedBy={pool.used_by}
                          noun="volumes"
                          subjectName={pool.name}
                          emptyLabel="No volumes"
                          onOpen={() => usedBy.openUsage(poolSubject(pool, poolResources))}
                        />
                      </TableCell>
                      <TableCell>
                        <ProjectsCell
                          usedBy={pool.used_by}
                          subjectName={pool.name}
                          onOpen={() => usedBy.openProjects(poolSubject(pool, poolResources))}
                        />
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>

          {totalPages > 1 && (
            <div className="mt-4">
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                pageSize={pageSize}
                totalItems={sortedPools.length}
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

      <UsedByDialog {...usedBy.dialogProps} />
    </div>
  );
}

// Suspense is required because useTableFilters reads useSearchParams, which opts the
// component out of prerendering.
export default function StoragePoolsPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <StoragePoolsContent />
    </Suspense>
  );
}

// note: the capacity panel reads the resources blob the page already fetches from
// /storage/pools/resources-all. Only space.used and space.total were ever displayed, so the
// whole inodes dict was paid for on every page load and thrown away. A pool can sit at 20% on
// bytes and be completely full on inodes, and until now the UI could not say so.
function poolSubject(pool: StoragePoolInfo, resources: Record<string, any>): UsedBySubject {
  const config = pool.config ?? {};
  const usage = resources?.[`${pool.node}-${pool.name}`] ?? {};
  return {
    kind: "storage-pools",
    name: pool.name,
    node: pool.node,
    description: pool.description || undefined,
    usedBy: pool.used_by ?? [],
    facts: [
      { label: "Driver", value: formatLabel(pool.driver) },
      { label: "Status", value: pool.status },
      { label: "Node", value: pool.node },
    ],
    panels: [
      { id: "capacity", label: "Capacity", kind: "capacity", space: usage.space, inodes: usage.inodes },
      { id: "config", label: "Configuration", kind: "config", entries: config },
    ],
  };
}
