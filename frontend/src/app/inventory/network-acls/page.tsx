"use client";

import type { NetworkACLInfo } from "@/types/api";

import { Suspense, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner, reachabilityOf } from "@/components/ui/unreachable-nodes-banner";
import { RefreshButton } from "@/components/ui/refresh-button";
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
  TableHead,
  TableRow } from "@/components/ui/table";
import { Pencil, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { EMPTY_VALUE, TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { OverflowText } from "@/components/ui/overflow-text";
import { TableLink } from "@/components/ui/table-link";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { projectsFromUsedBy } from "@/lib/used-by";
import { defineTable, fromSort } from "@/lib/table-filter";
import { useDeleteNetworkACL, useNetworkACLs } from "@/hooks/use-lxd";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { AclFormDialog } from "@/components/network-acls/acl-form-dialog";
import { usePermissions } from "@/hooks/use-permissions";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useToast } from "@/hooks/use-toast";
import { Role } from "@/types/rbac";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { PageReadError } from "@/components/ui/read-error-state";
import { useRowFocus } from "@/hooks/use-row-focus";
import { requestRowFocus } from "@/lib/row-focus";
import { RowFocusNotice } from "@/components/ui/row-focus-notice";
import { NodeLink } from "@/components/ui/node-link";
import { plural } from "@/lib/utils";
import { PoliciesIllustration } from "@/components/illustrations/scenes/onboarding";

// note: no local ACLInfo. It duplicated NetworkACLInfo and drifted from it, which is how a
// field the API had started sending stayed invisible to this page.
/** One key for a row and for the dialog's report of what it wrote. */
const aclRowKey = (a: { node?: string | null; project?: string | null; name: string }) =>
  `${a.node ?? ""}/${a.project || "default"}/${a.name}`;

const ACLS = defineTable<NetworkACLInfo>()({
  searchPlaceholder: "Search network ACLs",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "name", label: "Name", sort: (a) => a.name, search: (a) => a.name, filter: fromSort("text") },
    {
      field: "description",
      label: "Description",
      sort: (a) => a.description,
      search: (a) => a.description, filter: fromSort("text") },
    {
      field: "node",
      label: "Cluster",
      sort: (a) => a.node,
      facet: (a) => a.node,
      facetSpec: { role: "node", primary: true } },
    { field: "ingress", label: "Ingress Rules", sort: (a) => a.ingress?.length ?? 0, filter: fromSort("number", { unit: "count" }) },
    { field: "egress", label: "Egress Rules", sort: (a) => a.egress?.length ?? 0, filter: fromSort("number", { unit: "count" }) },
    { field: "used_by", label: "Used By", sort: (a) => a.used_by?.length ?? 0, filter: fromSort("number", { unit: "count", presets: [{ label: "None", min: null, max: 0 }, { label: "Any", min: 1, max: null }] }) },
    {
      field: "project",
      label: "Project",
      // The project that HOLDS this record, which is a different question from the
      // `projects` column below it: that one answers who consumes it.
      sort: (a) => a.project ?? "",
      facet: (a) => a.project ?? "",
      facetSpec: { role: "project", primary: true },
    },
    {
      // filter-coverage: this is not the project dimension. It answers which projects
      // CONSUME the record, derived from used_by, while the "project" column above answers
      // which one holds it, and that is the column carrying the project role.
      field: "projects",
      label: "Used By Projects",
      // Was a copy of the used_by accessor, so the header sorted by resource count while the
      // cell displayed a project count. It now sorts by what it displays.
      sort: (a) => projectsFromUsedBy(a.used_by).length,
      facet: (a) => projectsFromUsedBy(a.used_by),
      facetSpec: { primary: true, emptyLabel: "(no project)" } },
    {
      field: "usage",
      label: "Usage",
      facet: (a) => (a.used_by?.length ? "In use" : "Unused"),
      facetSpec: {} },
  ] });

