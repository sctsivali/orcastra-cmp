"use client";

import { useState, useMemo, useEffect, useCallback, useRef, Suspense } from "react";
import { usePathname, useSearchParams, useRouter } from "next/navigation";
import { currentQuery, replaceQuery } from "@/lib/url-state";
import { IntentLink } from "@/components/navigation/route-intent";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { UnreachableNodesBanner, reachabilityOf } from "@/components/ui/unreachable-nodes-banner";
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
import { KernelAdvisoryTab } from "./components/kernel-advisory-tab";
import {
  warningSeverityDisplay,
  warningSeverityLabel,
} from "./components/warning-severity";
import { TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { OverflowText } from "@/components/ui/overflow-text";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger } from "@/components/ui/tabs";
import { AlertTriangle, Info, Flame, HelpCircle, Server, FolderOpen, Shield, ShieldAlert, Wifi, WifiOff } from "lucide-react";
import { useWarnings, useSystemWarnings } from "@/hooks/use-lxd";
import { useActorQueryScope } from "@/hooks/use-query-scope";
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
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { defineTable, fromSort } from "@/lib/table-filter";
import type { CertificateWarning, WarningInfo } from "@/types/api";
import { SEVERITY_RANK } from "@/lib/sort";
import { inventoryLinks } from "@/lib/inventory-links";
import { serialiseFacet } from "@/lib/filter-url";
import {
  WARNING_SEVERITY_ORDER,
  warningClusterValue,
  warningNodeValue,
  warningSeverityValue,
} from "@/lib/warning-facets";
import { PageReadError } from "@/components/ui/read-error-state";
import { NodeLink } from "@/components/ui/node-link";

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
  // The cluster member that raised the warning stays searchable; the Node facet below keys on
  // the registered node instead, which is what every link into this page carries.
  searchExtra: (w) => [w.location],
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
      sort: (w) => SEVERITY_RANK[warningSeverityValue(w.severity)] ?? 99,
      facet: (w) => warningSeverityValue(w.severity),
      facetSpec: { primary: true, format: warningSeverityLabel, order: [...WARNING_SEVERITY_ORDER] } },
    {
      field: "node",
      label: "Cluster",
      // important: the registered node, not `location`. On a clustered host `location` is the
      // LXD member name (microcloud20), which nothing else in the console links by, so a link
      // carrying the cluster id landed on "No warning matches the current filters." while
      // the tile it came from counted five. The member is rendered as sub-text and searched.
      sort: (w) => warningNodeValue(w),
      facet: (w) => warningNodeValue(w),
      facetSpec: { role: "node", primary: true, emptyLabel: "(no cluster)" } },
    // note: real and populated, and until now rendered only inside the detail dialog.
    { field: "project", label: "Project", facet: (w) => w.project, facetSpec: { role: "project", emptyLabel: "(no project)" } },
    { field: "count", label: "Count", sort: (w) => w.count ?? 0, filter: fromSort("number", { unit: "count" }) },
    { field: "last_seen", label: "Last Seen", sort: (w) => toTime(w.last_seen_at), filter: fromSort("date", { horizon: "past" }) },
  ] });

const CERT_WARNINGS = defineTable<CertificateWarning>()({
  id: "certificates",
  searchPlaceholder: "Search certificate issues",
  // important: the id is searchable too. The accessors below fall back to `cluster_id` only when
  // there is no display name, so a cluster that HAS one could not be found by the id an operator
  // reads off every other surface. sort and facet are deliberately left alone: a facet accessor's
  // return value is both the URL value and the option label, so widening it would rename the
  // options and break every existing ?node=<display_name> link into this page.
  searchExtra: (w) => [w.cluster_id],
  defaultSort: { field: "days", direction: "asc" },
  columns: [
    {
      field: "cluster",
      label: "Cluster",
      sort: (w) => warningClusterValue(w),
      search: (w) => warningClusterValue(w),
      facet: (w) => warningClusterValue(w),
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
      sort: (w) => SEVERITY_RANK[warningSeverityValue(w.severity)] ?? 99,
      facet: (w) => warningSeverityValue(w.severity),
      facetSpec: { primary: true, format: warningSeverityLabel } },
    { field: "days", label: "Days Until Expiry", sort: (w) => w.days_until_expiry, filter: fromSort("number", { unit: "days" }) },
    { field: "expires", label: "Expires At", sort: (w) => toTime(w.expires_at), filter: fromSort("date", { horizon: "future" }) },
  ] });

