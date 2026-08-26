"use client";

import * as React from "react";
import { IntentLink } from "@/components/navigation/route-intent";
import { LoadingLottiePage } from "@/components/loading-lottie";
import {
  CheckCircle,
  XCircle,
  Clock,
  AlertTriangle,
  RefreshCw,
  KeyRound,
  Copy,
  Terminal,
  ChevronLeft,
  ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow } from "@/components/ui/table";
import { TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import { OverflowText } from "@/components/ui/overflow-text";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter } from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { certificateStatusMeta } from "./components/certificate-status";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/hooks/use-toast";
import {
  CertificateStatus,
  CertificateTTLOption,
  TTL_LABELS,
  type CertificateInfo } from "@/types/api";
import {
  useCertificates,
  useInitiateCertRenewal,
  useVerifyCertRenewal,
  useCancelPendingCert } from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useRequireRole } from "@/hooks/use-require-role";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { Role } from "@/types/rbac";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { formatDate, formatLabel } from "@/lib/utils";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { copyText } from "@/lib/clipboard";

function getDaysDisplay(days: number | null | undefined): string {
  if (days === null || days === undefined) return "N/A";
  if (days < 0) return `Expired ${Math.abs(days)} days ago`;
  if (days === 0) return "Expires today";
  if (days === 1) return "Expires tomorrow";
  return `${days} days remaining`;
}

const CERTIFICATES = defineTable<CertificateInfo>()({
  searchPlaceholder: "Search certificates",
  defaultSort: { field: "expires", direction: "asc" },
  columns: [
    {
      field: "cluster",
      label: "Cluster",
      sort: (c) => c.cluster_id,
      search: (c) => c.cluster_id,
      // The only cluster-scoped table that could not be filtered by cluster, while both
      // sibling tables on the warnings page facet the same dimension.
      //
      // note: this facets `cluster_id` where warnings facets `display_name || cluster_id`,
      // because CertificateInfo carries no display_name. Joining a second query inside a
      // facet accessor is the pattern lib/used-by.ts argues against, so the option labels
      // differ between sibling pages and that is deliberate.
      facet: (c) => c.cluster_id,
      facetSpec: { role: "node" } },
    {
      field: "status",
      label: "Status",
      sort: (c) => c.status,
      facet: (c) => c.status,
      // note: the enum carries five states while the summary cards show three, so Total is
      // not the sum of the cards. The facet lists all five; the cards stay at the three an
      // operator acts on.
      facetSpec: { primary: true, format: formatLabel } },
    {
      field: "issuer",
      label: "Issuer",
      sort: (c) => c.issuer,
      search: (c) => c.issuer,
      facet: (c) => c.issuer,
      facetSpec: { emptyLabel: "(no issuer)" } },
    { field: "issued", label: "Issued", sort: (c) => certToTime(c.issued_at), filter: fromSort("date", { horizon: "past" }) },
    { field: "expires", label: "Expires", sort: (c) => certToTime(c.expires_at), filter: fromSort("date", { horizon: "future" }) },
    { field: "days", label: "Days Left", sort: (c) => c.days_until_expiry, filter: fromSort("number", { unit: "days" }) },
  ] });

function certToTime(value: string | null | undefined): number | null {
  if (!value) return null;
  const t = new Date(value).getTime();
  return Number.isNaN(t) ? null : t;
}

