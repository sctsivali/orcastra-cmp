"use client";

import { Fragment, Suspense, useCallback, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, KeyRound, MoreVertical, Plus } from "lucide-react";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { EmptyState } from "@/components/ui/empty-state";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { useToast } from "@/hooks/use-toast";
import { usePermissions } from "@/hooks/use-permissions";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { defineTable, fromSort } from "@/lib/table-filter";
import { formatDate, formatDateTime } from "@/lib/utils";
import { miniApi, type IssuedBundle, type Operator, type OperatorRole } from "../api";
import { BundleReveal } from "./bundle-reveal";
import { IssueDialog } from "./issue-dialog";

const ROLE_LABEL: Record<OperatorRole, string> = { admin: "Administrator", partner: "Partner", tenant: "Tenant" };
const STATUS_LABEL: Record<Operator["status"], string> = { active: "Active", revoked: "Revoked", deleted: "Deleted" };

const OPERATORS = defineTable<Operator>()({
  id: "mini-operators",
  searchPlaceholder: "Search operators",
  defaultSort: { field: "operator", direction: "asc" },
  columns: [
    {
      field: "operator", label: "Operator", sort: (o) => (o.display_name || o.email).toLowerCase(),
      search: (o) => `${o.display_name ?? ""} ${o.email}`, filter: fromSort("text"),
    },
    { field: "role", label: "Role", sort: (o) => o.role, facet: (o) => ROLE_LABEL[o.role], facetSpec: { primary: true } },
    {
      field: "organization", label: "Organization", sort: (o) => o.owner_org_name ?? "",
      facet: (o) => o.owner_org_name, facetSpec: { emptyLabel: "(none)" },
    },
    { field: "status", label: "Status", sort: (o) => o.status, facet: (o) => STATUS_LABEL[o.status], facetSpec: { primary: true } },
    {
      field: "expires", label: "Certificate Expires", sort: (o) => (o.expires_at ? new Date(o.expires_at).getTime() : 0),
      facet: (o) => (o.expiring ? "Expiring soon" : null), filter: fromSort("date", { horizon: "future" }),
    },
    { field: "last", label: "Last Sign-In", sort: (o) => (o.last_login_at ? new Date(o.last_login_at).getTime() : 0), filter: fromSort("date", { horizon: "past" }) },
  ],
});

type Confirm =
  | { kind: "revoke" | "delete" | "restore" | "reactivate"; operator: Operator }
  | { kind: "revoke-cert"; operator: Operator; fingerprint: string }
  | { kind: "role"; operator: Operator; role: OperatorRole };

function statusBadge(status: Operator["status"]) {
  const variant = status === "active" ? "positive" : status === "revoked" ? "negative" : "outline";
  return <Badge variant={variant}>{STATUS_LABEL[status]}</Badge>;
}

