"use client";

import { Suspense, useCallback, useMemo, useState } from "react";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { NodeLink } from "@/components/ui/node-link";
import { OverflowText } from "@/components/ui/overflow-text";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Table, TableBody, TableCell, TableHeader, TableRow } from "@/components/ui/table";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { KERNEL_STATE_DISPLAY } from "@/components/kernel-advisory/kernel-advisory-display";
import { KernelCveUbuntuStatus } from "@/components/kernel-advisory/kernel-cve-ubuntu-status";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { defineTable, fromSort } from "@/lib/table-filter";
import { SEVERITY_RANK } from "@/lib/sort";
import { warningClusterValue } from "@/lib/warning-facets";
import type { KernelAdvisoryState, KernelAdvisoryWarning } from "@/types/kernel-advisory";
import { warningSeverityDisplay, warningSeverityLabel } from "./warning-severity";

const STATE_ORDER: KernelAdvisoryState[] = ["exposed", "unverified", "patched"];

const KERNEL_WARNINGS = defineTable<KernelAdvisoryWarning>()({
  id: "kernel",
  searchPlaceholder: "Search kernel CVEs",
  // the cluster id, the member and the kernel string are what an operator copies from a shell
  searchExtra: (w) => [w.cluster_id, w.member, w.kernel_version ?? ""],
  defaultSort: { field: "state", direction: "asc" },
  columns: [
    {
      field: "cluster",
      label: "Cluster",
      sort: (w) => warningClusterValue(w),
      search: (w) => warningClusterValue(w),
      facet: (w) => warningClusterValue(w),
      facetSpec: { role: "node" } },
    { field: "member", label: "Cluster Member", sort: (w) => w.member, search: (w) => w.member, filter: fromSort("text") },
    // one row per member and CVE, so the id is what tells two rows of one member apart
    { field: "cve", label: "CVE", sort: (w) => w.advisory_id, search: (w) => w.advisory_id, facet: (w) => w.advisory_id },
    { field: "kernel", label: "Kernel", sort: (w) => w.kernel_version ?? "", search: (w) => w.kernel_version ?? "", filter: fromSort("text") },
    {
      field: "state",
      label: "State",
      sort: (w) => STATE_ORDER.indexOf(w.state),
      facet: (w) => w.state,
      facetSpec: { primary: true, format: (v: string) => KERNEL_STATE_DISPLAY[v as KernelAdvisoryState]?.label ?? v, order: STATE_ORDER } },
    {
      field: "severity",
      label: "Severity",
      sort: (w) => SEVERITY_RANK[w.severity] ?? 99,
      facet: (w) => w.severity,
      facetSpec: { primary: true, format: warningSeverityLabel } },
    { field: "message", label: "Reason", sort: (w) => w.message, search: (w) => w.message, filter: fromSort("text") },
  ] });

interface KernelAdvisoryTabProps {
  rows: KernelAdvisoryWarning[];
  /** Clusters whose kernels were not read: named, so a short list is never mistaken for a clean one. */
  unread?: string[];
  /** The assessment failed outright: the empty state must not claim every kernel is patched. */
  failed?: boolean;
  onShowDetail: (record: KernelAdvisoryWarning) => void;
}

/** The Kernel CVEs tab: one row per member and CVE that is affected or cannot be confirmed patched. */
export function KernelAdvisoryTab(props: KernelAdvisoryTabProps) {
  // note: the filters live in the query string. The page already sits in a Suspense boundary,
  // but the boundary belongs beside the reader so this tab stays correct wherever it is mounted.
  return (
    <Suspense fallback={null}>
      <KernelAdvisoryTable {...props} />
    </Suspense>
  );
}

