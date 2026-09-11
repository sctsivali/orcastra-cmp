"use client";

import type { StorageVolumeInfo } from "@/types/api";

import { Suspense, useMemo, useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import {
  Table,
  TableBody,
  TableCell,
  
  TableHeader,
  TableRow } from "@/components/ui/table";
import { EMPTY_VALUE, TableEmptyRow } from "@/components/ui/table-text";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { IsoUploadDialog } from "@/components/iso-upload-dialog";
import { useStorageVolumes } from "@/hooks/use-lxd";
import { UnreachableNodesBanner, reachabilityOf } from "@/components/ui/unreachable-nodes-banner";
import { useTableSort } from "@/hooks/use-table-sort";
import { usePermissions } from "@/hooks/use-permissions";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { Role } from "@/types/rbac";
import { UploadCloud, Disc } from "lucide-react";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { projectsFromUsedBy } from "@/lib/used-by";
import {
  ProjectsCell,
  UsedByCell,
  UsedByDialog,
  useUsedByDialog,
  type UsedBySubject,
} from "@/components/used-by";

const ISOS = defineTable<any>()({
  searchPlaceholder: "Search ISO images",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "name", label: "Name", sort: (i) => i.name, search: (i) => i.name, filter: fromSort("text") },
    {
      field: "pool",
      label: "Pool",
      sort: (i) => i.pool,
      search: (i) => i.pool,
      facet: (i) => i.pool,
      facetSpec: { primary: true } },
    {
      field: "node",
      label: "Node",
      sort: (i) => i.node,
      search: (i) => i.node,
      facet: (i) => i.node,
      facetSpec: { role: "node", primary: true } },
    { field: "used_by", label: "Used By", sort: (i) => i.used_by?.length ?? 0, filter: fromSort("number", { unit: "count", presets: [{ label: "None", min: null, max: 0 }, { label: "Any", min: 1, max: null }] }) },
    {
      field: "project",
      label: "Project",
      // The project that HOLDS the image; `projects` below answers who consumes it.
      sort: (i) => i.project ?? "",
      facet: (i) => i.project ?? "",
      facetSpec: { role: "project", primary: true },
    },
    {
      // filter-coverage: this is not the project dimension. It answers which projects
      // CONSUME the image, derived from used_by, while the "project" column above answers
      // which one holds it, and that is the column carrying the project role.
      field: "projects",
      label: "Used By Projects",
      // The one page in the used_by family that never had this column, though the rows have
      // always carried used_by. Same shape as storage-volumes.
      sort: (i) => projectsFromUsedBy(i.used_by).length,
      facet: (i) => projectsFromUsedBy(i.used_by),
      facetSpec: { emptyLabel: "(no project)" },
    },
    {
      field: "usage",
      label: "Usage",
      facet: (i) => (i.used_by?.length ? "In use" : "Unused"),
      facetSpec: {},
    },
  ] });