function OperatorsContent() {
  const { scopeKey } = useActorQueryScope();
  const { isAdmin } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  const { toast } = useToast();
  const [issueOpen, setIssueOpen] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [busy, setBusy] = useState(false);
  const [reissued, setReissued] = useState<{ bundle: IssuedBundle; email: string } | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);

  const query = useRefreshableQuery({ queryKey: ["mini-operators", scopeKey], queryFn: () => miniApi.listOperators() });
  const resetPage = useCallback(() => setPage(1), []);
  const filters = useTableFilters(OPERATORS, query.data ?? [], { onFilterChange: resetPage });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    OPERATORS.sortAccessors, OPERATORS.defaultSort.field, OPERATORS.defaultSort.direction, resetPage,
  );
  const sorted = useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const rows = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const run = async (action: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await action();
      toast({ title: done });
      await query.refetch();
    } catch (err) {
      toast({ title: "The change was not made", description: err instanceof Error ? err.message : "Try again.", variant: "destructive" });
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const confirmAction = () => {
    if (!confirm) return;
    const { operator } = confirm;
    if (confirm.kind === "revoke") return run(() => miniApi.updateOperator(operator.uuid, { status: "revoked" }), `${operator.email} revoked`);
    if (confirm.kind === "reactivate") return run(() => miniApi.updateOperator(operator.uuid, { status: "active" }), `${operator.email} reactivated`);
    if (confirm.kind === "delete") return run(() => miniApi.deleteOperator(operator.uuid), `${operator.email} deleted`);
    if (confirm.kind === "restore") return run(() => miniApi.restoreOperator(operator.uuid), `${operator.email} restored; issue a certificate to sign in`);
    if (confirm.kind === "role") return run(() => miniApi.updateOperator(operator.uuid, { role: confirm.role }), `${operator.email} is now ${ROLE_LABEL[confirm.role]}`);
    if (confirm.kind === "revoke-cert") return run(() => miniApi.revokeCertificate(operator.uuid, confirm.fingerprint), "Certificate revoked");
  };

  const reissue = async (operator: Operator) => {
    setBusy(true);
    try {
      const bundle = await miniApi.reissueCertificate(operator.uuid);
      setReissued({ bundle, email: operator.email });
      await query.refetch();
    } catch (err) {
      toast({ title: "No certificate was issued", description: err instanceof Error ? err.message : "Try again.", variant: "destructive" });
    } finally {
      setBusy(false);
    }
  };

  if (query.isLoading) return <LoadingLottiePage />;

  const confirmCopy: Record<Confirm["kind"], { title: string; body: string; action: string; destructive: boolean }> = {
    revoke: { title: "Revoke Operator", body: "Every session ends on the next request and open terminals close within a minute. Reactivating later does not bring old sessions back.", action: "Revoke", destructive: true },
    reactivate: { title: "Reactivate Operator", body: "The operator can sign in again with any certificate that is still active.", action: "Reactivate", destructive: false },
    delete: { title: "Delete Operator", body: "Every certificate is revoked and every session ends. The email stays reserved so nobody else inherits this operator's access; restore it to bring the operator back.", action: "Delete", destructive: true },
    restore: { title: "Restore Operator", body: "The operator becomes active again with their memberships. Their old certificates stay revoked: issue a new one.", action: "Restore", destructive: false },
    "revoke-cert": { title: "Revoke Certificate", body: "Sessions signed in with this certificate end on the next request. The operator's other certificates keep working.", action: "Revoke Certificate", destructive: true },
    role: { title: "Change Role", body: "Every session of this operator ends on the next request and signs in again with the new role.", action: "Change Role", destructive: false },
  };
  const copy = confirm ? confirmCopy[confirm.kind] : null;

  return (
    <div className="space-y-6 p-4 sm:p-8">
      <ReadOnlyBanner />
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold sm:text-3xl">Operators</h1>
          <p className="mt-1 text-sm text-muted-foreground sm:text-base">
            {isAdmin ? "Everyone who signs in, their roles and their certificates." : "Tenants of the organizations you run, and their certificates."}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <RefreshButton label="Refresh operators" lastUpdatedAt={query.dataUpdatedAt} onRefresh={query.refreshNow} />
          <Button size="sm" onClick={() => setIssueOpen(true)} disabled={isReadOnly}>
            <Plus className="mr-2 h-4 w-4" aria-hidden="true" />Issue Certificate
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader><TableToolbar filters={filters} noun="operators" /></CardHeader>
        <CardContent className="space-y-4">
          {query.isError && !query.data ? (
            <ReadErrorState noun="operators" error={query.error} onRetry={() => void query.refetch()} />
          ) : (query.data?.length ?? 0) === 0 ? (
            <EmptyState icon={KeyRound} title="No operators" description="Issue a certificate to add the first one." />
          ) : (
            <>
              <Table data-testid="mini-operators-table">
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-8"><span className="sr-only">Certificates</span></TableHead>
                    <SortableTableHead label="Operator" field="operator" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Role" field="role" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Organization" field="organization" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Certificate Expires" field="expires" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Last Sign-In" field="last" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <TableHead className="w-10"><span className="sr-only">Actions</span></TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.length === 0 ? (
                    <TableEmptyRow colSpan={8} title="No operator matches the current filters" />
                  ) : rows.map((o) => {
                    const open = expanded === o.uuid;
                    return (
                      <Fragment key={o.uuid}>
                        <TableRow data-row-key={o.uuid}>
                          <TableCell>
                            <Button variant="ghost" size="icon" className="h-7 w-7" aria-expanded={open}
                              aria-label={open ? `Hide certificates of ${o.email}` : `Show certificates of ${o.email}`}
                              onClick={() => setExpanded(open ? null : o.uuid)}>
                              {open ? <ChevronDown className="h-4 w-4" aria-hidden="true" /> : <ChevronRight className="h-4 w-4" aria-hidden="true" />}
                            </Button>
                          </TableCell>
                          <TableCell className="break-all">
                            {o.display_name || o.email}
                            {o.display_name && <TableSubText>{o.email}</TableSubText>}
                          </TableCell>
                          <TableCell>{ROLE_LABEL[o.role]}</TableCell>
                          <TableCell className="break-words">{o.owner_org_name ?? <TableEmptyValue />}</TableCell>
                          <TableCell>{statusBadge(o.status)}</TableCell>
                          <TableCell className="whitespace-nowrap">
                            {o.expires_at ? formatDate(o.expires_at) : <TableEmptyValue />}
                            {o.expiring && <TableSubText>Expiring soon</TableSubText>}
                          </TableCell>
                          <TableCell className="whitespace-nowrap">{o.last_login_at ? formatDateTime(o.last_login_at) : "Never"}</TableCell>
                          <TableCell>
                            {o.manageable && !isReadOnly && (
                              <DropdownMenu>
                                <DropdownMenuTrigger asChild>
                                  <Button variant="ghost" size="icon" aria-label={`Actions for ${o.email}`} disabled={busy}>
                                    <MoreVertical className="h-4 w-4" aria-hidden="true" />
                                  </Button>
                                </DropdownMenuTrigger>
                                <DropdownMenuContent align="end">
                                  {o.status === "active" && (
                                    <DropdownMenuItem onClick={() => void reissue(o)}>
                                      <KeyRound className="mr-2 h-4 w-4" aria-hidden="true" />Issue Another Certificate
                                    </DropdownMenuItem>
                                  )}
                                  {isAdmin && o.status === "active" && (["admin", "partner", "tenant"] as OperatorRole[])
                                    .filter((r) => r !== o.role)
                                    .map((r) => (
                                      <DropdownMenuItem key={r} onClick={() => setConfirm({ kind: "role", operator: o, role: r })}>
                                        Make {ROLE_LABEL[r]}
                                      </DropdownMenuItem>
                                    ))}
                                  <DropdownMenuSeparator />
                                  {o.status === "active" && (
                                    <DropdownMenuItem className="text-destructive focus:text-destructive" onClick={() => setConfirm({ kind: "revoke", operator: o })}>Revoke</DropdownMenuItem>
                                  )}
                                  {o.status === "revoked" && (
                                    <DropdownMenuItem onClick={() => setConfirm({ kind: "reactivate", operator: o })}>Reactivate</DropdownMenuItem>
                                  )}
                                  {o.status !== "deleted" ? (
                                    <DropdownMenuItem className="text-destructive focus:text-destructive" onClick={() => setConfirm({ kind: "delete", operator: o })}>Delete</DropdownMenuItem>
                                  ) : (
                                    <DropdownMenuItem onClick={() => setConfirm({ kind: "restore", operator: o })}>Restore</DropdownMenuItem>
                                  )}
                                </DropdownMenuContent>
                              </DropdownMenu>
                            )}
                          </TableCell>
                        </TableRow>
                        {open && (
                          <TableRow className="bg-muted/30 hover:bg-muted/30">
                            <TableCell />
                            <TableCell colSpan={7}>
                              {o.certificates.length === 0 ? (
                                <p className="text-sm text-muted-foreground">No certificates.</p>
                              ) : (
                                <ul className="space-y-2" aria-label={`Certificates of ${o.email}`}>
                                  {o.certificates.map((c) => (
                                    <li key={c.fingerprint} className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                                      <code className="break-all font-mono text-xs" translate="no">{c.fingerprint}</code>
                                      <span>{c.status === "active" ? "Active" : c.status === "superseded" ? "Replaced by a renewal" : "Revoked"}</span>
                                      <span className="text-muted-foreground">Expires {c.not_after ? formatDate(c.not_after) : "never"}</span>
                                      <span className="text-muted-foreground">Last used {c.last_used_at ? formatDateTime(c.last_used_at) : "never"}</span>
                                      {c.renewed_from_fingerprint && <span className="break-all text-muted-foreground">Renews <code className="font-mono text-xs" translate="no">{c.renewed_from_fingerprint}</code></span>}
                                      {c.status === "active" && o.manageable && !isReadOnly && (
                                        <Button variant="outline" size="sm" disabled={busy}
                                          onClick={() => setConfirm({ kind: "revoke-cert", operator: o, fingerprint: c.fingerprint })}>
                                          Revoke Certificate
                                        </Button>
                                      )}
                                    </li>
                                  ))}
                                </ul>
                              )}
                            </TableCell>
                          </TableRow>
                        )}
                      </Fragment>
                    );
                  })}
                </TableBody>
              </Table>
              {totalPages > 1 && (
                <DataTablePagination
                  currentPage={currentPage} totalPages={totalPages} pageSize={pageSize}
                  pageSizeOptions={[...TABLE_PAGE_SIZES]} totalItems={sorted.length} onPageChange={setPage}
                  onPageSizeChange={(size) => { setPageSize(size); setPage(1); }}
                />
              )}
            </>
          )}
        </CardContent>
      </Card>

      <IssueDialog open={issueOpen} onOpenChange={setIssueOpen} isAdmin={isAdmin} onIssued={() => void query.refetch()} />

      <Dialog open={Boolean(reissued)} onOpenChange={(next) => { if (!next) setReissued(null); }}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Certificate Issued</DialogTitle>
            <DialogDescription>Another certificate for {reissued?.email}. Their other certificates keep working.</DialogDescription>
          </DialogHeader>
          {reissued && <BundleReveal bundle={reissued.bundle} holder={reissued.email} />}
          <DialogFooter><Button variant="outline" onClick={() => setReissued(null)}>Done</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={Boolean(confirm)} onOpenChange={(next) => { if (!next && !busy) setConfirm(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{copy?.title}</AlertDialogTitle>
            <AlertDialogDescription>
              <span className="font-medium text-foreground">{confirm?.operator.email}</span>. {copy?.body}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction disabled={busy} onClick={(event) => { event.preventDefault(); void confirmAction(); }}
              className={copy?.destructive ? "bg-destructive text-destructive-foreground hover:bg-destructive/90" : undefined}>
              {busy ? "Working…" : copy?.action}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

// Suspense: useTableFilters reads useSearchParams.
export function OperatorsView(): JSX.Element {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <OperatorsContent />
    </Suspense>
  );
}
