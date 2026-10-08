"use client";

import { Suspense, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRightLeft, Building2, RotateCcw } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { OverflowText } from "@/components/ui/overflow-text";
import { RowActions, type RowAction } from "@/components/ui/row-actions";
import { Skeleton } from "@/components/ui/skeleton";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { TransferOwnerKeyDialog } from "@/components/integrations/transfer-owner-key-dialog";
import { usePermissions } from "@/hooks/use-permissions";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { api } from "@/lib/api";
import {
  OWNER_KEY_STATE,
  OWNER_KEY_STATE_ORDER,
  formatKeyDate,
  ownerKeyIsFrozen,
  ownerKeyState,
} from "@/lib/key-ownership-display";
import { defineTable, fromSort } from "@/lib/table-filter";
import type { IntegrationOrganization } from "@/types/api";

const COLUMN_COUNT = 5;

function ownerLabel(org: IntegrationOrganization): string {
  return org.owner_key_name || org.api_key_id || "(no owner key)";
}

const SYNCED_ORGANIZATIONS = defineTable<IntegrationOrganization>()({
  // note: an id of its own. The Integrations tab already carries the API key list, and two tables
  // without a namespace would share `q` and narrow each other on every keystroke.
  id: "syncedorgs",
  searchPlaceholder: "Search organizations by name, slug, or owner key",
  defaultSort: { field: "organization", direction: "asc" },
  searchExtra: (org) => [org.api_key_id ?? "", org.external_id ?? "", org.app_name ?? ""],
  columns: [
    {
      field: "organization",
      label: "Organization",
      sort: (org) => org.name,
      search: (org) => org.name,
      filter: fromSort("text"),
    },
    {
      field: "slug",
      label: "Slug",
      sort: (org) => org.slug,
      search: (org) => org.slug,
      filter: fromSort("text"),
    },
    {
      field: "owner",
      label: "Owner Key",
      sort: (org) => ownerLabel(org),
      facet: (org) => ownerLabel(org),
    },
    {
      field: "state",
      label: "Owner State",
      sort: (org) => OWNER_KEY_STATE[ownerKeyState(org)].label,
      facet: (org) => OWNER_KEY_STATE[ownerKeyState(org)].label,
      // The one question this list exists for: which organizations are frozen behind a dead key.
      facetSpec: { primary: true, order: OWNER_KEY_STATE_ORDER },
    },
  ],
});

/**
 * The OrcaHub organizations synced into this control plane, and the key that owns each one.
 *
 * Only the owner key may sync an organization, update its members and policies and provision
 * tenant access for it, so an owner that is revoked, expired or deleted freezes it. Administrators
 * move such an organization to another key here; a Partner sees its own organizations read-only.
 */
