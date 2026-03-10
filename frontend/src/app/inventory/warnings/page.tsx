"use client";

import { useState, useMemo, useEffect, Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { AlertTriangle, RotateCw, X, Info, Filter, Flame, ShieldAlert, HelpCircle, Server, FolderOpen, Shield, KeyRound, Wifi, WifiOff } from "lucide-react";
import { useWarnings, useSystemWarnings } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { usePermissions } from "@/hooks/use-permissions";
import { formatDateTime } from "@/lib/utils";

// Separate component that uses useSearchParams
function WarningsContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { data, isLoading, refetch, isRefetching } = useWarnings();
  const { data: systemWarnings, isLoading: isLoadingSystem, refetch: refetchSystem } = useSystemWarnings();
  const allWarnings = (data as any)?.warnings || [];
  
  // Certificate and node warnings from system warnings
  const certWarnings = systemWarnings?.certificate_warnings || [];
  const nodeWarnings = systemWarnings?.node_warnings || [];
  
  // Active tab - read from URL params
  const initialTab = searchParams.get("tab") || "all";
  const [activeTab, setActiveTab] = useState(initialTab);
  
  // Get severity and node filter from URL
  const initialSeverity = searchParams.get("severity") || "all";
  const initialNode = searchParams.get("node") || "";
  const [severityFilter, setSeverityFilter] = useState(initialSeverity);
  const [nodeFilter, setNodeFilter] = useState(initialNode);
  
  // Update filter and tab when URL changes
  useEffect(() => {
    const urlSeverity = searchParams.get("severity") || "all";
    const urlTab = searchParams.get("tab") || "all";
    const urlNode = searchParams.get("node") || "";
    setSeverityFilter(urlSeverity);
    setActiveTab(urlTab);
    setNodeFilter(urlNode);
  }, [searchParams]);
  
  // Filter warnings by severity and node
  const warnings = useMemo(() => {
    let filtered = allWarnings;
    
    // filter by node
    if (nodeFilter) {
      filtered = filtered.filter((w: any) => {
        const warningNode = w.location || w.source_node || w.host || "";
        return warningNode === nodeFilter || warningNode.includes(nodeFilter);
      });
    }
    
    // filter by severity
    if (severityFilter !== "all") {
      filtered = filtered.filter((w: any) => {
        const sev = (w.severity || "").toLowerCase();
        switch (severityFilter) {
          case "critical": return sev === "critical" || sev === "error";
          case "high": return sev === "high";
          case "warning": return sev === "warning" || sev === "medium";
          case "low": return sev === "low" || sev === "info";
          default: return true;
        }
      });
    }
    
    return filtered;
  }, [allWarnings, severityFilter, nodeFilter]);
  
  // Clear node filter and update URL
  const clearNodeFilter = () => {
    setNodeFilter("");
    const params = new URLSearchParams(searchParams.toString());
    params.delete("node");
    router.replace(`/inventory/warnings?${params.toString()}`);
  };
  
  // State for warning detail modal
  const [selectedWarning, setSelectedWarning] = useState<any>(null);
  const [detailModalOpen, setDetailModalOpen] = useState(false);

  const handleShowWarningDetail = (warning: any) => {
    setSelectedWarning(warning);
    setDetailModalOpen(true);
  };

  const getSeverityColor = (severity: string) => {
    switch (severity.toLowerCase()) {
      case "high":
      case "critical":
        return "destructive";
      case "medium":
      case "warning":
        return "default";
      case "low":
      case "info":
        return "secondary";
      default:
        return "outline";
    }
  };
  
  const getSeverityIcon = (severity: string) => {
    switch (severity.toLowerCase()) {
      case "high":
      case "critical":
        return <Flame className="h-4 w-4" />;
      case "medium":
      case "warning":
        return <AlertTriangle className="h-4 w-4" />;
      default:
        return <Info className="h-4 w-4" />;
    }
  };

  if (isLoading || isLoadingSystem) {
    return <LoadingLottiePage />;
  }
  
  const handleRefreshAll = () => {
    refetch();
    refetchSystem();
  };

  return (
    <div className="p-4 md:p-8 space-y-4 md:space-y-6">
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl md:text-3xl font-bold">
            <span className="truncate">Warnings</span>
          </h1>
          <p className="text-sm text-muted-foreground mt-1 md:mt-2 line-clamp-2">
            View hypervisor system warnings and alerts
          </p>
        </div>
        <Button onClick={handleRefreshAll} disabled={isRefetching} size="sm" className="gap-2 shrink-0">
          <RotateCw className={`h-4 w-4 ${isRefetching ? "animate-spin" : ""}`} />
          <span className="hidden sm:inline">Refresh</span>
        </Button>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-4">
        <Card>
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium">Total Warnings</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold">
              {allWarnings.length + certWarnings.length + nodeWarnings.length}
            </div>
          </CardContent>
        </Card>
        <Card className="border-red-500/50">
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium text-red-600 dark:text-red-400">Critical</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold text-red-600 dark:text-red-400">
              {systemWarnings?.critical || 0}
            </div>
          </CardContent>
        </Card>
        <Card className="border-amber-500/50">
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium text-amber-600 dark:text-amber-400">Certificate Issues</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold text-amber-600 dark:text-amber-400">
              {certWarnings.length}
            </div>
          </CardContent>
        </Card>
        <Card className="border-purple-500/50">
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium text-purple-600 dark:text-purple-400">Offline Nodes</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold text-purple-600 dark:text-purple-400">
              {nodeWarnings.length}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Tabbed Content */}
      <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-4">
        <TabsList className="h-auto flex-wrap">
          <TabsTrigger value="all" className="gap-1 md:gap-2 text-xs md:text-sm">
            <AlertTriangle className="h-3 w-3 md:h-4 md:w-4" />
            <span className="hidden sm:inline">LXD Warnings</span>
            <span className="sm:hidden">LXD</span>
            <span className="text-[10px] md:text-xs">({allWarnings.length})</span>
          </TabsTrigger>
          <TabsTrigger value="certificates" className="gap-1 md:gap-2 text-xs md:text-sm">
            <Shield className="h-3 w-3 md:h-4 md:w-4" />
            <span className="hidden sm:inline">Certificates</span>
            <span className="sm:hidden">Certs</span>
            <span className="text-[10px] md:text-xs">({certWarnings.length})</span>
          </TabsTrigger>
          <TabsTrigger value="nodes" className="gap-1 md:gap-2 text-xs md:text-sm">
            <WifiOff className="h-3 w-3 md:h-4 md:w-4" />
            <span className="hidden sm:inline">Offline Nodes</span>
            <span className="sm:hidden">Offline</span>
            <span className="text-[10px] md:text-xs">({nodeWarnings.length})</span>
          </TabsTrigger>
        </TabsList>

        {/* LXD Warnings Tab */}
        <TabsContent value="all">
          <Card>
        <CardHeader className="flex flex-col md:flex-row md:items-center justify-between space-y-2 md:space-y-0 pb-4 p-4 md:p-6">
          <div className="min-w-0">
            <CardTitle className="text-base md:text-lg">System Warnings ({warnings.length}{severityFilter !== "all" || nodeFilter ? ` of ${allWarnings.length}` : ""})</CardTitle>
            <CardDescription className="mt-1 text-xs md:text-sm">
              {nodeFilter ? (
                <span className="flex items-center gap-1 flex-wrap">
                  Filtered by node: 
                  <Badge variant="outline" className="gap-1 text-xs">
                    <Server className="h-3 w-3" />
                    {nodeFilter}
                    <button onClick={clearNodeFilter} className="ml-1 hover:text-destructive">
                      <X className="h-3 w-3" />
                    </button>
                  </Badge>
                </span>
              ) : "Click on any row to view full details"}
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Select value={severityFilter} onValueChange={setSeverityFilter}>
              <SelectTrigger className="w-[120px] md:w-[140px] text-xs md:text-sm">
                <SelectValue placeholder="Filter severity" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Severities</SelectItem>
                <SelectItem value="critical">
                  <span className="flex items-center gap-2">
                    <span className="w-2 h-2 bg-red-500 rounded-full" />
                    Critical
                  </span>
                </SelectItem>
                <SelectItem value="high">
                  <span className="flex items-center gap-2">
                    <span className="w-2 h-2 bg-orange-500 rounded-full" />
                    High/Major
                  </span>
                </SelectItem>
                <SelectItem value="warning">
                  <span className="flex items-center gap-2">
                    <span className="w-2 h-2 bg-yellow-500 rounded-full" />
                    Warning
                  </span>
                </SelectItem>
                <SelectItem value="low">
                  <span className="flex items-center gap-2">
                    <span className="w-2 h-2 bg-blue-500 rounded-full" />
                    Low/Info
                  </span>
                </SelectItem>
              </SelectContent>
            </Select>
            {severityFilter !== "all" && (
              <Button 
                variant="ghost" 
                size="sm" 
                onClick={() => setSeverityFilter("all")}
                className="h-8 px-2"
              >
                <X className="h-4 w-4" />
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent className="p-0 md:p-6 md:pt-0">
          {warnings.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              {severityFilter !== "all" 
                ? `No ${severityFilter} warnings found` 
                : "No warnings found"}
            </div>
          ) : (
            <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="min-w-[100px]">Type</TableHead>
                  <TableHead className="min-w-[200px]">Message</TableHead>
                  <TableHead className="min-w-[80px]">Severity</TableHead>
                  <TableHead className="min-w-[100px]">Node</TableHead>
                  <TableHead className="min-w-[60px]">
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="flex items-center gap-1 cursor-help">
                            Count
                            <HelpCircle className="h-3 w-3 text-muted-foreground" />
                          </span>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="z-50">
                          <p>Number of times this warning has occurred</p>
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                  </TableHead>
                  <TableHead className="min-w-[150px]">Last Seen</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {warnings.map((warning: any) => (
                  <TableRow 
                    key={warning.uuid}
                    className="cursor-pointer hover:bg-accent/50 transition-colors"
                    onClick={() => handleShowWarningDetail(warning)}
                  >
                    <TableCell>
                      <Badge variant="outline">{warning.type}</Badge>
                    </TableCell>
                    <TableCell className="max-w-md truncate">
                      {warning.last_message}
                    </TableCell>
                    <TableCell>
                      <Badge variant={getSeverityColor(warning.severity)} className="flex items-center gap-1 w-fit">
                        {getSeverityIcon(warning.severity)}
                        {warning.severity}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <span className="font-medium">{warning.location || warning.source_node || "N/A"}</span>
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">{warning.count.toLocaleString()}</Badge>
                    </TableCell>
                    <TableCell className="text-xs md:text-sm whitespace-nowrap">
                      {formatDateTime(warning.last_seen_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            </div>
          )}
        </CardContent>
      </Card>
        </TabsContent>

        {/* Certificate Warnings Tab */}
        <TabsContent value="certificates">
          <Card>
            <CardHeader className="p-4 md:p-6">
              <CardTitle className="text-base md:text-lg">
                Certificate Issues ({certWarnings.length})
              </CardTitle>
              <CardDescription className="text-xs md:text-sm">
                Certificates expiring soon or already expired
              </CardDescription>
            </CardHeader>
            <CardContent className="p-0 md:p-6 md:pt-0">
              {certWarnings.length === 0 ? (
                <div className="text-center py-8 text-muted-foreground">
                  <Shield className="h-12 w-12 mx-auto mb-4 opacity-50" />
                  <p>No certificate issues found</p>
                  <p className="text-sm mt-1">All certificates are healthy</p>
                </div>
              ) : (
                <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Cluster</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Severity</TableHead>
                      <TableHead>Days Until Expiry</TableHead>
                      <TableHead>Expires At</TableHead>
                      <TableHead>Action</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {certWarnings.map((warning: any) => {
                      const isExpired = warning.warning_type === "cert_expired" || warning.days_until_expiry <= 0;
                      const statusLabel = isExpired ? "Expired" : "Expiring";
                      
                      return (
                      <TableRow key={warning.cluster_id}>
                        <TableCell>
                          <div>
                            <p className="font-medium">{warning.display_name || warning.cluster_id}</p>
                            <p className="text-xs text-muted-foreground">{warning.cluster_id}</p>
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge variant={isExpired ? "destructive" : "secondary"}>
                            {statusLabel}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <Badge 
                            variant={warning.severity === "critical" ? "destructive" : "default"}
                            className="flex items-center gap-1 w-fit"
                          >
                            {warning.severity === "critical" && <Flame className="h-3 w-3" />}
                            {warning.severity}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <span className={warning.days_until_expiry <= 3 ? "text-red-600 font-bold" : warning.days_until_expiry <= 7 ? "text-amber-600 font-medium" : ""}>
                            {warning.days_until_expiry <= 0 ? "Expired" : `${warning.days_until_expiry} days`}
                          </span>
                        </TableCell>
                        <TableCell className="text-xs md:text-sm whitespace-nowrap">{formatDateTime(warning.expires_at)}</TableCell>
                        <TableCell>
                          <Button
                            size="sm"
                            onClick={() => router.push(`/inventory/clusters/certificates?renew=${warning.cluster_id}`)}
                          >
                            Renew
                          </Button>
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
        </TabsContent>

        {/* Inactive Nodes Tab */}
        <TabsContent value="nodes">
          <Card>
            <CardHeader className="p-4 md:p-6">
              <CardTitle className="text-base md:text-lg">
                Offline Nodes ({nodeWarnings.length})
              </CardTitle>
              <CardDescription className="text-xs md:text-sm">
                Nodes that are disconnected or not responding
              </CardDescription>
            </CardHeader>
            <CardContent className="p-0 md:p-6 md:pt-0">
              {nodeWarnings.length === 0 ? (
                <div className="text-center py-8 text-muted-foreground">
                  <Wifi className="h-12 w-12 mx-auto mb-4 opacity-50" />
                  <p>All nodes are online</p>
                  <p className="text-sm mt-1">No connectivity issues detected</p>
                </div>
              ) : (
                <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="min-w-[100px]">Cluster</TableHead>
                      <TableHead className="min-w-[150px]">Endpoint</TableHead>
                      <TableHead className="min-w-[80px]">Severity</TableHead>
                      <TableHead className="min-w-[150px]">Error</TableHead>
                      <TableHead className="min-w-[150px]">Checked At</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {nodeWarnings.map((warning: any) => (
                      <TableRow key={warning.cluster_id}>
                        <TableCell>
                          <div>
                            <p className="font-medium">{warning.display_name || warning.cluster_id}</p>
                            <p className="text-xs text-muted-foreground">{warning.cluster_id}</p>
                          </div>
                        </TableCell>
                        <TableCell>
                          <code className="text-xs bg-secondary/50 px-2 py-1 rounded break-all">
                            {warning.endpoint || "N/A"}
                          </code>
                        </TableCell>
                        <TableCell className="whitespace-nowrap">
                          <Badge variant="destructive" className="flex items-center gap-1 w-fit">
                            <Flame className="h-3 w-3" />
                            {warning.severity || "critical"}
                          </Badge>
                        </TableCell>
                        <TableCell className="max-w-xs text-xs md:text-sm">
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <span className="cursor-help line-clamp-2">{warning.error || "Connection failed"}</span>
                              </TooltipTrigger>
                              <TooltipContent side="top" className="max-w-sm">
                                <p>{warning.error || "Connection failed"}</p>
                              </TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                        </TableCell>
                        <TableCell className="text-xs md:text-sm whitespace-nowrap">{formatDateTime(warning.checked_at)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* Warning Detail Modal */}
      <Dialog open={detailModalOpen} onOpenChange={setDetailModalOpen}>
        <DialogContent className="max-w-3xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>
              Warning Details
            </DialogTitle>
            <DialogDescription>
              Detailed information about the system warning
            </DialogDescription>
          </DialogHeader>
          <div className="mt-4 space-y-4">
            {selectedWarning && (
              <>
                {/* Warning Type & Severity */}
                <div className="flex items-center gap-3">
                  <div>
                    <span className="text-sm font-medium text-muted-foreground">Type:</span>
                    <Badge variant="outline" className="ml-2">{selectedWarning.type}</Badge>
                  </div>
                  <div>
                    <span className="text-sm font-medium text-muted-foreground">Severity:</span>
                    <Badge variant={getSeverityColor(selectedWarning.severity)} className="ml-2">
                      {selectedWarning.severity}
                    </Badge>
                  </div>
                  <div>
                    <span className="text-sm font-medium text-muted-foreground">Status:</span>
                    <Badge variant="secondary" className="ml-2">{selectedWarning.status}</Badge>
                  </div>
                </div>

                {/* Node & Project */}
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-1">
                    <span className="text-sm font-medium text-muted-foreground flex items-center gap-1">
                      <Server className="h-3 w-3" />
                      Node:
                    </span>
                    <p className="text-sm font-mono bg-secondary/50 p-2 rounded">
                      {selectedWarning.location || selectedWarning.source_node || "N/A"}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <span className="text-sm font-medium text-muted-foreground flex items-center gap-1">
                      <FolderOpen className="h-3 w-3" />
                      Project:
                    </span>
                    <p className="text-sm font-mono bg-secondary/50 p-2 rounded">
                      {selectedWarning.project || "default"}
                    </p>
                  </div>
                </div>

                {/* Full Message */}
                <div className="space-y-2">
                  <span className="text-sm font-medium text-muted-foreground">Full Message:</span>
                  <div className="p-4 bg-secondary/50 rounded-md border border-border">
                    <p className="text-sm whitespace-pre-wrap break-words">
                      {selectedWarning.last_message || "No message available"}
                    </p>
                  </div>
                </div>

                {/* Occurrence Info */}
                <div className="grid grid-cols-3 gap-4 pt-4 border-t">
                  <div className="space-y-1">
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="text-sm font-medium text-muted-foreground flex items-center gap-1 cursor-help">
                            Occurrences:
                            <HelpCircle className="h-3 w-3" />
                          </span>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="z-[100]">
                          <p>Total number of times this warning occurred</p>
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                    <p className="text-lg font-semibold">{selectedWarning.count.toLocaleString()}</p>
                  </div>
                  <div className="space-y-1">
                    <span className="text-sm font-medium text-muted-foreground">First Seen:</span>
                    <p className="text-sm">
                      {formatDateTime(selectedWarning.first_seen_at)}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <span className="text-sm font-medium text-muted-foreground">Last Seen:</span>
                    <p className="text-sm">
                      {formatDateTime(selectedWarning.last_seen_at)}
                    </p>
                  </div>
                </div>

                {/* UUID */}
                <div className="pt-4 border-t">
                  <span className="text-xs font-medium text-muted-foreground">UUID:</span>
                  <p className="text-xs font-mono bg-secondary/30 p-2 rounded mt-1">
                    {selectedWarning.uuid}
                  </p>
                </div>
              </>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// Main page component with Suspense wrapper
export default function WarningsPage() {
  const router = useRouter();
  const { isPartner } = usePermissions();

  // redirect if not Partner+
  useEffect(() => {
    if (!isPartner) {
      router.replace("/");
    }
  }, [isPartner, router]);

  if (!isPartner) return null;

  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <WarningsContent />
    </Suspense>
  );
}
