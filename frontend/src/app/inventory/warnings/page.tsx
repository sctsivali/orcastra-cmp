"use client";

import { useState, useMemo, useEffect, Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { IntentLink } from "@/components/navigation/route-intent";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle } from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow } from "@/components/ui/table";
import { TableLink } from "@/components/ui/table-link";
import {
  WarningDetailDialog,
  type WarningKind,
  type WarningRecord,
} from "./components/warning-detail-dialog";
import { TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger } from "@/components/ui/tabs";
import { AlertTriangle, Info, Flame, HelpCircle, Server, FolderOpen, Shield, Wifi, WifiOff } from "lucide-react";
import { useWarnings, useSystemWarnings } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger } from "@/components/ui/tooltip";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { formatDateTime, formatLabel} from "@/lib/utils";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import type { CertificateWarning, WarningInfo } from "@/types/api";
import { SEVERITY_RANK } from "@/lib/sort";

const toTime = (v: string | null | undefined): number | null => {
  if (!v) return null;
  const t = new Date(v).getTime();
  return isNaN(t) ? null : t;
};

// Separate component that uses useSearchParams
// Three tables, three shapes, three namespaces. Shared state would filter one tab by a
// field another does not carry, which is the defect this page already shipped in reverse:
// one severity Select rendered once but wired only to the platform tab.
const PLATFORM_WARNINGS = defineTable<WarningInfo>()({
  id: "all",
  searchPlaceholder: "Search warnings",
  defaultSort: { field: "severity", direction: "asc" },
  columns: [
    {
      field: "type",
      label: "Type",
      sort: (w) => w.type,
      search: (w) => w.type,
      facet: (w) => w.type,
      facetSpec: { primary: true, format: formatLabel } },
    { field: "message", label: "Message", sort: (w) => w.last_message, search: (w) => w.last_message, filter: fromSort("text") },
    {
      field: "severity",
      label: "Severity",
      sort: (w) => SEVERITY_RANK[String(w.severity || "").toLowerCase()] ?? 99,
      facet: (w) => String(w.severity || "").toLowerCase(),
      facetSpec: { primary: true, format: formatLabel, order: ["critical", "error", "high", "warning", "medium", "low", "info"] } },
    {
      field: "node",
      label: "Node",
      sort: (w) => w.location || w.source_node || "",
      // The node filter used to be settable only from ?node=, with no control on the page.
      facet: (w) => w.location || w.source_node || "",
      facetSpec: { role: "node", primary: true, emptyLabel: "(no node)" } },
    // note: real and populated, and until now rendered only inside the detail dialog.
    { field: "project", label: "Project", facet: (w) => w.project, facetSpec: { role: "project", emptyLabel: "(no project)" } },
    { field: "count", label: "Count", sort: (w) => w.count ?? 0, filter: fromSort("number", { unit: "count" }) },
    { field: "last_seen", label: "Last Seen", sort: (w) => toTime(w.last_seen_at), filter: fromSort("date", { horizon: "past" }) },
  ] });

const CERT_WARNINGS = defineTable<CertificateWarning>()({
  id: "certificates",
  searchPlaceholder: "Search certificate issues",
  defaultSort: { field: "days", direction: "asc" },
  columns: [
    {
      field: "cluster",
      label: "Cluster",
      sort: (w) => w.display_name || w.cluster_id || "",
      search: (w) => w.display_name || w.cluster_id || "",
      facet: (w) => w.display_name || w.cluster_id || "",
      facetSpec: { role: "node",} },
    {
      field: "status",
      label: "Status",
      sort: (w) => (w.warning_type === "cert_expired" || (w.days_until_expiry ?? 0) <= 0 ? "Expired" : "Expiring"),
      facet: (w) => (w.warning_type === "cert_expired" || (w.days_until_expiry ?? 0) <= 0 ? "Expired" : "Expiring"),
      facetSpec: { primary: true } },
    {
      field: "severity",
      label: "Severity",
      sort: (w) => SEVERITY_RANK[String(w.severity || "").toLowerCase()] ?? 99,
      facet: (w) => String(w.severity || "").toLowerCase(),
      facetSpec: { primary: true, format: formatLabel } },
    { field: "days", label: "Days Until Expiry", sort: (w) => w.days_until_expiry, filter: fromSort("number", { unit: "days" }) },
    { field: "expires", label: "Expires At", sort: (w) => toTime(w.expires_at), filter: fromSort("date", { horizon: "future" }) },
  ] });

