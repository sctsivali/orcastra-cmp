"use client";

import type { StorageVolumeInfo } from "@/types/api";

import { Suspense, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner, reachabilityOf } from "@/components/ui/unreachable-nodes-banner";
import { RefreshButton } from "@/components/ui/refresh-button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TableToolbar } from "@/components/ui/table-toolbar";
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
import { HardDrive } from "lucide-react";
import { useStorageVolumes } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { useTableFilters } from "@/hooks/use-table-filters";
import { projectsFromUsedBy } from "@/lib/used-by";
import { defineTable, fromSort } from "@/lib/table-filter";

interface VolumeInfo {
  name: string;
  type: string;
  content_type: string;
  description: string;
  config: Record<string, string>;
  used_by: string[];
  node?: string;
}

const VOLUMES = defineTable<VolumeInfo>()({
  searchPlaceholder: "Search volumes",
  defaultSort: { field: "name", direction: "asc" },
  searchExtra: (v) => [v.description],
  columns: [
    { field: "name", label: "Name", sort: (v) => v.name, search: (v) => v.name, filter: fromSort("text") },
    {
      field: "type",
      label: "Type",
      sort: (v) => v.type,
      facet: (v) => v.type,
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      field: "content_type",
      label: "Content Type",
      sort: (v) => v.content_type,
      facet: (v) => v.content_type,
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      field: "node",
      label: "Node",
      sort: (v) => v.node,
      facet: (v) => v.node,
      facetSpec: { role: "node", primary: true },
    },
    { field: "used_by", label: "Used By", sort: (v) => v.used_by?.length ?? 0, filter: fromSort("number", { unit: "count", presets: [{ label: "None", min: null, max: 0 }, { label: "Any", min: 1, max: null }] }) },
    {
      field: "projects",
      label: "Projects",
      // Was a copy of the used_by accessor, so the header sorted by resource count while the
      // cell displayed a project count. It now sorts by what it displays.
      sort: (v) => projectsFromUsedBy(v.used_by).length,
      facet: (v) => projectsFromUsedBy(v.used_by),
      facetSpec: { role: "project", emptyLabel: "(no project)" },
    },
    {
      field: "usage",
      label: "Usage",
      facet: (v) => (v.used_by?.length ? "In use" : "Unused"),
      facetSpec: {},
    },
  ],
});

function StorageVolumesContent() {
  const { data, isLoading, refreshNow, dataUpdatedAt } = useStorageVolumes();
  const volumes: VolumeInfo[] = useMemo(() => (data as any)?.volumes || [], [data]);

  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const reach = reachabilityOf(data);

  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const resetPage = () => setCurrentPage(1);

  // note: derived from the rows, not from the fleet. It is empty exactly when the scope
  // returns nothing, which is the state that used to leave the empty row one column wide.
  const nodes = useMemo(
    () => Array.from(new Set(volumes.map((v) => v.node).filter(Boolean))).sort(),
    [volumes],
  );

  // The column appears once any node is known, the facet once there are two to choose
  // between. The thresholds differ on purpose: one option filters nothing.
  const availableFields = useMemo(
    () => (nodes.length > 0 ? VOLUMES.facetFields : VOLUMES.facetFields.filter((f) => f !== "node")),
    [nodes.length],
  );

  // One dialog for the page, opened from either cell. See components/used-by.
  const usedBy = useUsedByDialog();

  const filters = useTableFilters(VOLUMES, volumes, {
    onFilterChange: resetPage,
    availableFields,
  });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    VOLUMES.sortAccessors,
    VOLUMES.defaultSort.field,
    VOLUMES.defaultSort.direction,
    resetPage,
  );

  const sortedVolumes = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  const totalPages = Math.ceil(sortedVolumes.length / pageSize);
  const paginatedVolumes = sortedVolumes.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  // Derived from the headers actually rendered. It used to be the with-Node count, so the
  // empty row was one column too wide on every scope that returns nothing.
  const columnCount = nodes.length > 0 ? 6 : 5;

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Storage Volumes
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor storage volumes
          </p>
        </div>
        <RefreshButton
          label="Refresh storage volumes"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner noun="storage volumes" nodes={reach.unreachable} reasons={reach.reasons} />
      <UnreachableNodesBanner noun="storage volumes" nodes={reach.partial} reasons={reach.reasons} partial />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>All Storage Volumes</CardTitle>
          <TableToolbar filters={filters} noun="volumes" />
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Content Type" field="content_type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && (
                  <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                )}
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedVolumes.length === 0 ? (
                <TableEmptyRow
                  colSpan={columnCount}
                  icon={HardDrive}
                  title="No storage volumes found"
                  description={
                    filters.activeFilterCount > 0
                      ? "No volume matches the current filters."
                      : undefined
                  }
                />
              ) : (
                paginatedVolumes.map((volume: any, idx: number) => (
                  <TableRow key={`${volume.node}-${volume.name}-${idx}`}>
                    <TableCell className="font-medium">{volume.name}</TableCell>
                    <TableCell>
                      {formatLabel(volume.type)}
                    </TableCell>
                    <TableCell>
                      {formatLabel(volume.content_type)}
                    </TableCell>
                    {nodes.length > 0 && (
                      <TableCell>
                        {volume.node}
                      </TableCell>
                    )}
                    <TableCell className="text-center">
                      <UsedByCell
                        usedBy={volume.used_by}
                        noun="resources"
                        subjectName={volume.name}
                        emptyLabel="Not used"
                        onOpen={() => usedBy.openUsage(volumeSubject(volume))}
                      />
                    </TableCell>
                    <TableCell>
                      <ProjectsCell
                        usedBy={volume.used_by}
                        subjectName={volume.name}
                        onOpen={() => usedBy.openProjects(volumeSubject(volume))}
                      />
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
                totalItems={sortedVolumes.length}
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
export default function StorageVolumesPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <StorageVolumesContent />
    </Suspense>
  );
}

// note: `pool` is on every volume and is not a column in this table, so the subject strip is
// where an operator finally learns which pool a volume lives in without leaving the page.
function volumeSubject(volume: StorageVolumeInfo): UsedBySubject {
  const config = volume.config ?? {};
  return {
    kind: "volumes",
    name: volume.name,
    node: volume.node,
    description: volume.description || undefined,
    usedBy: volume.used_by ?? [],
    facts: [
      { label: "Pool", value: volume.pool },
      { label: "Type", value: formatLabel(volume.type) },
      { label: "Content Type", value: volume.content_type },
      { label: "Node", value: volume.node },
    ],
    panels: Object.keys(config).length
      ? [{ id: "config", label: "Configuration", kind: "config", entries: config }]
      : [],
  };
}
