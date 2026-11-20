"use client";

import { Suspense, useCallback, useMemo, useState } from "react";
import { Download, ScrollText, ShieldCheck, ShieldX } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Table, TableBody, TableCell, TableHeader, TableRow } from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { useToast } from "@/hooks/use-toast";
import { authHeaders } from "@/lib/api";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { defineTable, fromSort } from "@/lib/table-filter";
import { formatCount, formatDateTimeSeconds } from "@/lib/utils";
import { miniApi, saveBlob, type AuditRow, type AuditVerifyReport } from "./api";

// The audit window is read whole from the server, up to a ceiling, and filtered in the browser,
// as Key Activity and the Sessions audit trail are. The export reads the same window server-side,
// uncapped by the ceiling.
const PAGE = 1000;
const CEILING = 5000;
const RANGES = [
  { value: "1", label: "Last 24 Hours" },
  { value: "7", label: "Last 7 Days" },
  { value: "30", label: "Last 30 Days" },
  { value: "90", label: "Last 90 Days" },
  { value: "365", label: "Last 365 Days" },
] as const;
const RESULT_LABEL: Record<string, string> = { success: "Succeeded", failure: "Failed", denied: "Refused" };

const targetText = (r: AuditRow) =>
  [r.target_type, r.target_host, r.target_project, r.target_id].filter(Boolean).join(" / ");

const AUDIT = defineTable<AuditRow>()({
  id: "mini-audit-log",
  searchPlaceholder: "Search audit events",
  defaultSort: { field: "time", direction: "desc" },
  columns: [
    { field: "time", label: "Time", sort: (r) => new Date(r.occurred_at).getTime(), filter: fromSort("date", { horizon: "past" }) },
    {
      field: "actor", label: "Actor", sort: (r) => r.actor_email ?? "", search: (r) => r.actor_email ?? "",
      facet: (r) => r.actor_email, facetSpec: { primary: true, emptyLabel: "(system)" },
    },
    {
      field: "action", label: "Action", sort: (r) => r.action, search: (r) => r.action,
      facet: (r) => r.action,
    },
    { field: "target", label: "Target", sort: (r) => targetText(r), search: (r) => targetText(r), filter: fromSort("text") },
    {
      field: "result", label: "Result", sort: (r) => r.result ?? "",
      facet: (r) => (r.result ? RESULT_LABEL[r.result] ?? r.result : null), facetSpec: { primary: true },
    },
    { field: "source", label: "Source", sort: (r) => r.source_ip ?? "", search: (r) => r.source_ip ?? "", filter: fromSort("text") },
  ],
});

