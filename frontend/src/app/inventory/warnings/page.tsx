"use client";

import { useState, useMemo, useEffect, useCallback, useRef, Suspense } from "react";
import { usePathname, useSearchParams, useRouter } from "next/navigation";
import { currentQuery, replaceQuery } from "@/lib/url-state";
import { IntentLink } from "@/components/navigation/route-intent";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { AlarmCount } from "@/components/dashboard/alarm-tile";
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
import { EmptyState } from "@/components/ui/empty-state";
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
import { formatDateTime, formatLabel, formatCount, plural } from "@/lib/utils";
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

// Each tab has its own table definition and URL namespace: shared filter state would filter one
// tab by a field another does not carry.
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
      // important: keyed on the registered node, not `location`. On a clustered host `location`
      // is the LXD member name, which no link into this page carries, so a link with the cluster
      // id would match nothing. The member is rendered as sub-text and searched.
      sort: (w) => warningNodeValue(w),
      facet: (w) => warningNodeValue(w),
      facetSpec: { role: "node", primary: true, emptyLabel: "(no cluster)" } },
    // Facet only: the table has no Project column; the detail dialog shows it.
    { field: "project", label: "Project", facet: (w) => w.project, facetSpec: { role: "project", emptyLabel: "(no project)" } },
    { field: "count", label: "Count", sort: (w) => w.count ?? 0, filter: fromSort("number", { unit: "count" }) },
    { field: "last_seen", label: "Last Seen", sort: (w) => toTime(w.last_seen_at), filter: fromSort("date", { horizon: "past" }) },
  ] });

const CERT_WARNINGS = defineTable<CertificateWarning>()({
  id: "certificates",
  searchPlaceholder: "Search certificate issues",
  // important: sort and facet stay on the display name. A facet value is both the URL value and
  // the option label, so widening it would rename the options and break existing links. The
  // cluster id is searchable here instead; the accessors use it only without a display name.
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
  // important: sort and facet stay on the display name. A facet value is both the URL value and
  // the option label, so widening it would rename the options and break existing links. The
  // cluster id is searchable here instead; the accessors use it only without a display name.
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
    // CertificateWarning carries the connection error in `message`.
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

  const certWarnings = useMemo(() => systemWarnings?.certificate_warnings || [], [systemWarnings]);
  const nodeWarnings = useMemo(() => systemWarnings?.node_warnings || [], [systemWarnings]);
  const kernelWarnings = useMemo(() => systemWarnings?.kernel_advisories || [], [systemWarnings]);
  
  // important: the tab is URL state, read on every render and written on change. A local state
  // seeded from ?tab= would bounce the operator back to the platform tab on a facet toggle.
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

  // Bare ?severity= and ?node= are an external contract: translated once into the platform tab's
  // keys, then removed so the seed cannot reassert itself when the operator clears the control.
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


  // A pinned role is exempt from the two-value threshold, so only availableFields can drop it;
  // otherwise a tab with no rows shows its node control with an empty popover. An empty
  // certificates tab is the normal state.
  //
  // important: the gate names the field, not the role. Two of these three tables call the node
  // dimension `cluster`.
  const gate = <F extends string>(fields: readonly F[], field: F, present: boolean) =>
    present ? fields : fields.filter((f) => f !== field);

  // Paging is per tab, like the filters: one shared page number would land the operator past the
  // end of a shorter table on switching tabs.
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

  // Sorting is per table too.
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

  
  // The kind travels with the selected record, because each tab carries its own shape and the
  // dialog renders each one differently.
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

  // A failed read is not an empty list. Either feed failing with nothing cached fails the whole
  // page: no tab can say "no warnings" about a read that did not return.
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
  
  // Awaited, so the refresh button sees completion and failure: a refresh against a dead backend
  // must not look like a successful one.
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

      <UnreachableNodesBanner
        noun="warnings"
        nodes={reach.unreachable}
        partialNodes={reach.partial}
        lateNodes={reach.late}
        reasons={reach.reasons}
      />

      {/* Summary counts: neutral, toned on the icon only while a count is above zero */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-4">
        <AlarmCount
          kind="total"
          count={allWarnings.length + certWarnings.length + nodeWarnings.length + kernelWarnings.length}
        />
        <AlarmCount kind="critical" count={systemWarnings?.critical || 0} />
        <AlarmCount kind="certificates" count={certWarnings.length} />
        <AlarmCount kind="offlineClusters" count={nodeWarnings.length} />
      </div>

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
            <span className="hidden sm:inline">Kernel CVEs</span>
            <span className="sm:hidden">CVEs</span>
            <span className="text-[10px] md:text-xs">({kernelWarnings.length})</span>
          </TabsTrigger>
        </TabsList>

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
            <EmptyState
              title={
                platformFilters.activeFilterCount > 0
                  ? "No warning matches the current filters."
                  : "No warnings found"
              }
            />
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
                      {formatCount(warning.count)}
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

        <TabsContent value="certificates">
          <Card>
            <CardHeader className="p-4 md:p-6 space-y-4">
              <div>
                <CardTitle className="text-base md:text-lg">Certificate Issues</CardTitle>
                <CardDescription className="text-xs md:text-sm">
                  Certificates expiring soon or already expired
                </CardDescription>
              </div>
              <TableToolbar filters={certFilters} noun="certificate issues" />
            </CardHeader>
            <CardContent className="p-0 md:p-6 md:pt-0">
              {sortedCertWarnings.length === 0 ? (
                /* "All healthy" is a claim about the fleet, so it must not be made about a list
                   that a filter emptied. */
                certFilters.activeFilterCount > 0 ? (
                  <EmptyState
                    icon={Shield}
                    title="No certificate issue matches the current filters"
                    description={`${plural(certFilters.activeFilterCount, "filter")} active. This is not a clean bill of health.`}
                    action={
                      <Button variant="outline" size="sm" onClick={() => certFilters.clearAll()}>
                        Clear filters
                      </Button>
                    }
                  />
                ) : (
                  <EmptyState
                    icon={Shield}
                    title="No certificate issues found"
                    description="All certificates are healthy"
                  />
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
                      <TableHead><span className="sr-only">Actions</span></TableHead>
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
                            {warning.days_until_expiry <= 0 ? "Expired" : plural(warning.days_until_expiry, "day")}
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
                /* Same reason as the certificate tab: "all clusters are online" is a claim
                   about the fleet, and a filter must not make it. */
                nodeFilters.activeFilterCount > 0 ? (
                  <EmptyState
                    icon={Wifi}
                    title="No offline cluster matches the current filters"
                    description={`${plural(nodeFilters.activeFilterCount, "filter")} active. This does not mean every cluster is online.`}
                    action={
                      <Button variant="outline" size="sm" onClick={() => nodeFilters.clearAll()}>
                        Clear filters
                      </Button>
                    }
                  />
                ) : (
                  <EmptyState
                    icon={Wifi}
                    title="No connectivity issues detected"
                    description="All clusters are online"
                  />
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
                        {/* OverflowText rather than a hover tooltip, so keyboard and touch
                            users can reach the full message. */}
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
            unreadReasons={systemWarnings?.kernel_advisories_errors}
            failed={systemWarnings?.kernel_advisories_failed === true}
            onShowDetail={(record) => handleShowWarningDetail("kernel", record)}
          />
        </TabsContent>
      </Tabs>

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

// The role gate sits above the Suspense boundary, so nothing below mounts for another role.
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
