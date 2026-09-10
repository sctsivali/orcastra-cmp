"use client";

import { Suspense, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
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
import { TableLink } from "@/components/ui/table-link";
import { OverflowText } from "@/components/ui/overflow-text";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EMPTY_VALUE, TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { Layers } from "lucide-react";
import { ProfileInfo } from "@/types/api";
import { useProfiles } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { useTableFilters } from "@/hooks/use-table-filters";
import { projectsFromUsedBy } from "@/lib/used-by";
import { defineTable, fromSort } from "@/lib/table-filter";

const PROFILES = defineTable<ProfileInfo>()({
  searchPlaceholder: "Search profiles",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "name", label: "Name", sort: (p) => p.name, search: (p) => p.name, filter: fromSort("text") },
    {
      field: "description",
      label: "Description",
      sort: (p) => p.description,
      search: (p) => p.description, filter: fromSort("text") },
    {
      field: "node",
      label: "Node",
      sort: (p) => p.node,
      facet: (p) => p.node,
      facetSpec: { role: "node", primary: true },
    },
    { field: "devices", label: "Devices", sort: (p) => Object.keys(p.devices || {}).length, filter: fromSort("number", { unit: "count" }) },
    { field: "used_by", label: "Used By", sort: (p) => p.used_by?.length ?? 0, filter: fromSort("number", { unit: "count", presets: [{ label: "None", min: null, max: 0 }, { label: "Any", min: 1, max: null }] }) },
    {
      field: "project",
      label: "Project",
      // The project that HOLDS this record, which is a different question from the
      // `projects` column below it: that one answers who consumes it.
      sort: (p) => p.project ?? "",
      facet: (p) => p.project ?? "",
      facetSpec: { role: "project", primary: true },
    },
    {
      // filter-coverage: this is not the project dimension. It answers which projects
      // CONSUME the record, derived from used_by, while the "project" column above answers
      // which one holds it, and that is the column carrying the project role.
      field: "projects",
      label: "Used By Projects",
      // Was a copy of the used_by accessor, so the header sorted by instance count while the
      // cell displayed a project count. It now sorts by what it displays.
      sort: (p) => projectsFromUsedBy(p.used_by).length,
      facet: (p) => projectsFromUsedBy(p.used_by),
      facetSpec: { primary: true, emptyLabel: "(no project)" },
    },
    {
      field: "usage",
      label: "Usage",
      facet: (p) => (p.used_by?.length ? "In use" : "Unused"),
      facetSpec: {},
    },
  ],
});

function ProfilesContent() {
  const { data, isLoading, refreshNow, dataUpdatedAt } = useProfiles();
  const profiles: ProfileInfo[] = useMemo(() => (data as any)?.profiles || [], [data]);

  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];

  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const resetPage = () => setCurrentPage(1);


  // note: derived from the rows, not from the fleet, matching the five sibling used_by pages.
  // A pinned role is exempt from the two-value threshold, so without this gate the Node control
  // renders on a scope with no node at all and offers nothing but the empty bucket.
  const nodes = useMemo(
    () => Array.from(new Set(profiles.map((p: any) => p.node).filter(Boolean))).sort(),
    [profiles],
  );

  // Header, cell and the empty row's width move together; a mismatch renders a short row.
  const columnCount = nodes.length > 0 ? 7 : 6;

  const availableFields = useMemo(
    () =>
      nodes.length > 0
        ? PROFILES.facetFields
        : PROFILES.facetFields.filter((f) => f !== "node"),
    [nodes.length],
  );

  // One dialog for the page, opened from the devices, used-by and projects cells alike.
  const usedBy = useUsedByDialog();

  const filters = useTableFilters(PROFILES, profiles, {
    onFilterChange: resetPage,
    availableFields,
  });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    PROFILES.sortAccessors,
    PROFILES.defaultSort.field,
    PROFILES.defaultSort.direction,
    resetPage,
  );

  const sortedProfiles = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  const totalPages = Math.ceil(sortedProfiles.length / pageSize);
  const paginatedProfiles = sortedProfiles.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Profiles
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor configuration profiles
          </p>
        </div>
        <RefreshButton
          label="Refresh profiles"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner noun="profiles" nodes={unreachableNodes} />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>All Profiles</CardTitle>
          <TableToolbar filters={filters} noun="profiles" />
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Description" field="description" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && (
                  <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                )}
                <SortableTableHead label="Devices" field="devices" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedProfiles.length === 0 ? (
                <TableEmptyRow
                  colSpan={columnCount}
                  icon={Layers}
                  title="No profiles found"
                  description={
                    filters.activeFilterCount > 0
                      ? "No profile matches the current filters."
                      : undefined
                  }
                />
              ) : (
                paginatedProfiles.map((profile: any) => (
                  <TableRow key={`${profile.node}-${profile.name}`}>
                    <TableCell className="font-medium">{profile.name}</TableCell>
                    <TableCell className="max-w-xs">
                      {profile.description ? (
                        <OverflowText
                          value={profile.description}
                          label={`description for ${profile.name}`}
                          lines={2}
                        />
                      ) : (
                        <TableEmptyValue label="No description" />
                      )}
                    </TableCell>
                    {nodes.length > 0 && (
                      <TableCell>
                        {profile.node}
                      </TableCell>
                    )}
                    <TableCell>
                      {Object.keys(profile.devices || {}).length > 0 ? (
                        <TableLink
                          onClick={() => usedBy.openPanel(profileSubject(profile), "devices")}
                          aria-label={`Show the devices in ${profile.name}`}
                        >
                          {`${Object.keys(profile.devices).length} devices`}
                        </TableLink>
                      ) : (
                        <TableEmptyValue label="No devices" />
                      )}
                    </TableCell>
                    <TableCell>
                      <UsedByCell
                        usedBy={profile.used_by}
                        noun="instances"
                        subjectName={profile.name}
                        emptyLabel="Not used"
                        onOpen={() => usedBy.openUsage(profileSubject(profile))}
                      />
                    </TableCell>
                    <TableCell>{profile.project || EMPTY_VALUE}</TableCell>
                    <TableCell>
                      <ProjectsCell
                        usedBy={profile.used_by}
                        subjectName={profile.name}
                        onOpen={() => usedBy.openProjects(profileSubject(profile))}
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
                totalItems={sortedProfiles.length}
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
export default function ProfilesPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <ProfilesContent />
    </Suspense>
  );
}

// note: built on click, for the one row the operator chose. profile.config is rendered here for
// the first time anywhere in the app: limits.cpu, limits.memory, security.* and cloud-init.*
// have always been on the wire and never on screen.
function profileSubject(profile: ProfileInfo): UsedBySubject {
  const devices = profile.devices ?? {};
  const config = profile.config ?? {};
  return {
    kind: "profiles",
    name: profile.name,
    node: profile.node,
    description: profile.description || undefined,
    usedBy: profile.used_by ?? [],
    facts: [
      { label: "Node", value: profile.node },
      { label: "Devices", value: Object.keys(devices).length },
      { label: "Config Keys", value: Object.keys(config).length },
    ],
    panels: [
      { id: "devices", label: "Devices", kind: "devices", devices },
      { id: "config", label: "Configuration", kind: "config", entries: config },
    ],
  };
}
