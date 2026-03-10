"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
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
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/hooks/use-toast";
import {
  CertificateStatus,
  CertificateTTLOption,
  TTL_LABELS,
  type CertificateInfo,
} from "@/types/api";
import {
  useCertificates,
  useInitiateCertRenewal,
  useVerifyCertRenewal,
  useCancelPendingCert,
} from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { usePermissions } from "@/hooks/use-permissions";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { formatDate } from "@/lib/utils";

const statusConfig = {
  [CertificateStatus.VALID]: {
    label: "Valid",
    variant: "default" as const,
    icon: CheckCircle,
    color: "text-green-500",
    bgColor: "bg-green-500/10",
  },
  [CertificateStatus.EXPIRING]: {
    label: "Expiring Soon",
    variant: "secondary" as const,
    icon: Clock,
    color: "text-amber-500",
    bgColor: "bg-amber-500/10",
  },
  [CertificateStatus.EXPIRED]: {
    label: "Expired",
    variant: "destructive" as const,
    icon: XCircle,
    color: "text-red-500",
    bgColor: "bg-red-500/10",
  },
  [CertificateStatus.UNKNOWN]: {
    label: "Unknown",
    variant: "outline" as const,
    icon: AlertTriangle,
    color: "text-gray-500",
    bgColor: "bg-gray-500/10",
  },
  [CertificateStatus.PENDING_RENEWAL]: {
    label: "Pending Renewal",
    variant: "secondary" as const,
    icon: RefreshCw,
    color: "text-blue-500",
    bgColor: "bg-blue-500/10",
  },
};

function getDaysDisplay(days: number | null | undefined): string {
  if (days === null || days === undefined) return "N/A";
  if (days < 0) return `Expired ${Math.abs(days)} days ago`;
  if (days === 0) return "Expires today";
  if (days === 1) return "Expires tomorrow";
  return `${days} days remaining`;
}

