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
import { EMPTY_VALUE, TableEmptyRow } from "@/components/ui/table-text";
import { formatLabel } from "@/lib/utils";
import { HardDrive } from "lucide-react";
import { useStorageVolumes } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { useTableFilters } from "@/hooks/use-table-filters";
import { projectsFromUsedBy } from "@/lib/used-by";
import { defineTable, fromSort } from "@/lib/table-filter";
import { PageReadError } from "@/components/ui/read-error-state";
import { NodeLink } from "@/components/ui/node-link";

interface VolumeInfo {
  name: string;
  type: string;
  pool: string;
  content_type: string;
  description: string;
  config: Record<string, string>;
  used_by: string[];
  node?: string;
  /** The project that holds the volume. `used_by` answers who consumes it. */
  project?: string;
  /** The cluster member that holds it; empty on a shared pool or a single host. */
  location?: string;
}

const VOLUMES = defineTable<VolumeInfo>()({
  searchPlaceholder: "Search volumes",
  defaultSort: { field: "name", direction: "asc" },
  searchExtra: (v) => [v.description],
  columns: [
    { field: "name", label: "Name", sort: (v) => v.name, search: (v) => v.name, filter: fromSort("text") },
    {
      // On a cluster with a per-member pool the same name exists once per member, and a
      // custom volume may exist in several pools of one project: name alone is not an identity.
      field: "pool",
      label: "Pool",
      sort: (v) => v.pool,
      search: (v) => v.pool,
      facet: (v) => v.pool,
      facetSpec: {},
    },
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
      label: "Cluster",
      sort: (v) => v.node,
      facet: (v) => v.node,
      facetSpec: { role: "node", primary: true },
    },
    {
      field: "location",
      // note: LXD's volume.location is the member holding a local volume, empty when the pool
      // is shared. "Location" means geography everywhere else in the console.
      label: "Cluster Member",
      sort: (v) => v.location ?? "",
      facet: (v) => v.location ?? "",
      facetSpec: { emptyLabel: "(shared)" },
    },
    { field: "used_by", label: "Used By", sort: (v) => v.used_by?.length ?? 0, filter: fromSort("number", { unit: "count", presets: [{ label: "None", min: null, max: 0 }, { label: "Any", min: 1, max: null }] }) },
    {
      field: "project",
      label: "Project",
      // The project that HOLDS this volume, which is a different question from the
      // `projects` column below it: that one answers who consumes it.
      sort: (v) => v.project ?? "",
      facet: (v) => v.project ?? "",
      facetSpec: { role: "project", primary: true },
    },
    {
      // filter-coverage: this is not the project dimension. It answers which projects
      // CONSUME the volume, derived from used_by, while the "project" column above answers
      // which one holds it, and that is the column carrying the project role.
      field: "projects",
      label: "Used By Projects",
      // Was a copy of the used_by accessor, so the header sorted by resource count while the
      // cell displayed a project count. It now sorts by what it displays.
      sort: (v) => projectsFromUsedBy(v.used_by).length,
      facet: (v) => projectsFromUsedBy(v.used_by),
      facetSpec: { emptyLabel: "(no project)" },
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
  const { data, isLoading, refreshNow, dataUpdatedAt, isError, error, refetch } = useStorageVolumes();
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
  // A member column only once some volume names one: a shared pool and a single host never do.
  const locations = useMemo(
    () => Array.from(new Set(volumes.map((v) => v.location).filter(Boolean))).sort(),
    [volumes],
  );

  // The column appears once any node is known, the facet once there are two to choose
  // between. The thresholds differ on purpose: one option filters nothing.
  const availableFields = useMemo(
    () => VOLUMES.facetFields.filter((f) => (f !== "node" || nodes.length > 0) && (f !== "location" || locations.length > 0)),
    [nodes.length, locations.length],
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

  // Derived from the headers actually rendered: seven fixed columns (Name, Pool, Type, Content
  // Type, Used By, Project, Used By Projects) plus Node and Location when the scope has them. It
  // used to count eight, so the empty row was one column too wide on every scope that returns
  // nothing; the table-filters spec measures it against the header row.
  const columnCount = 7 + (nodes.length > 0 ? 1 : 0) + (locations.length > 0 ? 1 : 0);

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  // A failed read is not an empty list. Rendered before the table so the filtered-empty row
  // cannot blame a filter for a request that did not return.
  if (isError && !data) {
    return (
      <PageReadError
        title="Storage Volumes"
        description="Manage hypervisor storage volumes"
        noun="storage volumes"
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
                <SortableTableHead label="Pool" field="pool" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Content Type" field="content_type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && (
                  <SortableTableHead label="Cluster" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                )}
                {locations.length > 0 && (
                  <SortableTableHead label="Cluster Member" field="location" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                )}
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
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
                  <TableRow key={`${volume.node}-${volume.pool}-${volume.location}-${volume.name}-${idx}`}>
                    <TableCell className="font-medium">{volume.name}</TableCell>
                    <TableCell>{volume.pool}</TableCell>
                    <TableCell>
                      {formatLabel(volume.type)}
                    </TableCell>
                    <TableCell>
                      {formatLabel(volume.content_type)}
                    </TableCell>
                    {nodes.length > 0 && (
                      <TableCell><NodeLink clusterId={volume.node} /></TableCell>
                    )}
                    {locations.length > 0 && (
                      <TableCell>{volume.location || EMPTY_VALUE}</TableCell>
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
                    <TableCell>{volume.project || EMPTY_VALUE}</TableCell>
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
      { label: "Cluster", value: <NodeLink clusterId={volume.node} /> },
    ],
    panels: Object.keys(config).length
      ? [{ id: "config", label: "Configuration", kind: "config", entries: config }]
      : [],
  };
}