function NetworkACLsContent() {
  const { data, isLoading, refreshNow, dataUpdatedAt, isError, error, refetch } = useNetworkACLs();
  const acls = (data as any)?.acls || [];
  
  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const reach = reachabilityOf(data);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const resetPage = () => setCurrentPage(1);

  // note: derived from the rows, not from the fleet. It is empty exactly when the scope
  // returns nothing, which is the state that used to leave the empty row one column too wide.
  const nodes = useMemo(() => {
    const nodeSet = new Set(acls.map((a: any) => a.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [acls]);

  // The column appears once any node is known, the facet once there are two to choose
  // between. The thresholds differ on purpose: one option filters nothing.
  const availableFields = useMemo(
    () => (nodes.length > 0 ? ACLS.facetFields : ACLS.facetFields.filter((f) => f !== "node")),
    [nodes.length],
  );

  // One dialog for the page, opened from either cell. See components/used-by.
  const usedBy = useUsedByDialog();

  const { hasRole } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  const canManageACL = hasRole(Role.PARTNER);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<NetworkACLInfo | undefined>(undefined);
  const [pendingDelete, setPendingDelete] = useState<NetworkACLInfo | undefined>(
    undefined,
  );
  const deleteACL = useDeleteNetworkACL();
  const { toast } = useToast();

  const filters = useTableFilters(ACLS, acls as NetworkACLInfo[], {
    onFilterChange: resetPage,
    availableFields });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    ACLS.sortAccessors,
    ACLS.defaultSort.field,
    ACLS.defaultSort.direction,
    resetPage,
  );

  const sortedACLs = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );
  const focus = useRowFocus({
    list: "network-acls",
    rows: acls,
    orderedRows: sortedACLs,
    keyOf: aclRowKey,
    pageSize,
    setPage: setCurrentPage,
    dataUpdatedAt,
    clearFilters: filters.clearAll,
  });

  const totalPages = Math.ceil(sortedACLs.length / pageSize);
  const paginatedACLs = sortedACLs.slice(
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
        title="Network ACLs"
        description="Manage hypervisor network access control lists"
        noun="network ACLs"
        error={error}
        onRetry={() => void refetch()}
      />
    );
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Network ACLs
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor network access control lists
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RefreshButton
            label="Refresh network ACLs"
            lastUpdatedAt={dataUpdatedAt}
            onRefresh={refreshNow}
          />
          {/* Only Partner+ can write a policy; the API enforces this too. */}
          {canManageACL && (
            <Button
              onClick={() => {
                setEditing(undefined);
                setDialogOpen(true);
              }}
              disabled={isReadOnly}
              title={
                isReadOnly
                  ? "Action disabled in read-only mode"
                  : "Create a new network ACL"
              }
            >
              <Plus className="h-4 w-4 mr-2" />
              Create Network ACL
            </Button>
          )}
        </div>
      </div>

      <UnreachableNodesBanner
        noun="network ACLs"
        nodes={reach.unreachable}
        partialNodes={reach.partial}
        lateNodes={reach.late}
        reasons={reach.reasons}
      />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>All Network ACLs</CardTitle>
          <TableToolbar filters={filters} noun="network ACLs" />
        </CardHeader>
        <CardContent>
          {focus.hidden && (
            <RowFocusNotice label={focus.hidden.label} onShow={focus.showHidden} className="mb-4" />
          )}
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Description" field="description" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && <SortableTableHead label="Cluster" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />}
                <SortableTableHead label="Ingress Rules" field="ingress" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Egress Rules" field="egress" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {canManageACL && <TableHead className="text-right"><span className="sr-only">Actions</span></TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedACLs.length === 0 ? (
                <TableEmptyRow
                  colSpan={(nodes.length > 0 ? 8 : 7) + (canManageACL ? 1 : 0)}
                  icon={ShieldCheck}
                  illustration={filters.activeFilterCount > 0 ? undefined : PoliciesIllustration}
                  title={filters.activeFilterCount > 0 ? "No network ACLs found" : "No network ACLs yet"}
                  description={
                    filters.activeFilterCount > 0
                      ? "No network ACL matches the current filters."
                      : undefined
                  }
                />
              ) : (
                paginatedACLs.map((acl: any) => (
                  <TableRow key={aclRowKey(acl)} {...focus.rowProps(aclRowKey(acl))}>
                    <TableCell className="font-medium">{acl.name}</TableCell>
                    <TableCell className="max-w-md">
                      {acl.description ? (
                        <OverflowText
                          value={acl.description}
                          label={`description for ${acl.name}`}
                          lines={2}
                        />
                      ) : (
                        <TableEmptyValue label="No description" />
                      )}
                    </TableCell>
                    {nodes.length > 0 && (
                      <TableCell><NodeLink clusterId={acl.node} /></TableCell>
                    )}
                    <TableCell>
                      <RuleCountCell
                        count={acl.ingress?.length ?? 0}
                        direction="ingress"
                        aclName={acl.name}
                        onOpen={() => usedBy.openPanel(aclSubject(acl), "rules")}
                      />
                    </TableCell>
                    <TableCell>
                      <RuleCountCell
                        count={acl.egress?.length ?? 0}
                        direction="egress"
                        aclName={acl.name}
                        onOpen={() => usedBy.openPanel(aclSubject(acl), "rules")}
                      />
                    </TableCell>
                    <TableCell>
                      <UsedByCell
                        usedBy={acl.used_by}
                        noun="networks"
                        subjectName={acl.name}
                        emptyLabel="Not used"
                        onOpen={() => usedBy.openUsage(aclSubject(acl))}
                      />
                    </TableCell>
                    <TableCell>{acl.project || EMPTY_VALUE}</TableCell>
                    <TableCell>
                      <ProjectsCell
                        usedBy={acl.used_by}
                        subjectName={acl.name}
                        onOpen={() => usedBy.openProjects(aclSubject(acl))}
                      />
                    </TableCell>
                    {canManageACL && (
                      <TableCell className="text-right whitespace-nowrap">
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => {
                            setEditing(acl);
                            setDialogOpen(true);
                          }}
                          disabled={isReadOnly}
                          aria-label={`Edit ${acl.name}`}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => setPendingDelete(acl)}
                          disabled={isReadOnly}
                          aria-label={`Delete ${acl.name}`}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
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
                totalItems={sortedACLs.length}
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

      {canManageACL && (
        <AclFormDialog
          open={dialogOpen}
          onOpenChange={setDialogOpen}
          existing={editing}
          onSaved={(saved) => {
            if (saved.created) requestRowFocus("network-acls", aclRowKey(saved), saved.name);
            // the write already succeeded; a refresh that fails is the list's own error state
            void Promise.resolve(refreshNow()).catch(() => undefined);
          }}
        />
      )}

      <AlertDialog
        open={!!pendingDelete}
        onOpenChange={(open) => !open && setPendingDelete(undefined)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {pendingDelete?.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              {pendingDelete?.used_by?.length
                ? `This ACL is attached to ${plural(pendingDelete.used_by.length, "resource")}. LXD refuses to delete an ACL that is still in use.`
                : "This removes the policy from the cluster. It cannot be undone."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={async () => {
                if (!pendingDelete) return;
                const target = pendingDelete;
                setPendingDelete(undefined);
                try {
                  await deleteACL.mutateAsync({
                    name: target.name,
                    host: target.node ?? "",
                    project: target.project,
                  });
                  toast({
                    title: "Network ACL deleted",
                    description: `"${target.name}" removed from ${target.node}.`,
                  });
                  refreshNow();
                } catch (e) {
                  toast({
                    title: "Could not delete network ACL",
                    description:
                      e instanceof Error ? e.message : "The cluster refused the delete.",
                    variant: "destructive",
                  });
                }
              }}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

// Suspense is required because useTableFilters reads useSearchParams, which opts the
// component out of prerendering.
export default function NetworkACLsPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <NetworkACLsContent />
    </Suspense>
  );
}

// note: the ingress and egress arrays are the richest payload in the app that nothing rendered.
// The table showed "N rules" as plain text, so action, source, destination, protocol, ports,
// state and each rule's own description all arrived on the wire and were dropped.
function aclSubject(acl: NetworkACLInfo): UsedBySubject {
  const config = acl.config ?? {};
  const ingress = acl.ingress ?? [];
  const egress = acl.egress ?? [];
  return {
    kind: "network-acls",
    name: acl.name,
    node: acl.node,
    description: acl.description || undefined,
    usedBy: acl.used_by ?? [],
    facts: [
      { label: "Cluster", value: <NodeLink clusterId={acl.node} /> },
      { label: "Ingress Rules", value: ingress.length },
      { label: "Egress Rules", value: egress.length },
    ],
    panels: [
      { id: "rules", label: "Rules", kind: "aclRules", ingress, egress },
      { id: "config", label: "Configuration", kind: "config", entries: config },
    ],
  };
}

// A rule count with the rules already in the row payload. Plain text here was the same dead end
// as the ISO "N instances" cell: a number the operator could see and never open.
function RuleCountCell({
  count,
  direction,
  aclName,
  onOpen,
}: {
  count: number;
  direction: "ingress" | "egress";
  aclName: string;
  onOpen: () => void;
}) {
  if (count === 0) return <TableEmptyValue label={`No ${direction} rules`} />;
  return (
    <TableLink onClick={onOpen} aria-label={`Show the ${plural(count, `${direction} rule`)} on ${aclName}`}>
      {plural(count, "rule")}
    </TableLink>
  );
}