function IsoImagesContent() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useStorageVolumes();
  const allVolumes = (data as any)?.volumes || [];
  // The ISO list is the volume list filtered, so a cluster the volume read lost is a
  // cluster whose ISOs are missing here too. Without this banner the page said "No ISO
  // images yet" for a cluster holding two.
  const reach = reachabilityOf(data);
  // ISOs are custom storage volumes with content-type "iso".
  const isos = useMemo(
    () => allVolumes.filter((v: any) => v.content_type === "iso"),
    [allVolumes],
  );

  const { hasRole } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  const canUpload = hasRole(Role.PARTNER);

  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [uploadOpen, setUploadOpen] = useState(false);

  const resetPage = () => setCurrentPage(1);

  // note: derived from the rows, not from the fleet, matching the five sibling used_by pages.
  // A pinned role is exempt from the two-value threshold, so without this gate the Node control
  // renders on a scope with no node at all and offers nothing but the empty bucket.
  const nodes = useMemo(
    () => Array.from(new Set(isos.map((i: any) => i.node).filter(Boolean))).sort(),
    [isos],
  );

  // Header, cell and the empty row's width move together; a mismatch renders a short row.
  const columnCount = nodes.length > 0 ? 6 : 5;

  const availableFields = useMemo(
    () =>
      nodes.length > 0
        ? ISOS.facetFields
        : ISOS.facetFields.filter((f) => f !== "node"),
    [nodes.length],
  );

  // One dialog for the page, opened from either cell. See components/used-by.
  const usedBy = useUsedByDialog();

  const filters = useTableFilters(ISOS, isos as any[], {
    onFilterChange: resetPage,
    availableFields,
  });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    ISOS.sortAccessors,
    ISOS.defaultSort.field,
    ISOS.defaultSort.direction,
    resetPage,
  );

  const sorted = useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);
  const totalPages = Math.ceil(sorted.length / pageSize);
  const paginated = sorted.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  if (isLoading) return <LoadingLottiePage />;

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">ISO Images</h1>
          <p className="text-muted-foreground mt-2">
            Custom ISO storage volumes that can be attached to VMs as boot media
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RefreshButton
            label="Refresh ISO images"
            lastUpdatedAt={dataUpdatedAt}
            onRefresh={refreshNow}
          />
          {/* Partner+ only; the upload endpoint requires Partner. */}
          {canUpload && (
            <Button
              onClick={() => setUploadOpen(true)}
              disabled={isReadOnly}
              title={isReadOnly ? "Action disabled in read-only mode" : "Upload an ISO"}
            >
              <UploadCloud className="h-4 w-4 mr-2" />
              Upload ISO
            </Button>
          )}
        </div>
      </div>

      <UnreachableNodesBanner noun="ISO images" nodes={reach.unreachable} reasons={reach.reasons} />
      <UnreachableNodesBanner noun="ISO images" nodes={reach.partial} reasons={reach.reasons} partial />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>ISO Images</CardTitle>
          <TableToolbar filters={filters} noun="ISO images" />
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Pool" field="pool" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && (
                  <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                )}
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginated.length === 0 ? (
                <TableEmptyRow
                  colSpan={columnCount}
                  icon={Disc}
                  title="No ISO images yet"
                  description={canUpload ? 'Use "Upload ISO" to add one.' : undefined}
                />
              ) : (
                paginated.map((v: any, idx: number) => (
                  <TableRow key={`${v.node}-${v.pool}-${v.name}-${idx}`}>
                    <TableCell className="font-medium">{v.name}</TableCell>
                    <TableCell>
                      {v.pool}
                    </TableCell>
                    {nodes.length > 0 && (
                      <TableCell>
                        {v.node}
                      </TableCell>
                    )}
                    <TableCell>
                      <UsedByCell
                        usedBy={v.used_by}
                        noun="instances"
                        subjectName={v.name}
                        emptyLabel="Not used"
                        onOpen={() => usedBy.openUsage(isoSubject(v))}
                      />
                    </TableCell>
                    <TableCell>{v.project || EMPTY_VALUE}</TableCell>
                    <TableCell>
                      <ProjectsCell
                        usedBy={v.used_by}
                        subjectName={v.name}
                        onOpen={() => usedBy.openProjects(isoSubject(v))}
                      />
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
          <DataTablePagination
            currentPage={currentPage}
            totalPages={totalPages}
            pageSize={pageSize}
            totalItems={sorted.length}
            onPageChange={setCurrentPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setCurrentPage(1);
            }}
          />
        </CardContent>
      </Card>

      <UsedByDialog {...usedBy.dialogProps} />

      <IsoUploadDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        onUploaded={() => refetch()}
      />
    </div>
  );
}

// Suspense is required because useTableFilters reads useSearchParams, which opts the
// component out of prerendering.
export default function IsoImagesPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <IsoImagesContent />
    </Suspense>
  );
}

// note: built on click for the row the operator chose, never during the table's render pass.
// An ISO volume carries no devices, rules or capacity of its own, so its only extra panel is
// its config, and when that is empty the dialog renders no tab strip at all.
function isoSubject(volume: StorageVolumeInfo): UsedBySubject {
  const config = volume.config ?? {};
  return {
    kind: "volumes",
    name: volume.name,
    node: volume.node,
    description: volume.description || undefined,
    usedBy: volume.used_by ?? [],
    facts: [
      { label: "Pool", value: volume.pool },
      { label: "Node", value: volume.node },
      { label: "Content Type", value: volume.content_type },
    ],
    panels: Object.keys(config).length
      ? [{ id: "config", label: "Configuration", kind: "config", entries: config }]
      : [],
  };
}