export default function CertificatesPage() {
  const router = useRouter();
  const { data, isLoading, refetch, isRefetching } = useCertificates();
  const certificates = data?.certificates || [];
  const { isReadOnly } = useReadOnlyMode();
  const { isPartner } = usePermissions();

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

  // redirect if not Partner+
  React.useEffect(() => {
    if (!isPartner) {
      router.replace("/");
    }
  }, [isPartner, router]);

  if (!isPartner) return null;

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
        trust_password: trustPassword || undefined,
      });

      if (result.auto_renewed) {
        toast({
          title: "Certificate Renewed",
          description: "Certificate has been automatically renewed and activated.",
        });
        setRenewalModal({ open: false, certificate: null });
        refetch();
      } else if (result.requires_manual_trust) {
        setRenewalResult({
          certificate: result.certificate,
          cli_command: result.cli_command,
          pending_cert_id: result.pending_cert_id,
        });
        setShowManualTrust(true);
        toast({
          title: "Manual Trust Required",
          description: "Please run the CLI command on your hypervisor server.",
        });
      }
    } catch (error: any) {
      toast({
        title: "Renewal Failed",
        description: error.message || "Failed to initiate certificate renewal",
        variant: "destructive",
      });
    }
  };

  const handleVerifyRenewal = async () => {
    if (!renewalModal.certificate) return;

    try {
      const result = await verifyCertRenewal.mutateAsync({
        cluster_id: renewalModal.certificate.cluster_id,
        pending_cert_id: renewalResult?.pending_cert_id,
      });

      if (result.verified && result.activated) {
        toast({
          title: "Certificate Activated",
          description: "New certificate is now active.",
        });
        setRenewalModal({ open: false, certificate: null });
        refetch();
      } else {
        toast({
          title: "Not Yet Verified",
          description: "Certificate not yet trusted. Please run the CLI command first.",
          variant: "destructive",
        });
      }
    } catch (error: any) {
      toast({
        title: "Verification Failed",
        description: error.message || "Failed to verify certificate",
        variant: "destructive",
      });
    }
  };

  const handleCancelPending = async (clusterId: string) => {
    try {
      await cancelPendingCert.mutateAsync(clusterId);
      toast({
        title: "Cancelled",
        description: "Pending certificate renewal has been cancelled.",
      });
      refetch();
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to cancel pending renewal",
        variant: "destructive",
      });
    }
  };

  const copyToClipboard = async (text: string, label: string) => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback for non-secure contexts
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "absolute";
        textarea.style.left = "-9999px";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      toast({
        title: "Copied",
        description: `${label} copied to clipboard`,
      });
    } catch (error) {
      console.error("Failed to copy:", error);
      toast({
        title: "Failed to copy",
        description: "Please copy manually",
        variant: "destructive",
      });
    }
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
          <Button
            variant="ghost"
            size="sm"
            onClick={() => router.push("/inventory/clusters")}
            className="shrink-0"
          >
            <ChevronLeft className="h-4 w-4 mr-1" />
            <span className="hidden sm:inline">Back to Clusters</span>
            <span className="sm:hidden">Back</span>
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
        <Button
          onClick={() => refetch()}
          disabled={isRefetching}
          size="sm"
          className="gap-2 shrink-0"
        >
          <RefreshCw className={`h-4 w-4 ${isRefetching ? "animate-spin" : ""}`} />
          <span className="hidden sm:inline">Refresh</span>
        </Button>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-4">
        <Card>
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium">Total Certificates</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold">{data?.total || 0}</div>
          </CardContent>
        </Card>
        <Card className="border-green-500/50">
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium text-green-600">Valid</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold text-green-600">
              {certificates.filter((c) => c.status === CertificateStatus.VALID).length}
            </div>
          </CardContent>
        </Card>
        <Card className="border-amber-500/50">
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium text-amber-600">Expiring Soon</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold text-amber-600">
              {data?.expiring_count || 0}
            </div>
          </CardContent>
        </Card>
        <Card className="border-red-500/50">
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium text-red-600">Expired</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold text-red-600">
              {data?.expired_count || 0}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Certificates Table */}
      <Card>
        <CardHeader className="p-4 md:p-6">
          <CardTitle className="text-lg md:text-xl">Cluster Certificates</CardTitle>
          <CardDescription className="text-xs md:text-sm">
            View and manage certificates for all registered clusters
          </CardDescription>
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
                  <TableHead className="min-w-[120px]">Cluster</TableHead>
                  <TableHead className="min-w-[100px]">Status</TableHead>
                  <TableHead className="min-w-[100px] hidden md:table-cell">Issuer</TableHead>
                  <TableHead className="min-w-[120px] hidden lg:table-cell">Issued</TableHead>
                  <TableHead className="min-w-[120px]">Expires</TableHead>
                  <TableHead className="min-w-[100px]">Days Left</TableHead>
                  <TableHead className="text-right min-w-[100px]">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {certificates.map((cert) => {
                  const config = statusConfig[cert.status] || statusConfig[CertificateStatus.UNKNOWN];
                  const StatusIcon = config.icon;

                  return (
                    <TableRow key={cert.cluster_id}>
                      <TableCell className="text-sm">
                        <div>
                          <div className="font-medium truncate max-w-[150px]">{cert.cluster_id}</div>
                          {(cert.endpoint || cert.common_name) && (
                            <div className="text-xs text-muted-foreground truncate max-w-[150px]">
                              {cert.endpoint || cert.common_name}
                            </div>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-col gap-1">
                          <Badge variant={config.variant} className="gap-1 w-fit">
                            <StatusIcon className="h-3 w-3" />
                            <span className="hidden sm:inline">{config.label}</span>
                          </Badge>
                          {cert.has_pending_renewal && (
                            <Badge variant="outline" className="gap-1 text-blue-500 w-fit">
                              <RefreshCw className="h-3 w-3" />
                              <span className="hidden sm:inline">Pending</span>
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
                              ? "text-red-500"
                              : (cert.days_until_expiry ?? 0) <= 14
                              ? "text-amber-500"
                              : "text-green-500"
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
                                className="text-blue-600 h-8 px-2 md:px-3"
                              >
                                <CheckCircle className="h-4 w-4 md:mr-1" />
                                <span className="hidden md:inline">Verify</span>
                              </Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => handleCancelPending(cert.cluster_id)}
                                className="text-red-600 h-8 px-2"
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
                })}
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

              {renewalModal.certificate?.status === CertificateStatus.EXPIRED && (
                <div className="space-y-2">
                  <Label>Trust Password (Optional)</Label>
                  <Input
                    type="password"
                    placeholder="Enter LXD trust password"
                    value={trustPassword}
                    onChange={(e) => setTrustPassword(e.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">
                    If not provided, you'll need to add trust manually via CLI.
                  </p>
                </div>
              )}

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
