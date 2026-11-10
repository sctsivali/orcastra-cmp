"use client";

import * as React from "react";

import { LoadingLottiePage } from "@/components/loading-lottie";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { BackLink } from "@/components/ui/back-link";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { EmptyClustersIllustration } from "@/components/illustrations/scenes/onboarding";
import { PageReadError } from "@/components/ui/read-error-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useCancelPendingCert, useCertificates } from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useRequireRole } from "@/hooks/use-require-role";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { toast } from "@/hooks/use-toast";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { CertificateStatus, type CertificateInfo } from "@/types/api";
import { Role } from "@/types/rbac";

import { CertificateSummaryCards } from "./components/certificate-summary-cards";
import { CERTIFICATES, CertificatesTable } from "./components/certificates-table";
import { RenewCertificateDialog, type RenewDialogMode } from "./components/renew-certificate-dialog";
import { TrustCleanupDialog } from "./components/trust-cleanup-dialog";

function CertificatesContent() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt, isError, error } = useCertificates();
  const certificates = React.useMemo(() => data?.certificates || [], [data]);
  const { isReadOnly } = useReadOnlyMode();

  const [renewTarget, setRenewTarget] = React.useState<{ cert: CertificateInfo; mode: RenewDialogMode } | null>(null);
  const [cancelTarget, setCancelTarget] = React.useState<CertificateInfo | null>(null);
  const [cleanupTarget, setCleanupTarget] = React.useState<CertificateInfo | null>(null);

  const cancelPendingCert = useCancelPendingCert();
  // Renew and verify run inside their modal dialog, so the row only has to wait for a cancel.
  const busy = cancelPendingCert.isPending;

  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState(TABLE_PAGE_SIZE);
  const resetPage = React.useCallback(() => setPage(1), []);

  const filters = useTableFilters(CERTIFICATES, certificates, {
    onFilterChange: resetPage,
  });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    CERTIFICATES.sortAccessors,
    CERTIFICATES.defaultSort.field,
    CERTIFICATES.defaultSort.direction,
    resetPage,
  );

  const sortedCerts = React.useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  // Clamped on read instead of corrected in an effect. A renewal drops a certificate out of
  // the Expiring facet while the operator sits on the last page, so the page count shrinks
  // underneath them. An effect would fire only after the empty frame had already painted.
  const totalPages = Math.max(1, Math.ceil(sortedCerts.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedCerts = sortedCerts.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // The summary cards and the Status facet render one piece of state twice; neither keeps its own.
  const statusSelected = filters.selectedValues("status");

  // A row's object is replaced on every refetch; the dialog follows the fresh one by id.
  const liveCleanupTarget = React.useMemo(
    () => (cleanupTarget ? certificates.find((c) => c.cluster_id === cleanupTarget.cluster_id) ?? cleanupTarget : null),
    [certificates, cleanupTarget],
  );

  const cancelInFlight = React.useRef(false);
  const handleCancelPending = async () => {
    if (!cancelTarget || cancelPendingCert.isPending || cancelInFlight.current) return;
    cancelInFlight.current = true;
    try {
      await cancelPendingCert.mutateAsync(cancelTarget.cluster_id);
      toast({
        title: "Certificate renewal cancelled",
        description: "Pending certificate renewal has been cancelled." });
      setCancelTarget(null);
      refetch();
    } catch (error) {
      toast({
        title: "Could not cancel certificate renewal",
        description: error instanceof Error && error.message ? error.message : "Try again.",
        variant: "destructive" });
    } finally {
      cancelInFlight.current = false;
    }
  };

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  // A failed read is not an empty list. Rendered before the table so the filtered-empty row
  // cannot blame a filter for a request that did not return.
  if (isError && !data) {
    return (
      <PageReadError
        title="Certificate Manager"
        noun="certificates"
        error={error}
        onRetry={() => void refetch()}
      />
    );
  }

  return (
    <div className="p-4 md:p-8 space-y-4 md:space-y-6">
      {isReadOnly && <ReadOnlyBanner />}

      {/* The heading and the refresh control share a row only from lg: at md the sidebar leaves the
          header 448px and the row would run 450px past the page. */}
      <div className="flex flex-col lg:flex-row items-start lg:items-center justify-between gap-4">
        <div className="flex flex-col sm:flex-row items-start sm:items-center gap-2 sm:gap-4">
          <BackLink href="/inventory/clusters" label="Clusters" />
          <div className="min-w-0">
            <h1 className="text-2xl md:text-3xl font-bold">
              <span className="truncate">Certificate Manager</span>
            </h1>
            <p className="text-sm text-muted-foreground mt-1 line-clamp-2">
              Manage SSL/TLS certificates for cluster connections
            </p>
          </div>
        </div>
        <RefreshButton
          label="Refresh certificates"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <CertificateSummaryCards
        total={data?.total || 0}
        valid={certificates.filter((c) => c.status === CertificateStatus.VALID).length}
        expiring={data?.expiring_count || 0}
        expired={data?.expired_count || 0}
        selected={statusSelected}
        onToggle={(status) => filters.toggleFacetValue("status", status)}
        onClear={() => filters.setFacet("status", [])}
      />

      <Card>
        <CardHeader className="p-4 md:p-6 space-y-4">
          <CardTitle className="text-lg md:text-xl">Cluster Certificates</CardTitle>
          <CardDescription className="text-xs md:text-sm">
            View and manage certificates for all registered clusters. The fingerprint matches the
            FINGERPRINT column of lxc config trust list on LXD and incus config trust list on Incus.
          </CardDescription>
          <TableToolbar filters={filters} noun="certificates" />
        </CardHeader>
        <CardContent className="p-0 md:p-6 md:pt-0">
          {certificates.length === 0 ? (
            <EmptyState illustration={EmptyClustersIllustration} title="No certificates found" description="Register a cluster first." />
          ) : (
            <>
            <CertificatesTable
              rows={pagedCerts}
              matched={sortedCerts.length}
              total={certificates.length}
              activeFilterCount={filters.activeFilterCount}
              onClearFilters={() => filters.clearAll()}
              sortField={sortField}
              sortDirection={sortDirection}
              onSort={handleSort}
              isReadOnly={isReadOnly}
              busy={busy}
              onRenew={(c) => setRenewTarget({ cert: c, mode: "renew" })}
              onVerify={(c) => setRenewTarget({ cert: c, mode: "verify" })}
              onCancel={setCancelTarget}
              onShowSuperseded={setCleanupTarget}
            />

            {/* Absent rather than disabled when everything fits on one page. Outside the
                horizontal scroller so it does not slide away with a wide table. */}
            {totalPages > 1 && (
              <div className="mt-4">
                <DataTablePagination
                  currentPage={currentPage}
                  totalPages={totalPages}
                  pageSize={pageSize}
                  pageSizeOptions={[...TABLE_PAGE_SIZES]}
                  totalItems={sortedCerts.length}
                  onPageChange={setPage}
                  onPageSizeChange={(size) => {
                    setPageSize(size);
                    setPage(1);
                  }}
                />
              </div>
            )}
            </>
          )}
        </CardContent>
      </Card>

      <RenewCertificateDialog
        certificate={renewTarget?.cert ?? null}
        mode={renewTarget?.mode ?? null}
        onClose={() => {
          setRenewTarget(null);
          refetch();
        }}
      />

      <TrustCleanupDialog
        certificate={liveCleanupTarget}
        isReadOnly={isReadOnly}
        onClose={() => setCleanupTarget(null)}
      />

      <DestructiveConfirmDialog
        open={!!cancelTarget}
        onOpenChange={(open) => !open && setCancelTarget(null)}
        title="Cancel pending renewal?"
        description={
          <>
            The pending certificate for <span className="font-medium">{cancelTarget?.cluster_id}</span> is
            discarded and the current one stays active. If its trust was already added on the
            cluster, that entry is removed automatically later.
          </>
        }
        confirmLabel="Cancel renewal"
        pendingLabel="Cancelling…"
        pending={cancelPendingCert.isPending}
        onConfirm={() => void handleCancelPending()}
      />
    </div>
  );
}

// The role gate stays in this file: check-role-gates.mjs reads page text and does not follow
// imports. Suspense is required because useTableFilters reads useSearchParams.
export default function CertificatesPage() {
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  return (
    <React.Suspense fallback={<LoadingLottiePage />}>
      <CertificatesContent />
    </React.Suspense>
  );
}
