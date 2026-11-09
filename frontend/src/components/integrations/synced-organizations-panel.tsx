"use client";

import { Suspense, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRightLeft, Building2, KeyRound } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { OverflowText } from "@/components/ui/overflow-text";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { RowActions, type RowAction } from "@/components/ui/row-actions";
import { Skeleton } from "@/components/ui/skeleton";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { OwnerKeysDialog } from "@/components/integrations/owner-keys-dialog";
import { TransferOwnerKeyDialog } from "@/components/integrations/transfer-owner-key-dialog";
import { usePermissions } from "@/hooks/use-permissions";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { api } from "@/lib/api";
import {
  OWNER_KEY_STATE,
  OWNER_KEY_STATE_ORDER,
  formatKeyDate,
  organizationIsFrozen,
  ownerKeyState,
} from "@/lib/key-ownership-display";
import { defineTable, fromSort } from "@/lib/table-filter";
import { retryUnlessUnavailable } from "@/lib/read-error";
import type { IntegrationOrganization } from "@/types/api";
import { plural } from "@/lib/utils";
import { OrganizationsIllustration } from "@/components/illustrations/scenes/onboarding";

/** Organization, Slug, Owner Key, Owner State, and Actions for an administrator. */
const columnCount = (withActions: boolean) => (withActions ? 5 : 4);

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
  const [managing, setManaging] = useState<IntegrationOrganization | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const resetPage = () => setPage(1);

  const orgsQuery = useQuery({
    queryKey: ["integrations", "organizations"],
    queryFn: () => api.integrations.listOrganizations(),
    staleTime: 30000,
    // Each owner key is read live from Vault on this route, so a 503 does not clear in a retry.
    retry: retryUnlessUnavailable,
  });
  // The transfer dialog picks from the key list. Shared with the key cards above, so this is one
  // read for the whole tab, and partners never open the dialog.
  const keysQuery = useQuery({
    queryKey: ["integrations", "api-keys"],
    queryFn: () => api.integrations.listAPIKeys(),
    enabled: isAdmin,
    staleTime: 30000,
    // important: the same rule as the key list's own observer. Without it an admin's 503 was
    // retried with react-query's default while a partner's was not.
    retry: retryUnlessUnavailable,
  });

  const rows = useMemo(() => orgsQuery.data ?? [], [orgsQuery.data]);
  const frozen = rows.filter((org) => organizationIsFrozen(org)).length;

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

  // Both dialogs pick from the key list. Offered only once it has been read, so an empty picker
  // never stands in for a list that is still loading or could not be read.
  const keysUnavailable = keysQuery.isLoading
    ? "Reading the key list"
    : keysQuery.error && !keysQuery.data
      ? "The key list could not be read; refresh to try again"
      : undefined;
  const actionsFor = (org: IntegrationOrganization): RowAction[] => [
    {
      id: "owner-keys",
      label: "Owner Keys",
      icon: KeyRound,
      onSelect: () => setManaging(org),
      unavailableReason: org.api_key_id ? keysUnavailable : "No owner yet: transfer it to a key first",
    },
    {
      id: "transfer-owner",
      label: "Transfer Owner",
      icon: ArrowRightLeft,
      onSelect: () => setTransferring(org),
      unavailableReason: keysUnavailable,
    },
  ];

  let body: React.ReactNode;
  if (orgsQuery.isLoading) {
    body = <Skeleton className="h-40 rounded-lg" />;
  } else if (orgsQuery.error && !orgsQuery.data) {
    body = (
      <ReadErrorState noun="synced organizations" error={orgsQuery.error} onRetry={() => void orgsQuery.refetch()} />
    );
  } else if (rows.length === 0) {
    body = (
      <EmptyState
        icon={Building2}
        illustration={OrganizationsIllustration}
        title="No organizations are synced yet"
        description="An OrcaHub connection's first sync creates them, owned by the key it used."
      />
    );
  } else {
    body = (
      <div className="space-y-4">
        <TableToolbar filters={filters} noun="organizations" pageSize={pageSize} />
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
              {/* Partners read this list; a column of empty cells for them said nothing. */}
              {isAdmin && (
                <TableHead pinned="end" className="w-12 text-right">
                  <span className="sr-only">Actions</span>
                </TableHead>
              )}
            </TableRow>
          </TableHeader>
          <TableBody>
            {paginated.length === 0 ? (
              <TableEmptyRow
                colSpan={columnCount(isAdmin)}
                icon={Building2}
                title="No organization matches the current filters"
                description={`${plural(filters.activeFilterCount, "filter")} active`}
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
                      {(org.owner_keys?.length ?? 0) > 1 && (
                        <TableSubText wrap>
                          Primary of {org.owner_keys?.length} owner keys
                        </TableSubText>
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge variant={OWNER_KEY_STATE[state].tone}>{OWNER_KEY_STATE[state].label}</Badge>
                      {state === "expiring" && org.owner_key_expires_at && (
                        <TableSubText>Expires {formatKeyDate(org.owner_key_expires_at)}</TableSubText>
                      )}
                      {!isAdmin && organizationIsFrozen(org) && (
                        <TableSubText wrap>Ask an administrator to transfer this organization to an active key.</TableSubText>
                      )}
                      {state === "hidden" && (
                        <TableSubText wrap>Only administrators see this owner key, and partners of the key&apos;s own organization once it has provisioned a buyer.</TableSubText>
                      )}
                    </TableCell>
                    {isAdmin && (
                      <TableCell pinned="end" className="text-right">
                        <RowActions triggerLabel={`Actions for ${org.slug}`} actions={actionsFor(org)} />
                      </TableCell>
                    )}
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
    // note: scroll-mt-20 clears the sticky header (h-14, md:h-16) when the Review jump scrolls here.
    <Card id="synced-organizations" tabIndex={-1} className="scroll-mt-20 focus:outline-none">
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
              {plural(frozen, "organization has", "organizations have")} an owner key that
              no longer works. Their sync and provisioning are paused until you transfer them.
            </AlertDescription>
          </Alert>
        )}
        {body}
      </CardContent>
      {managing && (
        <OwnerKeysDialog
          organization={rows.find((org) => org.id === managing.id) ?? managing}
          keys={keysQuery.data?.keys ?? []}
          onOpenChange={(open) => {
            if (!open) setManaging(null);
          }}
        />
      )}
      {transferring && (
        <TransferOwnerKeyDialog
          // the current row, so a background re-read is not hidden behind a snapshot
          organization={rows.find((org) => org.id === transferring.id) ?? transferring}
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