function AuditLogContent() {
  const { scopeKey } = useActorQueryScope();
  const { toast } = useToast();
  const [range, setRange] = useState<string>("7");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const [verifying, setVerifying] = useState(false);
  const [report, setReport] = useState<AuditVerifyReport | null>(null);
  const [exporting, setExporting] = useState<"csv" | "json" | null>(null);
  const since = useMemo(() => new Date(Date.now() - Number(range) * 86_400_000).toISOString(), [range]);

  const { data, isLoading, isError, error, refreshNow, dataUpdatedAt, refetch } = useRefreshableQuery({
    queryKey: ["mini-audit-log", scopeKey, range],
    queryFn: async () => {
      const rows: AuditRow[] = [];
      let total = 0;
      for (let p = 1; rows.length < CEILING; p += 1) {
        const chunk = await miniApi.auditLog({ page: p, page_size: PAGE, start: since });
        rows.push(...chunk.items);
        total = chunk.total;
        if (chunk.items.length < PAGE) break;
      }
      return { rows, total };
    },
    staleTime: 30_000,
  });
  const status = useRefreshableQuery({ queryKey: ["mini-audit-status", scopeKey], queryFn: () => miniApi.auditStatus() });

  const resetPage = useCallback(() => setPage(1), []);
  const filters = useTableFilters(AUDIT, data?.rows ?? [], { onFilterChange: resetPage });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    AUDIT.sortAccessors, AUDIT.defaultSort.field, AUDIT.defaultSort.direction, resetPage,
  );
  const sorted = useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const paged = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const verify = async () => {
    setVerifying(true);
    try {
      setReport(await miniApi.verifyAudit());
    } catch (failure) {
      toast({ title: "Verification did not run", description: failure instanceof Error ? failure.message : "Try again.", variant: "destructive" });
    } finally {
      setVerifying(false);
    }
  };

  const exportAs = async (format: "csv" | "json") => {
    setExporting(format);
    try {
      const response = await fetch(miniApi.auditExportUrl(format, { start: since }), { headers: authHeaders(), cache: "no-store" });
      if (!response.ok) throw new Error(response.status === 503 ? "The export could not be audited, so it did not run." : "The export failed. Try again.");
      const name = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? `audit.${format}`;
      saveBlob(await response.blob(), name);
    } catch (failure) {
      toast({ title: "Nothing was exported", description: failure instanceof Error ? failure.message : "Try again.", variant: "destructive" });
    } finally {
      setExporting(null);
    }
  };

  const writerTrouble = status.data ? status.data.writer.dropped + status.data.writer.failed : 0;

  return (
    <div className="space-y-6 p-4 sm:p-8">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold sm:text-3xl">Audit Log</h1>
          <p className="mt-1 text-sm text-muted-foreground sm:text-base">
            Every sign-in and change, in a hash-chained log that shows any edit, reorder or deletion.
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Select value={range} onValueChange={(value) => { setRange(value); resetPage(); }}>
            <SelectTrigger className="h-9 w-40" aria-label="Time range"><SelectValue /></SelectTrigger>
            <SelectContent>
              {RANGES.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}
            </SelectContent>
          </Select>
          <RefreshButton label="Refresh the audit log" lastUpdatedAt={dataUpdatedAt} onRefresh={refreshNow} />
          <Button size="sm" variant="outline" onClick={() => void verify()} disabled={verifying}>
            <ShieldCheck className="mr-2 h-4 w-4" aria-hidden="true" />{verifying ? "Verifying…" : "Verify Integrity"}
          </Button>
          <Button size="sm" variant="outline" onClick={() => void exportAs("csv")} disabled={exporting !== null}>
            <Download className="mr-2 h-4 w-4" aria-hidden="true" />{exporting === "csv" ? "Exporting…" : "Export CSV"}
          </Button>
          <Button size="sm" variant="outline" onClick={() => void exportAs("json")} disabled={exporting !== null}>
            <Download className="mr-2 h-4 w-4" aria-hidden="true" />{exporting === "json" ? "Exporting…" : "Export JSON"}
          </Button>
        </div>
      </div>

      <div aria-live="polite">
        {report && (
          <Alert variant={report.valid ? "default" : "destructive"}>
            {report.valid ? <ShieldCheck className="h-4 w-4" aria-hidden="true" /> : <ShieldX className="h-4 w-4" aria-hidden="true" />}
            <AlertDescription>
              {report.valid
                ? `The chain is intact: ${formatCount(report.checked)} rows verified through id ${report.max_id}.`
                : `The chain is broken. ${report.note ?? ""} Export the log now and keep the export before anything else changes.`}
            </AlertDescription>
          </Alert>
        )}
      </div>

      {status.data && (
        <p className="text-sm text-muted-foreground">
          {formatCount(status.data.rows)} rows kept;{" "}
          {status.data.retention_days ? `rows older than ${status.data.retention_days} days are removed daily.` : "none are removed."}
          {writerTrouble > 0 && ` ${formatCount(writerTrouble)} events could not be written: see Host Health.`}
        </p>
      )}

      <Card>
        <CardHeader><TableToolbar filters={filters} noun="events" /></CardHeader>
        <CardContent className="space-y-4">
          {data && data.total > data.rows.length && (
            <PartialReadNotice>
              Showing the newest {formatCount(data.rows.length)} of {formatCount(data.total)} events.
              Narrow the time range to read the rest, or export them.
            </PartialReadNotice>
          )}
          {isLoading ? (
            <LoadingLottiePage />
          ) : isError && !data ? (
            <ReadErrorState noun="audit events" error={error} onRetry={() => void refetch()} />
          ) : (data?.rows.length ?? 0) === 0 ? (
            <EmptyState icon={ScrollText} title="No audit events" description="Nothing was recorded in this time range." />
          ) : (
            <>
              <Table data-testid="mini-audit-table">
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="Time" field="time" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Actor" field="actor" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Action" field="action" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Target" field="target" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Result" field="result" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Source" field="source" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paged.length === 0 ? (
                    <TableEmptyRow colSpan={6} title="No event matches the current filters" />
                  ) : (
                    paged.map((row) => (
                      <TableRow key={row.id} data-row-key={row.id}>
                        <TableCell className="whitespace-nowrap">{formatDateTimeSeconds(row.occurred_at)}</TableCell>
                        <TableCell className="break-all">
                          {row.actor_email ?? <TableEmptyValue />}
                          {row.actor_role && <TableSubText>{row.actor_role}</TableSubText>}
                        </TableCell>
                        <TableCell className="break-all font-mono" translate="no">{row.action}</TableCell>
                        <TableCell className="break-all">{targetText(row) || <TableEmptyValue />}</TableCell>
                        <TableCell>{row.result ? RESULT_LABEL[row.result] ?? row.result : <TableEmptyValue />}</TableCell>
                        <TableCell className="break-all">{row.source_ip ?? <TableEmptyValue />}</TableCell>
                      </TableRow>
                    ))
                  )}
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
    </div>
  );
}

// Suspense: useTableFilters reads useSearchParams.
export function AuditLogView(): JSX.Element {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <AuditLogContent />
    </Suspense>
  );
}