const NODE_WARNINGS = defineTable<CertificateWarning>()({
  id: "nodes",
  searchPlaceholder: "Search offline clusters",
  // important: the id is searchable too. The accessors below fall back to `cluster_id` only when
  // there is no display name, so a cluster that HAS one could not be found by the id an operator
  // reads off every other surface. sort and facet are deliberately left alone: a facet accessor's
  // return value is both the URL value and the option label, so widening it would rename the
  // options and break every existing ?node=<display_name> link into this page.
  searchExtra: (w) => [w.cluster_id],
  defaultSort: { field: "cluster", direction: "asc" },
  columns: [
    {
      field: "cluster",
      label: "Cluster",
      sort: (w) => warningClusterValue(w),
      search: (w) => warningClusterValue(w),
      facet: (w) => warningClusterValue(w),
      facetSpec: { role: "node",} },
    { field: "endpoint", label: "Endpoint", sort: (w) => w.endpoint || "", search: (w) => w.endpoint || "", filter: fromSort("text") },
    {
      field: "severity",
      label: "Severity",
      sort: (w) => SEVERITY_RANK[warningSeverityValue(w.severity || "critical")] ?? 99,
      facet: (w) => warningSeverityValue(w.severity || "critical"),
      facetSpec: { primary: true, format: warningSeverityLabel } },
    // note: `message` is what CertificateWarning actually carries. The columns here used to
    // read `error` and `checked_at`, which the model has never had, so every row rendered the
    // same "Connection failed" fallback and an empty date while the populated message went
    // unshown. Typing this table is what turned that from a review finding into a build error.
    { field: "message", label: "Message", sort: (w) => w.message || "", search: (w) => w.message || "", filter: fromSort("text") },
  ] });

function WarningsContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { scopeKey } = useActorQueryScope();
  const { data, isLoading, isError, error, refetch, refreshNow, dataUpdatedAt } = useWarnings();
  const {
    data: systemWarnings,
    isLoading: isLoadingSystem,
    isError: isSystemError,
    error: systemError,
    refetch: refetchSystem,
    refreshNow: refreshSystemWarnings,
  } = useSystemWarnings();
  const allWarnings = useMemo(() => (data as any)?.warnings || [], [data]);
  // Clusters that never answered: their warnings are absent from the list, so warn rather
  // than showing a shorter list with nothing to explain it.
  const reach = reachabilityOf(data);

  // Certificate and node warnings from system warnings
  const certWarnings = useMemo(() => systemWarnings?.certificate_warnings || [], [systemWarnings]);
  const nodeWarnings = useMemo(() => systemWarnings?.node_warnings || [], [systemWarnings]);
  const kernelWarnings = useMemo(() => systemWarnings?.kernel_advisories || [], [systemWarnings]);
  
  // important: the tab is URL state, read on every render and written on change. It used to be
  // a useState re-seeded from ?tab= on every URL change while nothing ever wrote ?tab=, so a
  // facet toggle on the certificates tab bounced the operator back to the platform tab.
  // Severity and node live in the per-tab filter instances, same store, and the write composes
  // on the live query so a facet written in the same tick is kept.
  const activeTab = searchParams.get("tab") || "all";
  const setActiveTab = useCallback(
    (value: string) => {
      const params = currentQuery();
      if (value === "all") params.delete("tab");
      else params.set("tab", value);
      replaceQuery(pathname, params);
    },
    [pathname],
  );

  // Bare ?severity= and ?node= predate the tabs being namespaced and are an external
  // contract, so they are read forever and translated once into the platform tab's keys,
  // then removed so the seed cannot reassert itself when the operator clears the control.
  // Internal links come through lib/inventory-links.ts and never carry these.
  useEffect(() => {
    const legacySeverity = searchParams.get("severity");
    const legacyNode = searchParams.get("node");
    if (legacySeverity === null && legacyNode === null) return;
    const params = new URLSearchParams(searchParams.toString());
    params.delete("severity");
    params.delete("node");
    if (legacySeverity && legacySeverity !== "all") {
      params.set("all.severity", serialiseFacet([legacySeverity.toLowerCase()]));
    }
    if (legacyNode) params.set("all.node", serialiseFacet([legacyNode]));
    const query = params.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  }, [searchParams, router, pathname]);


  // A pinned role is exempt from the two-value threshold, so only availableFields can drop it.
  // Without these, a tab with no rows still rendered its node control with an empty popover, and
  // an empty certificates tab is the DESIRED state here, not an edge case.
  //
  // important: the gate names the FIELD, not the role. Two of these three spell the node
  // dimension `cluster`.
  const gate = <F extends string>(fields: readonly F[], field: F, present: boolean) =>
    present ? fields : fields.filter((f) => f !== field);

  // Paging is per tab for the same reason the filters are: three tables of three shapes and
  // three lengths. One shared page number would drop the operator onto page 4 of a two-page
  // certificates table the moment they switched tabs.
  const [platformPage, setPlatformPage] = useState(1);
  const [platformPageSize, setPlatformPageSize] = useState(TABLE_PAGE_SIZE);
  const resetPlatformPage = useCallback(() => setPlatformPage(1), []);
  const [certPage, setCertPage] = useState(1);
  const [certPageSize, setCertPageSize] = useState(TABLE_PAGE_SIZE);
  const resetCertPage = useCallback(() => setCertPage(1), []);
  const [nodePage, setNodePage] = useState(1);
  const [nodePageSize, setNodePageSize] = useState(TABLE_PAGE_SIZE);
  const resetNodePage = useCallback(() => setNodePage(1), []);

  // One filter instance per tab, each namespaced in the URL so a selection on one cannot
  // leak into another.
  const platformFilters = useTableFilters(PLATFORM_WARNINGS, allWarnings as any[], {
    onFilterChange: resetPlatformPage,
    availableFields: gate(
      PLATFORM_WARNINGS.facetFields,
      "node",
      (allWarnings as any[]).some((w) => warningNodeValue(w)),
    ),
  });
  const certFilters = useTableFilters(CERT_WARNINGS, certWarnings as any[], {
    onFilterChange: resetCertPage,
    availableFields: gate(
      CERT_WARNINGS.facetFields,
      "cluster",
      (certWarnings as any[]).some((w) => w.display_name || w.cluster_id),
    ),
  });
  const nodeFilters = useTableFilters(NODE_WARNINGS, nodeWarnings as any[], {
    onFilterChange: resetNodePage,
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
    sortItems: platformSortItems } = useTableSort(PLATFORM_WARNINGS.sortAccessors, PLATFORM_WARNINGS.defaultSort.field, PLATFORM_WARNINGS.defaultSort.direction, resetPlatformPage);
  const sortedWarnings = useMemo(() => platformSortItems(platformFilters.filteredRows), [platformFilters.filteredRows, platformSortItems]);
  // Clamped on read rather than corrected in an effect: this list refetches on its own and a
  // warning can age out of it, so the page under the operator shrinks with no interaction.
  const platformTotalPages = Math.max(1, Math.ceil(sortedWarnings.length / platformPageSize));
  const platformCurrentPage = Math.min(platformPage, platformTotalPages);
  const pagedWarnings = sortedWarnings.slice(
    (platformCurrentPage - 1) * platformPageSize,
    platformCurrentPage * platformPageSize,
  );

  const {
    sortField: certSortField,
    sortDirection: certSortDirection,
    handleSort: certHandleSort,
    sortItems: certSortItems } = useTableSort(CERT_WARNINGS.sortAccessors, CERT_WARNINGS.defaultSort.field, CERT_WARNINGS.defaultSort.direction, resetCertPage);
  const sortedCertWarnings = useMemo(() => certSortItems(certFilters.filteredRows), [certFilters.filteredRows, certSortItems]);
  const certTotalPages = Math.max(1, Math.ceil(sortedCertWarnings.length / certPageSize));
  const certCurrentPage = Math.min(certPage, certTotalPages);
  const pagedCertWarnings = sortedCertWarnings.slice(
    (certCurrentPage - 1) * certPageSize,
    certCurrentPage * certPageSize,
  );

  const {
    sortField: nodeSortField,
    sortDirection: nodeSortDirection,
    handleSort: nodeHandleSort,
    sortItems: nodeSortItems } = useTableSort(NODE_WARNINGS.sortAccessors, NODE_WARNINGS.defaultSort.field, NODE_WARNINGS.defaultSort.direction, resetNodePage);
  const sortedNodeWarnings = useMemo(() => nodeSortItems(nodeFilters.filteredRows), [nodeFilters.filteredRows, nodeSortItems]);
  const nodeTotalPages = Math.max(1, Math.ceil(sortedNodeWarnings.length / nodePageSize));
  const nodeCurrentPage = Math.min(nodePage, nodeTotalPages);
  const pagedNodeWarnings = sortedNodeWarnings.slice(
    (nodeCurrentPage - 1) * nodePageSize,
    nodeCurrentPage * nodePageSize,
  );

  
  // State for warning detail modal. The kind travels with the record, because the three tabs
  // carry three different shapes and the dialog renders each one differently.
  const [warningUiActorScope, setWarningUiActorScope] = useState(scopeKey);
  const previousWarningActorScopeRef = useRef(scopeKey);
  const [selectedWarning, setSelectedWarning] = useState<WarningRecord | null>(null);
  const [detailModalOpen, setDetailModalOpen] = useState(false);
  const warningUiIsCurrent = warningUiActorScope === scopeKey;
  const currentSelectedWarning = warningUiIsCurrent ? selectedWarning : null;

  // The page can remain mounted while the session changes. Render-gate the selected record
  // immediately, then clear it after commit so a new operator never sees the prior actor's
  // warning message, cluster id, endpoint, project, or raw connection detail.
  useEffect(() => {
    if (previousWarningActorScopeRef.current === scopeKey) return;
    previousWarningActorScopeRef.current = scopeKey;
    setWarningUiActorScope(scopeKey);
    setSelectedWarning(null);
    setDetailModalOpen(false);
  }, [scopeKey]);

  const handleShowWarningDetail = (kind: WarningKind, record: any) => {
    if (!warningUiIsCurrent) return;
    setWarningUiActorScope(scopeKey);
    setSelectedWarning({ kind, record });
    setDetailModalOpen(true);
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

  // A failed read is not an empty list. Either feed failing with nothing cached is the whole
  // page failing: the three tabs cannot honestly say "no warnings" about a read that did not
  // return.
  if ((isError && !data) || (isSystemError && !systemWarnings)) {
    return (
      <PageReadError
        title="Warnings"
        noun="warnings"
        error={isError && !data ? error : systemError}
        onRetry={() => {
          void refetch();
          void refetchSystem();
        }}
      />
    );
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

      <UnreachableNodesBanner noun="warnings" nodes={reach.unreachable} reasons={reach.reasons} />
      <UnreachableNodesBanner noun="warnings" nodes={reach.partial} reasons={reach.reasons} partial />

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-4">
        <Card>
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium">Total Warnings</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold">
              {allWarnings.length + certWarnings.length + nodeWarnings.length + kernelWarnings.length}
            </div>
          </CardContent>
        </Card>
        <Card className="border-status-offline/50">
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium text-status-offline">Critical</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold text-status-offline">
              {systemWarnings?.critical || 0}
            </div>
          </CardContent>
        </Card>
        <Card className="border-status-pending/50">
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium text-status-pending">Certificate Issues</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold text-status-pending">
              {certWarnings.length}
            </div>
          </CardContent>
        </Card>
        <Card className="border-status-offline/50">
          <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
            <CardTitle className="text-xs md:text-sm font-medium text-status-offline">Offline Clusters</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
            <div className="text-xl md:text-2xl font-bold text-status-offline">
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
            <span className="hidden sm:inline">Offline Clusters</span>
            <span className="sm:hidden">Offline</span>
            <span className="text-[10px] md:text-xs">({nodeWarnings.length})</span>
          </TabsTrigger>
          <TabsTrigger value="kernel" className="gap-1 md:gap-2 text-xs md:text-sm">
            <ShieldAlert className="h-3 w-3 md:h-4 md:w-4" />
            <span className="hidden sm:inline">Host Kernels</span>
            <span className="sm:hidden">Kernels</span>
            <span className="text-[10px] md:text-xs">({kernelWarnings.length})</span>
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
            <>
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead className="min-w-[100px]" label="Type" field="type" currentField={platformSortField} direction={platformSortDirection} onSort={platformHandleSort} />
                  <SortableTableHead className="min-w-[200px]" label="Message" field="message" currentField={platformSortField} direction={platformSortDirection} onSort={platformHandleSort} />
                  <SortableTableHead className="min-w-[80px]" label="Severity" field="severity" currentField={platformSortField} direction={platformSortDirection} onSort={platformHandleSort} />
                  <SortableTableHead className="min-w-[100px]" label="Cluster" field="node" currentField={platformSortField} direction={platformSortDirection} onSort={platformHandleSort} />
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
                {pagedWarnings.map((warning: any) => (
                  <TableRow key={warning.uuid}>
                    <TableCell>
                      <TableLink
                        onClick={() => handleShowWarningDetail("platform", warning)}
                        aria-label={`Show details for ${formatLabel(warning.type)} warning`}
                      >
                        {formatLabel(warning.type)}
                      </TableLink>
                    </TableCell>
                    <TableCell className="max-w-md">
                      <OverflowText
                        value={warning.last_message}
                        label={`message for the ${formatLabel(warning.type)} warning`}
                        lines={2}
                      />
                    </TableCell>
                    <TableCell>
                      <Badge variant={warningSeverityDisplay(warning.severity).tone} className="flex items-center gap-1 w-fit">
                        {getSeverityIcon(warning.severity)}
                        {warningSeverityDisplay(warning.severity).label}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {/* The registered node (source_node), never the LXD member (location),
                          which is a different namespace and stays a plain qualifier. */}
                      <NodeLink clusterId={warningNodeValue(warning)} className="font-medium" />
                      {warningNodeValue(warning) && warning.location && warning.location !== warning.source_node && (
                        <TableSubText>{warning.location}</TableSubText>
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
            {/* Absent rather than disabled when everything fits on one page. Outside the
                horizontal scroller, so the controls stay put when a wide table scrolls. */}
            {platformTotalPages > 1 && (
              <div className="mt-4">
                <DataTablePagination
                  currentPage={platformCurrentPage}
                  totalPages={platformTotalPages}
                  pageSize={platformPageSize}
                  pageSizeOptions={[...TABLE_PAGE_SIZES]}
                  totalItems={sortedWarnings.length}
                  onPageChange={setPlatformPage}
                  onPageSizeChange={(size) => {
                    setPlatformPageSize(size);
                    setPlatformPage(1);
                  }}
                />
              </div>
            )}
            </>
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
                /* important: "all healthy" is a claim about the fleet, so it must not be made
                   about a list that a filter emptied. On a health surface the two readings lead
                   an operator to opposite conclusions, and the filtered one is now easier to
                   reach than it was before this table could be narrowed and paged. */
                certFilters.activeFilterCount > 0 ? (
                  <div className="text-center py-8 text-muted-foreground">
                    <Shield className="h-12 w-12 mx-auto mb-4 opacity-50" />
                    <p>No certificate issue matches the current filters</p>
                    <p className="text-sm mt-1">
                      {certFilters.activeFilterCount} filter
                      {certFilters.activeFilterCount === 1 ? "" : "s"} active. This is not a
                      clean bill of health.
                    </p>
                    <Button variant="outline" size="sm" className="mt-4" onClick={() => certFilters.clearAll()}>
                      Clear filters
                    </Button>
                  </div>
                ) : (
                  <div className="text-center py-8 text-muted-foreground">
                    <Shield className="h-12 w-12 mx-auto mb-4 opacity-50" />
                    <p>No certificate issues found</p>
                    <p className="text-sm mt-1">All certificates are healthy</p>
                  </div>
                )
              ) : (
                <>
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
                    {pagedCertWarnings.map((warning: any) => {
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
                            <TableSubText><NodeLink clusterId={warning.cluster_id} /></TableSubText>
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge variant={isExpired ? "negative" : "caution"}>
                            {statusLabel}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={warningSeverityDisplay(warning.severity).tone}
                            className="flex items-center gap-1 w-fit"
                          >
                            {warning.severity === "critical" && <Flame className="h-3 w-3" />}
                            {warningSeverityDisplay(warning.severity).label}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <span className={warning.days_until_expiry <= 3 ? "text-status-offline font-bold" : warning.days_until_expiry <= 7 ? "text-status-pending font-medium" : ""}>
                            {warning.days_until_expiry <= 0 ? "Expired" : `${warning.days_until_expiry} days`}
                          </span>
                        </TableCell>
                        <TableCell className="whitespace-nowrap">{formatDateTime(warning.expires_at)}</TableCell>
                        <TableCell>
                          <Button asChild size="sm">
                            <IntentLink href={inventoryLinks.certificates({ q: warning.cluster_id })}>
                              Renew
                            </IntentLink>
                          </Button>
                        </TableCell>
                      </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
                {certTotalPages > 1 && (
                  <div className="mt-4">
                    <DataTablePagination
                      currentPage={certCurrentPage}
                      totalPages={certTotalPages}
                      pageSize={certPageSize}
                      pageSizeOptions={[...TABLE_PAGE_SIZES]}
                      totalItems={sortedCertWarnings.length}
                      onPageChange={setCertPage}
                      onPageSizeChange={(size) => {
                        setCertPageSize(size);
                        setCertPage(1);
                      }}
                    />
                  </div>
                )}
                </>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Inactive clusters tab */}
        <TabsContent value="nodes">
          <Card>
            <CardHeader className="p-4 md:p-6 space-y-4">
              <div>
                <CardTitle className="text-base md:text-lg">Offline Clusters</CardTitle>
                <CardDescription className="text-xs md:text-sm">
                  Clusters that are disconnected or not responding
                </CardDescription>
              </div>
              <TableToolbar filters={nodeFilters} noun="offline clusters" />
            </CardHeader>
            <CardContent className="p-0 md:p-6 md:pt-0">
              {sortedNodeWarnings.length === 0 ? (
                /* Same reason as the certificate tab: "all nodes are online" is a claim about
                   the fleet and a filter must never be allowed to make it. */
                nodeFilters.activeFilterCount > 0 ? (
                  <div className="text-center py-8 text-muted-foreground">
                    <Wifi className="h-12 w-12 mx-auto mb-4 opacity-50" />
                    <p>No offline cluster matches the current filters</p>
                    <p className="text-sm mt-1">
                      {nodeFilters.activeFilterCount} filter
                      {nodeFilters.activeFilterCount === 1 ? "" : "s"} active. This does not mean
                      every cluster is online.
                    </p>
                    <Button variant="outline" size="sm" className="mt-4" onClick={() => nodeFilters.clearAll()}>
                      Clear filters
                    </Button>
                  </div>
                ) : (
                  <div className="text-center py-8 text-muted-foreground">
                    <Wifi className="h-12 w-12 mx-auto mb-4 opacity-50" />
                    <p>All clusters are online</p>
                    <p className="text-sm mt-1">No connectivity issues detected</p>
                  </div>
                )
              ) : (
                <>
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
                    {pagedNodeWarnings.map((warning: any) => (
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
                            <TableSubText><NodeLink clusterId={warning.cluster_id} /></TableSubText>
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
                          <Badge variant={warningSeverityDisplay(warning.severity || "critical").tone} className="flex items-center gap-1 w-fit">
                            <Flame className="h-3 w-3" />
                            {warningSeverityDisplay(warning.severity || "critical").label}
                          </Badge>
                        </TableCell>
                        {/* important: this was a tooltip on a bare span, so it opened for a mouse
                            and for nothing else, and cursor-help advertised an affordance no
                            keyboard or touch user could reach. */}
                        <TableCell className="max-w-xs">
                          <OverflowText
                            value={warning.message || "Connection failed"}
                            label={`connection message for ${warning.display_name || warning.cluster_id}`}
                            lines={2}
                          />
                        </TableCell>
                                              </TableRow>
                    ))}
                  </TableBody>
                </Table>
                {nodeTotalPages > 1 && (
                  <div className="mt-4">
                    <DataTablePagination
                      currentPage={nodeCurrentPage}
                      totalPages={nodeTotalPages}
                      pageSize={nodePageSize}
                      pageSizeOptions={[...TABLE_PAGE_SIZES]}
                      totalItems={sortedNodeWarnings.length}
                      onPageChange={setNodePage}
                      onPageSizeChange={(size) => {
                        setNodePageSize(size);
                        setNodePage(1);
                      }}
                    />
                  </div>
                )}
                </>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Own component: this page already carries three tables and their state. */}
        <TabsContent value="kernel">
          <KernelAdvisoryTab
            rows={kernelWarnings}
            unread={systemWarnings?.kernel_advisories_unread ?? []}
            failed={systemWarnings?.kernel_advisories_failed === true}
            onShowDetail={(record) => handleShowWarningDetail("kernel", record)}
          />
        </TabsContent>
      </Tabs>

      {/* One dialog for all three tabs. Certificates and Offline Nodes never had one, and
          each dropped the field that matters most: a full cluster id, and the connection
          error the table clips to two lines behind a tooltip. */}
      <WarningDetailDialog
        selected={currentSelectedWarning}
        open={warningUiIsCurrent && detailModalOpen}
        onOpenChange={(next) => {
          if (!warningUiIsCurrent) return;
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
