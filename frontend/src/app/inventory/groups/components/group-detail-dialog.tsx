"use client";

import * as React from "react";
import { Suspense } from "react";
import { Shield, Users } from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { Button } from "@/components/ui/button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty-state";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { CopyableText } from "@/components/detail/copyable-text";
import { defineTable, fromSort } from "@/lib/table-filter";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { PANEL_PAGE_SIZE, PANEL_PAGE_SIZES } from "@/lib/pagination";
import { parseLxdReference } from "@/lib/used-by";
import { referenceKind } from "@/lib/used-by-kinds";
import { formatLabel, plural } from "@/lib/utils";
import { OverflowText } from "@/components/ui/overflow-text";
import { NodeLink } from "@/components/ui/node-link";

export type GroupDetailTab = "overview" | "members" | "permissions";

// A group's overview, members and permissions in one dialog, with each permission's resource
// resolved to a link where it has a page.
//
// important: permission URLs go through parseLxdReference, the same module the used_by dialogs
// use and a faithful port of the backend's tenant-visibility parser. Do not write a second
// parser here.
const PERMISSIONS = defineTable<any>()({
  id: "perms",
  searchPlaceholder: "Search permissions",
  defaultSort: { field: "entity_type", direction: "asc" },
  columns: [
    {
      field: "entity_type",
      label: "Entity Type",
      sort: (p) => p.entity_type,
      facet: (p) => p.entity_type,
      facetSpec: { primary: true, format: formatLabel },
    },
    { field: "url", label: "Resource", sort: (p) => p.url, search: (p) => p.url, filter: fromSort("text") },
    {
      field: "entitlement",
      label: "Entitlement",
      sort: (p) => p.entitlement,
      facet: (p) => p.entitlement,
      facetSpec: { primary: true },
    },
  ],
});

function PermissionsPanelContent({ group }: { group: any }) {
  const permissions = React.useMemo(() => group?.permissions ?? [], [group]);
  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState(PANEL_PAGE_SIZE);
  const resetPage = React.useCallback(() => setPage(1), []);

  const filters = useTableFilters(PERMISSIONS, permissions, { syncUrl: false, onFilterChange: resetPage });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    PERMISSIONS.sortAccessors,
    PERMISSIONS.defaultSort.field,
    PERMISSIONS.defaultSort.direction,
    resetPage,
  );
  // Filter, then sort, then slice, so an ordering covers every permission the filters kept and
  // not only the page on screen.
  const sortedPermissions = React.useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);
  // Clamped on read instead of corrected in an effect, which would paint one wrong frame first
  // and then move the table while the reader is auditing it.
  const totalPages = Math.max(1, Math.ceil(sortedPermissions.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedPermissions = sortedPermissions.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  if (permissions.length === 0) {
    return <EmptyState icon={Shield} title="No permissions" description="This group grants nothing." />;
  }

  return (
    <div className="space-y-3">
      <TableToolbar filters={filters} noun="permissions" />
      <Table>
        <TableHeader>
          <TableRow>
            <SortableTableHead label="Entity Type" field="entity_type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Resource" field="url" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Entitlement" field="entitlement" currentField={sortField} direction={sortDirection} onSort={handleSort} />
          </TableRow>
        </TableHeader>
        <TableBody>
          {pagedPermissions.length === 0 ? (
            <TableEmptyRow
              colSpan={3}
              icon={Shield}
              title="No permissions match the current filters"
              description={`${plural(filters.activeFilterCount, "filter")} active on ${plural(permissions.length, "permission")}.`}
              action={
                <Button variant="outline" size="sm" onClick={() => filters.clearAll()}>
                  Clear filters
                </Button>
              }
            />
          ) : (
            pagedPermissions.map((permission: any, index: number) => {
              const reference = parseLxdReference(permission.url ?? "");
              const kind = referenceKind(reference);
              const href = kind.href?.(reference, group?.node);
              const name = reference.name ?? permission.url;
              return (
                <TableRow key={`${permission.url}-${permission.entitlement}-${index}`}>
                  <TableCell>{formatLabel(permission.entity_type ?? "")}</TableCell>
                  <TableCell>
                    {/* The resource by name, linked when its kind has a page. */}
                    {href ? (
                      <TableLink href={href} title={`Open ${name} in ${kind.label}s`}>
                        {name}
                      </TableLink>
                    ) : (
                      <OverflowText value={name} label={`${kind.label} name`} />
                    )}
                    {/* important: the URL is what the permission grants against, so it must
                        stay readable in full; a reader who cannot see all of it cannot audit
                        the grant. */}
                    <TableSubText wrap>
                      <OverflowText value={permission.url} label={`permission target for ${name}`} mono />
                    </TableSubText>
                  </TableCell>
                  <TableCell>{formatLabel(permission.entitlement ?? "")}</TableCell>
                </TableRow>
              );
            })
          )}
        </TableBody>
      </Table>
      {/* Absent rather than disabled when everything fits on one page. No mt-4 wrapper: the
          panel is already a space-y-3 stack, whose gap wins over a margin on the child. */}
      {totalPages > 1 && (
        <DataTablePagination
          currentPage={currentPage}
          totalPages={totalPages}
          pageSize={pageSize}
          pageSizeOptions={[...PANEL_PAGE_SIZES]}
          totalItems={sortedPermissions.length}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setPage(1);
          }}
        />
      )}
    </div>
  );
}

