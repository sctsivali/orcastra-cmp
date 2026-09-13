"use client";

import { Suspense, useRef, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner, reachabilityOf } from "@/components/ui/unreachable-nodes-banner";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Button } from "@/components/ui/button";
import { RowActions } from "@/components/ui/row-actions";
import { ProfileFormDialog } from "@/components/profiles/profile-form-dialog";
import { ProfileDeleteDialog } from "@/components/profiles/profile-delete-dialog";
import { profileActions } from "@/components/profiles/profile-row-actions";
import { usePermissions } from "@/hooks/use-permissions";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useToast } from "@/hooks/use-toast";
import { Role } from "@/types/rbac";
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
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EMPTY_VALUE, TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { Layers, Plus } from "lucide-react";
import { ProfileInfo } from "@/types/api";
import { useDeleteProfile, useProfiles } from "@/hooks/use-lxd";
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
  const { toast } = useToast();
  const { hasRole } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  // Partner and above may write; a Tenant sees no affordance and the backend refuses anyway.
  const canManage = hasRole(Role.PARTNER);
  const deleteProfile = useDeleteProfile();

  // Nodes the fan-out could not reach, and nodes that answered but lost some rows.
  const reach = reachabilityOf(data);

  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const resetPage = () => setCurrentPage(1);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ProfileInfo | undefined>(undefined);
  const [deleteTarget, setDeleteTarget] = useState<ProfileInfo | null>(null);
  // held one render longer than deleteTarget so the dialog body does not blank while it fades
  const [deleteDisplay, setDeleteDisplay] = useState<ProfileInfo | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  // the confirm is a click handler, so `deleting` alone cannot stop two clicks in one tick
  const deleteInFlightRef = useRef(false);

  const openCreate = () => {
    setEditing(undefined);
    setDialogOpen(true);
  };
  const openEdit = (profile: ProfileInfo) => {
    setEditing(profile);
    setDialogOpen(true);
  };
  const openDelete = (profile: ProfileInfo) => {
    setDeleteError(null);
    setDeleteDisplay(profile);
    setDeleteTarget(profile);
  };
  const confirmDelete = async () => {
    if (!deleteTarget || deleteInFlightRef.current) return;
    deleteInFlightRef.current = true;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteProfile.mutateAsync({
        name: deleteTarget.name,
        host: deleteTarget.node,
        project: deleteTarget.project,
      });
      toast({ title: "Profile deleted", description: `"${deleteTarget.name}" on ${deleteTarget.node}.` });
      setDeleteTarget(null);
      refreshNow();
    } catch (e) {
      // the dialog stays open with the reason (a 409 names the instances still using it)
      setDeleteError(e instanceof Error ? e.message : "Failed to delete the profile");
    } finally {
      deleteInFlightRef.current = false;
      setDeleting(false);
    }
  };


  // note: derived from the rows, not from the fleet, matching the five sibling used_by pages.
  // A pinned role is exempt from the two-value threshold, so without this gate the Node control
  // renders on a scope with no node at all and offers nothing but the empty bucket.
  const nodes = useMemo(
    () => Array.from(new Set(profiles.map((p: any) => p.node).filter(Boolean))).sort(),
    [profiles],
  );

  // Header, cell and the empty row's width move together; a mismatch renders a short row.
  const columnCount = (nodes.length > 0 ? 7 : 6) + (canManage ? 1 : 0);

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
        <div className="flex items-center gap-2">
          <RefreshButton
            label="Refresh profiles"
            lastUpdatedAt={dataUpdatedAt}
            onRefresh={refreshNow}
          />
          {canManage && (
            <Button
              onClick={openCreate}
              disabled={isReadOnly}
              title={isReadOnly ? "Action disabled in read-only mode" : undefined}
            >
              <Plus className="h-4 w-4 mr-2" />
              Create Profile
            </Button>
          )}
        </div>
      </div>

      <UnreachableNodesBanner noun="profiles" nodes={reach.unreachable} reasons={reach.reasons} />
      <UnreachableNodesBanner noun="profiles" nodes={reach.partial} reasons={reach.reasons} partial />

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
                {canManage && <TableHead className="text-right">Actions</TableHead>}
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
                // key: the project is part of a profile's identity, not decoration. Every
                // LXD project carries its own `default` profile, so once this list spans
                // projects rather than one, node+name collides on 26 of the 36 rows and React
                // reuses the wrong row: sorting appended rows instead of reordering them,
                // 25 -> 47 -> 61 -> 83 over three presses of the Name header.
                paginatedProfiles.map((profile: any) => (
                  <TableRow key={`${profile.node}-${profile.project ?? ""}-${profile.name}`}>
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
                    {canManage && (
                      <TableCell className="text-right">
                        <RowActions
                          actions={profileActions({
                            profile,
                            isReadOnly,
                            onEdit: () => openEdit(profile),
                            onDelete: () => openDelete(profile),
                          })}
                          triggerLabel={`Actions for profile ${profile.name}`}
                        />
                      </TableCell>
                    )}
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
      {canManage && (
        <>
          <ProfileFormDialog
            open={dialogOpen}
            onOpenChange={setDialogOpen}
            existing={editing}
            onSaved={refreshNow}
          />
          <ProfileDeleteDialog
            open={deleteTarget !== null}
            onOpenChange={(next) => {
              if (!next && !deleting) setDeleteTarget(null);
            }}
            profile={deleteTarget ?? deleteDisplay}
            pending={deleting}
            error={deleteError}
            onConfirm={confirmDelete}
          />
        </>
      )}
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