function CertificatesContent() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useCertificates();
  const certificates = data?.certificates || [];
  const { isReadOnly } = useReadOnlyMode();

  // Renewal modal state
  const [renewalModal, setRenewalModal] = React.useState<{
    open: boolean;
    certificate: CertificateInfo | null;
  }>({ open: false, certificate: null });
  const [selectedTTL, setSelectedTTL] = React.useState<CertificateTTLOption>(
    CertificateTTLOption.SIX_MONTHS
  );
  const [trustPassword, setTrustPassword] = React.useState("");
  const [showManualTrust, setShowManualTrust] = React.useState(false);
  const [renewalResult, setRenewalResult] = React.useState<{
    certificate?: string;
    cli_command?: string;
    pending_cert_id?: string;
  } | null>(null);

  // Verify modal state
  const [verifyModal, setVerifyModal] = React.useState<{
    open: boolean;
    clusterId: string;
    pendingCertId?: string;
  }>({ open: false, clusterId: "", pendingCertId: undefined });

  const initiateCertRenewal = useInitiateCertRenewal();
  const verifyCertRenewal = useVerifyCertRenewal();
  const cancelPendingCert = useCancelPendingCert();

  const filters = useTableFilters(CERTIFICATES, certificates);
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    CERTIFICATES.sortAccessors,
    CERTIFICATES.defaultSort.field,
    CERTIFICATES.defaultSort.direction,
  );

  const visibleCerts = sortItems(filters.filteredRows);

  // The summary cards and the Status facet are one piece of state rendered twice, not two
  // controls. Two controls for one state is the failure this whole change exists to remove.
  const statusSelected = filters.selectedValues("status");
  const toggleStatus = (value: string) => filters.toggleFacetValue("status", value);

  const handleRenewClick = (cert: CertificateInfo) => {
    setRenewalModal({ open: true, certificate: cert });
    setRenewalResult(null);
    setShowManualTrust(false);
    setTrustPassword("");
    // Pre-select TTL based on current status
    if (cert.status === CertificateStatus.EXPIRED) {
      setSelectedTTL(CertificateTTLOption.SIX_MONTHS);
    }
  };

  const handleInitiateRenewal = async () => {
    if (!renewalModal.certificate) return;

    try {
      const result = await initiateCertRenewal.mutateAsync({
        cluster_id: renewalModal.certificate.cluster_id,
        ttl: selectedTTL,
        trust_password: trustPassword || undefined });

      if (result.auto_renewed) {
        toast({
          title: "Certificate Renewed",
          description: "Certificate has been automatically renewed and activated." });
        setRenewalModal({ open: false, certificate: null });
        refetch();
      } else if (result.requires_manual_trust) {
        setRenewalResult({
          certificate: result.certificate,
          cli_command: result.cli_command,
          pending_cert_id: result.pending_cert_id });
        setShowManualTrust(true);
        toast({
          title: "Manual Trust Required",
          description: "Please run the CLI command on your hypervisor server." });
      }
    } catch (error: any) {
      toast({
        title: "Renewal Failed",
        description: error.message || "Failed to initiate certificate renewal",
        variant: "destructive" });
    }
  };

  const handleVerifyRenewal = async () => {
    if (!renewalModal.certificate) return;

    try {
      const result = await verifyCertRenewal.mutateAsync({
        cluster_id: renewalModal.certificate.cluster_id,
        pending_cert_id: renewalResult?.pending_cert_id });

      if (result.verified && result.activated) {
        toast({
          title: "Certificate Activated",
          description: "New certificate is now active." });
        setRenewalModal({ open: false, certificate: null });
        refetch();
      } else {
        toast({
          title: "Not Yet Verified",
          description: "Certificate not yet trusted. Please run the CLI command first.",
          variant: "destructive" });
      }
    } catch (error: any) {
      toast({
        title: "Verification Failed",
        description: error.message || "Failed to verify certificate",
        variant: "destructive" });
    }
  };

  const handleCancelPending = async (clusterId: string) => {
    try {
      await cancelPendingCert.mutateAsync(clusterId);
      toast({
        title: "Cancelled",
        description: "Pending certificate renewal has been cancelled." });
      refetch();
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to cancel pending renewal",
        variant: "destructive" });
    }
  };

  const copyToClipboard = async (text: string, label: string) => {
    const ok = await copyText(text);
    toast(
      ok
        ? { title: "Copied", description: `${label} copied to clipboard` }
        : {
            title: "Failed to copy",
            description: "Please copy manually",
            variant: "destructive" },
    );
  };

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-4 md:p-8 space-y-4 md:space-y-6">
      {isReadOnly && <ReadOnlyBanner />}

      {/* Header */}
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div className="flex flex-col sm:flex-row items-start sm:items-center gap-2 sm:gap-4">
          <Button asChild variant="ghost" size="sm" className="shrink-0">
            <IntentLink href="/inventory/clusters">
              <ChevronLeft className="h-4 w-4 mr-1" />
              <span className="hidden sm:inline">Back to Clusters</span>
              <span className="sm:hidden">Back</span>
            </IntentLink>
          </Button>
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

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-4">
        {/* Total is neither a bucket nor the sum of the three beside it: the status enum
            carries five states and the cards show three. It clears the status filter. */}
        <button
          type="button"
          aria-pressed={statusSelected.length === 0}
          onClick={() => filters.setFacet("status", [])}
          className="text-left rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Card className={`h-full ${statusSelected.length === 0 ? "ring-2 ring-ring" : ""}`}>
            <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
              <CardTitle className="text-xs md:text-sm font-medium">Total Certificates</CardTitle>
            </CardHeader>
            <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
              <div className="text-xl md:text-2xl font-bold">{data?.total || 0}</div>
            </CardContent>
          </Card>
        </button>
        <button
          type="button"
          aria-pressed={statusSelected.includes(CertificateStatus.VALID)}
          onClick={() => toggleStatus(CertificateStatus.VALID)}
          className="text-left rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Card className={`border-status-online/50 h-full ${statusSelected.includes(CertificateStatus.VALID) ? "ring-2 ring-ring" : ""}`}>
            <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
              <CardTitle className="text-xs md:text-sm font-medium text-status-online">Valid</CardTitle>
            </CardHeader>
            <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
              <div className="text-xl md:text-2xl font-bold text-status-online">
                {certificates.filter((c) => c.status === CertificateStatus.VALID).length}
              </div>
            </CardContent>
          </Card>
        </button>
        <button
          type="button"
          aria-pressed={statusSelected.includes(CertificateStatus.EXPIRING)}
          onClick={() => toggleStatus(CertificateStatus.EXPIRING)}
          className="text-left rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Card className={`border-status-pending/50 h-full ${statusSelected.includes(CertificateStatus.EXPIRING) ? "ring-2 ring-ring" : ""}`}>
            <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
              <CardTitle className="text-xs md:text-sm font-medium text-status-pending">Expiring Soon</CardTitle>
            </CardHeader>
            <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
              <div className="text-xl md:text-2xl font-bold text-status-pending">
                {data?.expiring_count || 0}
              </div>
            </CardContent>
          </Card>
        </button>
        <button
          type="button"
          aria-pressed={statusSelected.includes(CertificateStatus.EXPIRED)}
          onClick={() => toggleStatus(CertificateStatus.EXPIRED)}
          className="text-left rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Card className={`border-status-offline/50 h-full ${statusSelected.includes(CertificateStatus.EXPIRED) ? "ring-2 ring-ring" : ""}`}>
            <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
              <CardTitle className="text-xs md:text-sm font-medium text-status-offline">Expired</CardTitle>
            </CardHeader>
            <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
              <div className="text-xl md:text-2xl font-bold text-status-offline">
                {data?.expired_count || 0}
              </div>
            </CardContent>
          </Card>
        </button>
      </div>

      {/* Certificates Table */}
      <Card>
        <CardHeader className="p-4 md:p-6 space-y-4">
          <CardTitle className="text-lg md:text-xl">Cluster Certificates</CardTitle>
          <CardDescription className="text-xs md:text-sm">
            View and manage certificates for all registered clusters
          </CardDescription>
          <TableToolbar filters={filters} noun="certificates" />
        </CardHeader>
        <CardContent className="p-0 md:p-6 md:pt-0">
          {certificates.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              No certificates found. Register a cluster first.
            </div>
          ) : (
            <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead label="Cluster" field="cluster" currentField={sortField} direction={sortDirection} onSort={handleSort} className="min-w-[120px]" />
                  <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} className="min-w-[100px]" />
                  <SortableTableHead label="Issuer" field="issuer" currentField={sortField} direction={sortDirection} onSort={handleSort} className="min-w-[100px] hidden md:table-cell" />
                  <SortableTableHead label="Issued" field="issued" currentField={sortField} direction={sortDirection} onSort={handleSort} className="min-w-[120px] hidden lg:table-cell" />
                  <SortableTableHead label="Expires" field="expires" currentField={sortField} direction={sortDirection} onSort={handleSort} className="min-w-[120px]" />
                  <SortableTableHead label="Days Left" field="days" currentField={sortField} direction={sortDirection} onSort={handleSort} className="min-w-[100px]" />
                  <TableHead className="text-right min-w-[100px]">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleCerts.length === 0 ? (
                  <TableEmptyRow
                    colSpan={7}
                    icon={ShieldCheck}
                    title="No certificates found"
                    description={
                      filters.activeFilterCount > 0
                        ? "No certificate matches the current filters."
                        : undefined
                    }
                  />
                ) : (
                  visibleCerts.map((cert) => {
                  const config = certificateStatusMeta(cert.status);
                  const StatusIcon = config.icon;

                  return (
                    <TableRow key={cert.cluster_id}>
                      <TableCell className="max-w-[16rem]">
                        <div>
                          <div className="font-medium">
                            <OverflowText value={cert.cluster_id} label="cluster id" />
                          </div>
                          {(cert.endpoint || cert.common_name) && (
                            <TableSubText wrap>
                              <OverflowText
                                value={cert.endpoint || cert.common_name || ""}
                                label={`endpoint for cluster ${cert.cluster_id}`}
                              />
                            </TableSubText>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-col gap-1">
                          <Badge variant={config.tone} className="gap-1 w-fit">
                            <StatusIcon className="h-3 w-3" />
                            {config.label}
                          </Badge>
                          {cert.has_pending_renewal && (
                            <Badge variant="outline" className="gap-1 w-fit">
                              <RefreshCw className="h-3 w-3" />
                              Pending
                            </Badge>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="hidden md:table-cell">
                        <span className="text-sm text-muted-foreground">
                          {cert.issuer || "N/A"}
                        </span>
                      </TableCell>
                      <TableCell className="hidden lg:table-cell">
                        <span className="text-sm whitespace-nowrap">
                          {formatDate(cert.issued_at)}
                        </span>
                      </TableCell>
                      <TableCell>
                        <span className="text-sm whitespace-nowrap">
                          {formatDate(cert.expires_at)}
                        </span>
                      </TableCell>
                      <TableCell>
                        <span
                          className={`text-xs md:text-sm font-medium whitespace-nowrap ${
                            (cert.days_until_expiry ?? 0) <= 0
                              ? "text-status-offline"
                              : (cert.days_until_expiry ?? 0) <= 14
                              ? "text-status-pending"
                              : "text-status-online"
                          }`}
                        >
                          {getDaysDisplay(cert.days_until_expiry)}
                        </span>
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-1 md:gap-2">
                          {cert.has_pending_renewal ? (
                            <>
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() => {
                                  setRenewalModal({ open: true, certificate: cert });
                                  setShowManualTrust(true);
                                }}
                                className="h-8 px-2 md:px-3"
                              >
                                <CheckCircle className="h-4 w-4 md:mr-1" />
                                <span className="hidden md:inline">Verify</span>
                              </Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => handleCancelPending(cert.cluster_id)}
                                className="text-status-offline h-8 px-2"
                              >
                                <XCircle className="h-4 w-4" />
                              </Button>
                            </>
                          ) : (
                            <Button
                              variant={
                                cert.status === CertificateStatus.EXPIRED
                                  ? "destructive"
                                  : cert.status === CertificateStatus.EXPIRING
                                  ? "secondary"
                                  : "outline"
                              }
                              size="sm"
                              onClick={() => handleRenewClick(cert)}
                              disabled={isReadOnly}
                              className="h-8 px-2 md:px-3"
                            >
                              <KeyRound className="h-4 w-4 md:mr-1" />
                              <span className="hidden md:inline">Renew</span>
                            </Button>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
              </TableBody>
            </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Renewal Modal */}
      <Dialog
        open={renewalModal.open}
        onOpenChange={(open) => {
          if (!open) {
            setRenewalModal({ open: false, certificate: null });
            setRenewalResult(null);
            setShowManualTrust(false);
          }
        }}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {showManualTrust ? "Complete Certificate Renewal" : "Renew Certificate"}
            </DialogTitle>
            <DialogDescription>
              {renewalModal.certificate?.cluster_id}
            </DialogDescription>
          </DialogHeader>

          {!showManualTrust ? (
            <div className="space-y-4">
              {renewalModal.certificate?.status === CertificateStatus.EXPIRED && (
                <Alert variant="destructive">
                  <AlertTriangle className="h-4 w-4" />
                  <AlertDescription>
                    This certificate is expired. You may need to provide a trust password
                    or add trust manually via CLI.
                  </AlertDescription>
                </Alert>
              )}

              <div className="space-y-2">
                <Label>Certificate Validity Period</Label>
                <Select
                  value={selectedTTL}
                  onValueChange={(v) => setSelectedTTL(v as CertificateTTLOption)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {Object.entries(TTL_LABELS).map(([value, label]) => (
                      <SelectItem key={value} value={value}>
                        {label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              {/* Offered for every renewal, not only an expired certificate. A certificate can be
                  valid and still rejected by the cluster, which is what happens once its trust
                  entry is replaced or dropped on the LXD side. In that state the backend cannot
                  authenticate with the existing certificate, so the trust password is the only
                  way to re-add trust without shell access to the host. */}
              <div className="space-y-2">
                <Label>Trust Password (Optional)</Label>
                <Input
                  type="password"
                  placeholder="Enter cluster trust password"
                  value={trustPassword}
                  onChange={(e) => setTrustPassword(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  Used when the cluster no longer trusts the current certificate. If not
                  provided, you will need to add trust manually via CLI.
                </p>
              </div>

              <DialogFooter>
                <Button
                  variant="outline"
                  onClick={() => setRenewalModal({ open: false, certificate: null })}
                >
                  Cancel
                </Button>
                <Button
                  onClick={handleInitiateRenewal}
                  disabled={initiateCertRenewal.isPending}
                >
                  {initiateCertRenewal.isPending ? (
                    <>
                      <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                      Renewing...
                    </>
                  ) : (
                    "Renew Certificate"
                  )}
                </Button>
              </DialogFooter>
            </div>
          ) : (
            <div className="space-y-4">
              <Alert>
                <Terminal className="h-4 w-4" />
                <AlertDescription>
                  Run the following command on your hypervisor server to add trust for the new certificate.
                </AlertDescription>
              </Alert>

              {renewalResult?.cli_command && (
                <div className="space-y-2">
                  <Label>CLI Command</Label>
                  <div className="relative">
                    <Textarea
                      readOnly
                      value={renewalResult.cli_command}
                      className="font-mono text-xs h-20"
                    />
                    <Button
                      variant="ghost"
                      size="sm"
                      className="absolute top-2 right-2"
                      onClick={() =>
                        copyToClipboard(renewalResult.cli_command!, "Command")
                      }
                    >
                      <Copy className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
              )}

              {renewalResult?.certificate && (
                <div className="space-y-2">
                  <Label>Certificate (PEM)</Label>
                  <div className="relative">
                    <Textarea
                      readOnly
                      value={renewalResult.certificate}
                      className="font-mono text-xs h-32"
                    />
                    <Button
                      variant="ghost"
                      size="sm"
                      className="absolute top-2 right-2"
                      onClick={() =>
                        copyToClipboard(renewalResult.certificate!, "Certificate")
                      }
                    >
                      <Copy className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
              )}

              <DialogFooter className="gap-2">
                <Button
                  variant="outline"
                  onClick={() => {
                    setRenewalModal({ open: false, certificate: null });
                    setRenewalResult(null);
                    setShowManualTrust(false);
                  }}
                >
                  Close
                </Button>
                <Button
                  onClick={handleVerifyRenewal}
                  disabled={verifyCertRenewal.isPending}
                >
                  {verifyCertRenewal.isPending ? (
                    <>
                      <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                      Verifying...
                    </>
                  ) : (
                    "Verify & Activate"
                  )}
                </Button>
              </DialogFooter>
            </div>
          )}
        </DialogContent>
      </Dialog>
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