// See the note in components/used-by/usage-panel.tsx: useTableFilters reads the query string
// even with syncUrl false, so this carries its own boundary instead of relying on its host.
function PermissionsPanel(props: React.ComponentProps<typeof PermissionsPanelContent>) {
  return (
    <Suspense fallback={<Skeleton className="h-64 w-full" />}>
      <PermissionsPanelContent {...props} />
    </Suspense>
  );
}

function IdentityList({ label, ids }: { label: string; ids: string[] }) {
  if (!ids?.length) return null;
  return (
    <DetailSection title={`${label} (${ids.length})`}>
      <ul className="divide-y rounded-md border">
        {ids.map((id) => (
          <li key={id} className="px-3 py-2">
            <CopyableText value={id} label={`${label.toLowerCase()} identity ${id}`} />
          </li>
        ))}
      </ul>
    </DetailSection>
  );
}

export function GroupDetailDialog({
  group,
  open,
  onOpenChange,
  defaultTab = "overview",
}: {
  group: any | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaultTab?: GroupDetailTab;
}) {
  if (!group) return null;

  const tls = group.identities?.tls ?? [];
  const oidc = group.identities?.oidc ?? [];
  const memberCount = tls.length + oidc.length;

  return (
    <RecordDetailDialog
      open={open}
      onOpenChange={onOpenChange}
      title={group.name}
      recordId={group.name}
      recordIdLabel="Group"
      subtitle={group.node}
      defaultTab={defaultTab}
      size="xl"
      tabs={[
        {
          value: "overview",
          label: "Overview",
          content: (
            <DetailGrid cols={3}>
              <DetailField label="Description" value={group.description} span="full" />
              <DetailField label="Cluster" value={<NodeLink clusterId={group.node} />} />
              <DetailField label="Members" value={memberCount} />
              <DetailField label="Permissions" value={group.permissions?.length ?? 0} />
            </DetailGrid>
          ),
        },
        {
          value: "members",
          label: `Members (${memberCount})`,
          content:
            memberCount === 0 ? (
              <EmptyState icon={Users} title="No members" description="Nothing is in this group." />
            ) : (
              <div className="space-y-4">
                <IdentityList label="TLS" ids={tls} />
                <IdentityList label="OIDC" ids={oidc} />
              </div>
            ),
        },
        {
          value: "permissions",
          label: `Permissions (${group.permissions?.length ?? 0})`,
          content: <PermissionsPanel group={group} />,
        },
      ]}
    />
  );
}