const NODE_WARNINGS = defineTable<CertificateWarning>()({
  id: "nodes",
  searchPlaceholder: "Search offline nodes",
  defaultSort: { field: "cluster", direction: "asc" },
  columns: [
    {
      field: "cluster",
      label: "Cluster",
      sort: (w) => w.display_name || w.cluster_id || "",
      search: (w) => w.display_name || w.cluster_id || "",
      facet: (w) => w.display_name || w.cluster_id || "",
      facetSpec: { role: "node",} },
    { field: "endpoint", label: "Endpoint", sort: (w) => w.endpoint || "", search: (w) => w.endpoint || "", filter: fromSort("text") },
    {
      field: "severity",
      label: "Severity",
      sort: (w) => SEVERITY_RANK[String(w.severity || "critical").toLowerCase()] ?? 99,
      facet: (w) => String(w.severity || "critical").toLowerCase(),
      facetSpec: { primary: true, format: formatLabel } },
    // note: `message` is what CertificateWarning actually carries. The columns here used to
    // read `error` and `checked_at`, which the model has never had, so every row rendered the
    // same "Connection failed" fallback and an empty date while the populated message went
    // unshown. Typing this table is what turned that from a review finding into a build error.
    { field: "message", label: "Message", sort: (w) => w.message || "", search: (w) => w.message || "", filter: fromSort("text") },
  ] });

function WarningsContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { data, isLoading, refreshNow, dataUpdatedAt } = useWarnings();
  const { data: systemWarnings, isLoading: isLoadingSystem, refreshNow: refreshSystemWarnings } = useSystemWarnings();
  const allWarnings = useMemo(() => (data as any)?.warnings || [], [data]);
  // Clusters that never answered: their warnings are absent from the list, so warn rather
  // than showing a shorter list with nothing to explain it.
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];

  // Certificate and node warnings from system warnings
  const certWarnings = useMemo(() => systemWarnings?.certificate_warnings || [], [systemWarnings]);
  const nodeWarnings = useMemo(() => systemWarnings?.node_warnings || [], [systemWarnings]);
  
  // Active tab - read from URL params
  const initialTab = searchParams.get("tab") || "all";
  const [activeTab, setActiveTab] = useState(initialTab);
  
  // Get severity and node filter from URL
  
  // Only the tab is mirrored here now. Severity and node moved into the per-tab filter
  // instances, where the URL is the single store rather than a copy of one.
  useEffect(() => {
    setActiveTab(searchParams.get("tab") || "all");
  }, [searchParams]);

  // Bare ?severity= and ?node= predate the tabs being namespaced and are an external
  // contract, so they are read forever and translated once into the platform tab's keys,
  // then removed so the seed cannot reassert itself when the operator clears the control.
  useEffect(() => {
    const legacySeverity = searchParams.get("severity");
    const legacyNode = searchParams.get("node");
    if (legacySeverity === null && legacyNode === null) return;
    const params = new URLSearchParams(searchParams.toString());
    params.delete("severity");
    params.delete("node");
    if (legacySeverity && legacySeverity !== "all") {
      params.set("all.severity", legacySeverity.toLowerCase());
    }
    if (legacyNode) params.set("all.node", legacyNode);
    const query = params.toString();
    router.replace(query ? `/inventory/warnings?${query}` : "/inventory/warnings", { scroll: false });
  }, [searchParams, router]);


  // A pinned role is exempt from the two-value threshold, so only availableFields can drop it.
  // Without these, a tab with no rows still rendered its node control with an empty popover, and
  // an empty certificates tab is the DESIRED state here, not an edge case.
  //
  // important: the gate names the FIELD, not the role. Two of these three spell the node
  // dimension `cluster`.
  const gate = <F extends string>(fields: readonly F[], field: F, present: boolean) =>
    present ? fields : fields.filter((f) => f !== field);

  // One filter instance per tab, each namespaced in the URL so a selection on one cannot
  // leak into another.
  const platformFilters = useTableFilters(PLATFORM_WARNINGS, allWarnings as any[], {
    availableFields: gate(
      PLATFORM_WARNINGS.facetFields,
      "node",
      (allWarnings as any[]).some((w) => w.location || w.source_node),
    ),
  });
  const certFilters = useTableFilters(CERT_WARNINGS, certWarnings as any[], {
    availableFields: gate(
      CERT_WARNINGS.facetFields,
      "cluster",
      (certWarnings as any[]).some((w) => w.display_name || w.cluster_id),
    ),
  });
  const nodeFilters = useTableFilters(NODE_WARNINGS, nodeWarnings as any[], {
    availableFields: gate(
      NODE_WARNINGS.facetFields,
      "cluster",
      (nodeWarnings as any[]).some((w) => w.display_name || w.cluster_id),
    ),
  });

  // Per-table sorting (3 independent tables: platform / certificates / offline nodes)
  const {
    sortField: platformSortField,
    sortDirection: platformSortDirection,
    handleSort: platformHandleSort,
    sortItems: platformSortItems } = useTableSort(PLATFORM_WARNINGS.sortAccessors, PLATFORM_WARNINGS.defaultSort.field, PLATFORM_WARNINGS.defaultSort.direction);
  const sortedWarnings = useMemo(() => platformSortItems(platformFilters.filteredRows), [platformFilters.filteredRows, platformSortItems]);

  const {
    sortField: certSortField,
    sortDirection: certSortDirection,
    handleSort: certHandleSort,
    sortItems: certSortItems } = useTableSort(CERT_WARNINGS.sortAccessors, CERT_WARNINGS.defaultSort.field, CERT_WARNINGS.defaultSort.direction);
  const sortedCertWarnings = useMemo(() => certSortItems(certFilters.filteredRows), [certFilters.filteredRows, certSortItems]);

  const {
    sortField: nodeSortField,
    sortDirection: nodeSortDirection,
    handleSort: nodeHandleSort,
    sortItems: nodeSortItems } = useTableSort(NODE_WARNINGS.sortAccessors, NODE_WARNINGS.defaultSort.field, NODE_WARNINGS.defaultSort.direction);
  const sortedNodeWarnings = useMemo(() => nodeSortItems(nodeFilters.filteredRows), [nodeFilters.filteredRows, nodeSortItems]);

  
  // State for warning detail modal. The kind travels with the record, because the three tabs
  // carry three different shapes and the dialog renders each one differently.
  const [selectedWarning, setSelectedWarning] = useState<WarningRecord | null>(null);
  const [detailModalOpen, setDetailModalOpen] = useState(false);

  const handleShowWarningDetail = (kind: WarningKind, record: any) => {
    setSelectedWarning({ kind, record });
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
  
  // important: awaited, not fire-and-forget. The previous `void Promise.all(...)`
  // discarded both the completion and any failure, so a refresh against a dead
  // backend was indistinguishable from a successful one.
  const handleRefreshAll = () => Promise.all([refreshNow(), refreshSystemWarnings()]);

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
        <RefreshButton
          label="Refresh warnings"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={handleRefreshAll}
        />
      </div>

      <UnreachableNodesBanner noun="warnings" nodes={unreachableNodes} />

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
            <span className="hidden sm:inline">Platform Warnings</span>
            <span className="sm:hidden">Platform</span>
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

        {/* Platform Warnings Tab */}
        <TabsContent value="all">
          <Card>
        <CardHeader className="pb-4 p-4 md:p-6 space-y-4">
          <div className="min-w-0">
            <CardTitle className="text-base md:text-lg">System Warnings</CardTitle>
            <CardDescription className="mt-1 text-xs md:text-sm">
              Select a warning type to see its full message and where it came from
            </CardDescription>
          </div>
          <TableToolbar filters={platformFilters} noun="warnings" />
        </CardHeader>
        <CardContent className="p-0 md:p-6 md:pt-0">
          {sortedWarnings.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              {platformFilters.activeFilterCount > 0
                ? "No warning matches the current filters."
                : "No warnings found"}
            </div>
          ) : (
            <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead className="min-w-[100px]" label="Type" field="type" currentField={platformSortField} direction={platformSortDirection} onSort={platformHandleSort} />
                  <SortableTableHead className="min-w-[200px]" label="Message" field="message" currentField={platformSortField} direction={platformSortDirection} onSort={platformHandleSort} />
                  <SortableTableHead className="min-w-[80px]" label="Severity" field="severity" currentField={platformSortField} direction={platformSortDirection} onSort={platformHandleSort} />
                  <SortableTableHead className="min-w-[100px]" label="Node" field="node" currentField={platformSortField} direction={platformSortDirection} onSort={platformHandleSort} />
                  <SortableTableHead
                    className="min-w-[60px]"
                    field="count"
                    currentField={platformSortField}
                    direction={platformSortDirection}
                    onSort={platformHandleSort}
                    label={
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
                    }
                  />
                  <SortableTableHead className="min-w-[150px]" label="Last Seen" field="last_seen" currentField={platformSortField} direction={platformSortDirection} onSort={platformHandleSort} />
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedWarnings.map((warning: any) => (
                  <TableRow key={warning.uuid}>
                    <TableCell>
                      <TableLink
                        onClick={() => handleShowWarningDetail("platform", warning)}
                        aria-label={`Show details for ${formatLabel(warning.type)} warning`}
                      >
                        {formatLabel(warning.type)}
                      </TableLink>
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
                      {warning.location || warning.source_node ? (
                        <span className="font-medium">{warning.location || warning.source_node}</span>
                      ) : (
                        <TableEmptyValue label="No location" />
                      )}
                    </TableCell>
                    <TableCell>
                      {warning.count.toLocaleString()}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
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
            <CardHeader className="p-4 md:p-6 space-y-4">
              <div>
                <CardTitle className="text-base md:text-lg">Certificate Issues</CardTitle>
                <CardDescription className="text-xs md:text-sm">
                  Certificates expiring soon or already expired
                </CardDescription>
              </div>
              {/* Severity applies here too. One Select wired only to the platform tab is the
                  defect this replaces: all three tables carry a severity. */}
              <TableToolbar filters={certFilters} noun="certificate issues" />
            </CardHeader>
            <CardContent className="p-0 md:p-6 md:pt-0">
              {sortedCertWarnings.length === 0 ? (
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
                      <SortableTableHead label="Cluster" field="cluster" currentField={certSortField} direction={certSortDirection} onSort={certHandleSort} />
                      <SortableTableHead label="Status" field="status" currentField={certSortField} direction={certSortDirection} onSort={certHandleSort} />
                      <SortableTableHead label="Severity" field="severity" currentField={certSortField} direction={certSortDirection} onSort={certHandleSort} />
                      <SortableTableHead label="Days Until Expiry" field="days" currentField={certSortField} direction={certSortDirection} onSort={certHandleSort} />
                      <SortableTableHead label="Expires At" field="expires" currentField={certSortField} direction={certSortDirection} onSort={certHandleSort} />
                      <TableHead>Action</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sortedCertWarnings.map((warning: any) => {
                      const isExpired = warning.warning_type === "cert_expired" || warning.days_until_expiry <= 0;
                      const statusLabel = isExpired ? "Expired" : "Expiring";
                      
                      return (
                      <TableRow key={warning.cluster_id}>
                        <TableCell>
                          <div>
                            <p className="font-medium">
                              <TableLink
                                onClick={() => handleShowWarningDetail("certificate", warning)}
                                aria-label={`Show details for ${warning.display_name || warning.cluster_id}`}
                              >
                                {warning.display_name || warning.cluster_id}
                              </TableLink>
                            </p>
                            <TableSubText>{warning.cluster_id}</TableSubText>
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
                        <TableCell className="whitespace-nowrap">{formatDateTime(warning.expires_at)}</TableCell>
                        <TableCell>
                          <Button asChild size="sm">
                            <IntentLink href={`/inventory/clusters/certificates?renew=${warning.cluster_id}`}>
                              Renew
                            </IntentLink>
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
            <CardHeader className="p-4 md:p-6 space-y-4">
              <div>
                <CardTitle className="text-base md:text-lg">Offline Nodes</CardTitle>
                <CardDescription className="text-xs md:text-sm">
                  Nodes that are disconnected or not responding
                </CardDescription>
              </div>
              <TableToolbar filters={nodeFilters} noun="offline nodes" />
            </CardHeader>
            <CardContent className="p-0 md:p-6 md:pt-0">
              {sortedNodeWarnings.length === 0 ? (
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
                      <SortableTableHead className="min-w-[100px]" label="Cluster" field="cluster" currentField={nodeSortField} direction={nodeSortDirection} onSort={nodeHandleSort} />
                      <SortableTableHead className="min-w-[150px]" label="Endpoint" field="endpoint" currentField={nodeSortField} direction={nodeSortDirection} onSort={nodeHandleSort} />
                      <SortableTableHead className="min-w-[80px]" label="Severity" field="severity" currentField={nodeSortField} direction={nodeSortDirection} onSort={nodeHandleSort} />
                      <SortableTableHead className="min-w-[150px]" label="Message" field="message" currentField={nodeSortField} direction={nodeSortDirection} onSort={nodeHandleSort} />
                                          </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sortedNodeWarnings.map((warning: any) => (
                      <TableRow key={warning.cluster_id}>
                        <TableCell>
                          <div>
                            <p className="font-medium">
                              <TableLink
                                onClick={() => handleShowWarningDetail("node", warning)}
                                aria-label={`Show details for ${warning.display_name || warning.cluster_id}`}
                              >
                                {warning.display_name || warning.cluster_id}
                              </TableLink>
                            </p>
                            <TableSubText>{warning.cluster_id}</TableSubText>
                          </div>
                        </TableCell>
                        <TableCell>
                          {warning.endpoint ? (
                            <span className="font-mono break-all">{warning.endpoint}</span>
                          ) : (
                            <TableEmptyValue label="No endpoint" />
                          )}
                        </TableCell>
                        <TableCell className="whitespace-nowrap">
                          <Badge variant="destructive" className="flex items-center gap-1 w-fit">
                            <Flame className="h-3 w-3" />
                            {warning.severity || "critical"}
                          </Badge>
                        </TableCell>
                        <TableCell className="max-w-xs">
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <span className="cursor-help line-clamp-2">{warning.message || "Connection failed"}</span>
                              </TooltipTrigger>
                              <TooltipContent side="top" className="max-w-sm">
                                <p>{warning.message || "Connection failed"}</p>
                              </TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
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
      </Tabs>

      {/* One dialog for all three tabs. Certificates and Offline Nodes never had one, and
          each dropped the field that matters most: a full cluster id, and the connection
          error the table clips to two lines behind a tooltip. */}
      <WarningDetailDialog
        selected={selectedWarning}
        open={detailModalOpen}
        onOpenChange={(next) => {
          setDetailModalOpen(next);
          if (!next) setSelectedWarning(null);
        }}
      />
    </div>
  );
}

// Main page component with Suspense wrapper
export default function WarningsPage() {
  const router = useRouter();
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <WarningsContent />
    </Suspense>
  );
}