function KernelAdvisoryTable({
  rows,
  unread = [],
  failed = false,
  onShowDetail,
}: KernelAdvisoryTabProps) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);
  const filters = useTableFilters(KERNEL_WARNINGS, rows, { onFilterChange: resetPage });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    KERNEL_WARNINGS.sortAccessors,
    KERNEL_WARNINGS.defaultSort.field,
    KERNEL_WARNINGS.defaultSort.direction,
    resetPage,
  );
  const sorted = useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const paged = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return (
    <Card>
      <CardHeader className="p-4 md:p-6 space-y-4">
        <div>
          <CardTitle className="text-base md:text-lg">Kernel CVEs</CardTitle>
          <CardDescription className="text-xs md:text-sm">
            Cluster members whose running kernel is affected by a known kernel CVE, or could not be confirmed as patched
          </CardDescription>
          <div className="mt-2">
            <KernelCveUbuntuStatus />
          </div>
        </div>
        <UnreachableNodesBanner noun="member kernels" nodes={unread} />
        <TableToolbar filters={filters} noun="kernel CVE findings" />
      </CardHeader>
      <CardContent className="p-0 md:p-6 md:pt-0">
        {sorted.length === 0 ? (
          filters.activeFilterCount > 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              <ShieldAlert className="h-12 w-12 mx-auto mb-4 opacity-50" />
              <p>No kernel CVE matches the current filters</p>
              <p className="text-sm mt-1">
                {filters.activeFilterCount} filter{filters.activeFilterCount === 1 ? "" : "s"} active. This does not
                mean every kernel is patched.
              </p>
              <Button variant="outline" size="sm" className="mt-4" onClick={() => filters.clearAll()}>
                Clear filters
              </Button>
            </div>
          ) : failed ? (
            <div className="text-center py-8 text-muted-foreground">
              <ShieldAlert className="h-12 w-12 mx-auto mb-4 opacity-50" />
              <p>Kernel CVEs could not be assessed</p>
              <p className="text-sm mt-1">This is not an all-clear. Refresh the page, or read a cluster&apos;s kernels from its card.</p>
            </div>
          ) : (
            <div className="text-center py-8 text-muted-foreground">
              <ShieldCheck className="h-12 w-12 mx-auto mb-4 opacity-50" />
              <p>No open kernel CVEs</p>
              <p className="text-sm mt-1">Every cluster member that answered runs a kernel with the known fixes</p>
            </div>
          )
        ) : (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead className="min-w-[120px]" label="Cluster" field="cluster" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead className="min-w-[120px]" label="Cluster Member" field="member" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead className="min-w-[130px]" label="CVE" field="cve" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead className="min-w-[160px]" label="Kernel" field="kernel" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead className="min-w-[90px]" label="State" field="state" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead className="min-w-[90px]" label="Severity" field="severity" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead className="min-w-[200px]" label="Reason" field="message" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                </TableRow>
              </TableHeader>
              <TableBody>
                {paged.map((warning) => {
                  const severity = warningSeverityDisplay(warning.severity);
                  return (
                    <TableRow key={`${warning.cluster_id}/${warning.member}/${warning.advisory_id}`}>
                      <TableCell>
                        <p className="font-medium">
                          <TableLink
                            onClick={() => onShowDetail(warning)}
                            aria-label={`Show ${warning.advisory_id} for ${warning.member} on ${warningClusterValue(warning)}`}
                          >
                            {warningClusterValue(warning)}
                          </TableLink>
                        </p>
                        <TableSubText><NodeLink clusterId={warning.cluster_id} /></TableSubText>
                      </TableCell>
                      <TableCell>{warning.member}</TableCell>
                      <TableCell className="whitespace-nowrap">{warning.advisory_id}</TableCell>
                      <TableCell>
                        {warning.kernel_version ? (
                          <span className="font-mono whitespace-nowrap">{warning.kernel_version}</span>
                        ) : (
                          <TableEmptyValue label="Not read" />
                        )}
                      </TableCell>
                      <TableCell className="whitespace-nowrap">{KERNEL_STATE_DISPLAY[warning.state].label}</TableCell>
                      <TableCell className="whitespace-nowrap">
                        <Badge variant={severity.tone} className="w-fit">{severity.label}</Badge>
                      </TableCell>
                      <TableCell className="max-w-xs">
                        <OverflowText value={warning.message} label={`reason for ${warning.member}`} lines={2} />
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
            {totalPages > 1 && (
              <div className="mt-4">
                <DataTablePagination
                  currentPage={currentPage}
                  totalPages={totalPages}
                  pageSize={pageSize}
                  pageSizeOptions={[...TABLE_PAGE_SIZES]}
                  totalItems={sorted.length}
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
  );
}