function SyncedOrganizationsContent() {
  const { isAdmin } = usePermissions();
  const [transferring, setTransferring] = useState<IntegrationOrganization | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const resetPage = () => setPage(1);

  const orgsQuery = useQuery({
    queryKey: ["integrations", "organizations"],
    queryFn: () => api.integrations.listOrganizations(),
    staleTime: 30000,
  });
  // The transfer dialog picks from the key list. Shared with the key cards above, so this is one
  // read for the whole tab, and partners never open the dialog.
  const keysQuery = useQuery({
    queryKey: ["integrations", "api-keys"],
    queryFn: () => api.integrations.listAPIKeys(),
    enabled: isAdmin,
    staleTime: 30000,
  });

  const rows = useMemo(() => orgsQuery.data ?? [], [orgsQuery.data]);
  const frozen = rows.filter((org) => ownerKeyIsFrozen(ownerKeyState(org))).length;

  const filters = useTableFilters(SYNCED_ORGANIZATIONS, rows, { onFilterChange: resetPage, syncUrl: false });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    SYNCED_ORGANIZATIONS.sortAccessors,
    SYNCED_ORGANIZATIONS.defaultSort.field,
    SYNCED_ORGANIZATIONS.defaultSort.direction,
    resetPage,
  );
  const sorted = sortItems(filters.filteredRows);
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const paginated = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const actionsFor = (org: IntegrationOrganization): RowAction[] => [
    {
      id: "transfer-owner",
      label: "Transfer Owner",
      icon: ArrowRightLeft,
      onSelect: () => setTransferring(org),
    },
  ];

  let body: React.ReactNode;
  if (orgsQuery.isLoading) {
    body = <Skeleton className="h-40 rounded-lg" />;
  } else if (orgsQuery.error) {
    body = (
      <EmptyState
        icon={RotateCcw}
        title="Could not load synced organizations"
        description={(orgsQuery.error as Error).message}
        action={
          <Button size="sm" onClick={() => void orgsQuery.refetch()}>
            Retry
          </Button>
        }
      />
    );
  } else if (rows.length === 0) {
    body = (
      <EmptyState
        icon={Building2}
        title="No organizations are synced yet"
        description="An OrcaHub connection's first sync creates them, owned by the key it used."
      />
    );
  } else {
    body = (
      <div className="space-y-4">
        <TableToolbar filters={filters} noun="organizations" />
        <Table>
          <TableHeader>
            <TableRow>
              <SortableTableHead label="Organization" field="organization" currentField={sortField}
                direction={sortDirection} onSort={handleSort} />
              <SortableTableHead label="Slug" field="slug" currentField={sortField}
                direction={sortDirection} onSort={handleSort} />
              <SortableTableHead label="Owner Key" field="owner" currentField={sortField}
                direction={sortDirection} onSort={handleSort} />
              <SortableTableHead label="Owner State" field="state" currentField={sortField}
                direction={sortDirection} onSort={handleSort} />
              <TableHead className="w-12 text-right">
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {paginated.length === 0 ? (
              <TableEmptyRow
                colSpan={COLUMN_COUNT}
                icon={Building2}
                title="No organization matches the current filters"
                description={`${filters.activeFilterCount} filter${filters.activeFilterCount === 1 ? "" : "s"} active`}
              />
            ) : (
              paginated.map((org) => {
                const state = ownerKeyState(org);
                return (
                  <TableRow key={org.id}>
                    <TableCell className="font-medium">{org.name}</TableCell>
                    <TableCell className="font-mono">{org.slug}</TableCell>
                    <TableCell className="max-w-[16rem]">
                      <OverflowText value={ownerLabel(org)} label={`owner key of ${org.slug}`} />
                      {org.api_key_id && org.owner_key_name && (
                        <TableSubText wrap>
                          <OverflowText value={org.api_key_id} label={`owner key id of ${org.slug}`} />
                        </TableSubText>
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge variant={OWNER_KEY_STATE[state].tone}>{OWNER_KEY_STATE[state].label}</Badge>
                      {state === "expiring" && org.owner_key_expires_at && (
                        <TableSubText>Expires {formatKeyDate(org.owner_key_expires_at)}</TableSubText>
                      )}
                      {!isAdmin && ownerKeyIsFrozen(state) && (
                        <TableSubText wrap>Ask an administrator to transfer this organization to an active key.</TableSubText>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {isAdmin && (
                        <RowActions triggerLabel={`Actions for ${org.slug}`} actions={actionsFor(org)} />
                      )}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
        {totalPages > 1 && (
          <DataTablePagination
            currentPage={currentPage}
            totalPages={totalPages}
            totalItems={sorted.length}
            pageSize={pageSize}
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

  return (
    <Card id="synced-organizations" tabIndex={-1} className="scroll-mt-4 focus:outline-none">
      <CardHeader>
        <CardTitle className="text-lg">Synced Organizations</CardTitle>
        <CardDescription>
          The OrcaHub organizations synced into this control plane. Only the owner key may sync an
          organization, update its members and policies, and provision tenant access for it.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {isAdmin && frozen > 0 && (
          <Alert>
            <AlertTriangle className="h-4 w-4" aria-hidden />
            <AlertDescription>
              {frozen} {frozen === 1 ? "organization has" : "organizations have"} an owner key that
              no longer works. Their sync and provisioning are paused until you transfer them.
            </AlertDescription>
          </Alert>
        )}
        {body}
      </CardContent>
      {transferring && (
        <TransferOwnerKeyDialog
          organization={transferring}
          keys={keysQuery.data?.keys ?? []}
          onOpenChange={(open) => {
            if (!open) setTransferring(null);
          }}
        />
      )}
    </Card>
  );
}

/**
 * The boundary the filter hook requires: it reads the query string, which opts a client component
 * out of prerendering, and Next fails the build on the route unless a boundary sits above it.
 */
export function SyncedOrganizationsPanel() {
  return (
    <Suspense fallback={<Skeleton className="h-40 rounded-lg" />}>
      <SyncedOrganizationsContent />
    </Suspense>
  );
}
