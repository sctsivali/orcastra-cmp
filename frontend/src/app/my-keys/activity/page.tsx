"use client";

import { Suspense, useCallback, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Download } from "lucide-react";
import { BackLink } from "@/components/ui/back-link";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { NodeLink } from "@/components/ui/node-link";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Table, TableBody, TableCell, TableHeader, TableRow } from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { IntentLink } from "@/components/navigation/route-intent";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { useRequireRole } from "@/hooks/use-require-role";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { useToast } from "@/hooks/use-toast";
import { myKeysActivityAPI } from "@/lib/my-keys-activity-api";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { defineTable, fromSort } from "@/lib/table-filter";
import { formatDateTime, formatCount } from "@/lib/utils";
import { Role } from "@/types/rbac";
import type { KeyActivityEntry } from "@/types/my-keys";
import { activityLabel, clientText } from "../components/key-activity-line";

// Every key's activity at once: the dashboard trail (created, downloaded, deleted, refused) and
// what LXD did under each key's certificate. The server window is read whole, up to a ceiling,
// and filtered in the browser, as the Sessions audit trail is (documentation/ui/pagination.md).

const PAGE = 500;
const CEILING = 5000;
const RANGES = [
  { value: "7", label: "Last 7 Days" },
  { value: "30", label: "Last 30 Days" },
  { value: "90", label: "Last 90 Days" },
  { value: "365", label: "Last 365 Days" },
] as const;

const RESULT_LABEL: Record<string, string> = { success: "Succeeded", failure: "Failed", denied: "Refused" };

const ACTIVITY = defineTable<KeyActivityEntry>()({
  id: "key-activity",
  searchPlaceholder: "Search key activity",
  defaultSort: { field: "time", direction: "desc" },
  columns: [
    { field: "time", label: "Time", sort: (e) => new Date(e.ts).getTime(), filter: fromSort("date", { horizon: "past" }) },
    {
      field: "key",
      label: "Key",
      sort: (e) => e.key_name ?? e.key_id,
      search: (e) => `${e.key_name ?? ""} ${e.key_id ?? ""}`,
      facet: (e) => e.key_name ?? e.key_id,
      facetSpec: { primary: true },
    },
    {
      field: "event",
      label: "Event",
      sort: (e) => activityLabel(e),
      search: (e) => activityLabel(e),
      facet: (e) => (e.kind === "usage" ? "On the cluster" : activityLabel(e)),
      facetSpec: { primary: true },
    },
    {
      field: "result",
      label: "Result",
      sort: (e) => e.result,
      facet: (e) => (e.result ? RESULT_LABEL[e.result] ?? e.result : null),
    },
    { field: "node", label: "Cluster", facet: (e) => e.cluster_id, facetSpec: { role: "node" } },
    { field: "project", label: "Project", facet: (e) => e.project, facetSpec: { role: "project" } },
    { field: "actor", label: "By", sort: (e) => e.actor_email, search: (e) => e.actor_email, filter: fromSort("text") },
    { field: "client", label: "Client", sort: (e) => clientText(e), search: (e) => clientText(e), filter: fromSort("text") },
  ],
});

function KeyActivityContent() {
  const { allowed, isResolved } = useRequireRole(Role.PARTNER);
  const { scopeKey } = useActorQueryScope();
  const { toast } = useToast();
  const keyId = useSearchParams().get("key") ?? undefined;
  const [range, setRange] = useState<string>("30");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const [exporting, setExporting] = useState(false);
  const since = useMemo(() => new Date(Date.now() - Number(range) * 86_400_000).toISOString(), [range]);

  const { data, isLoading, isError, error, refreshNow, dataUpdatedAt, refetch } = useRefreshableQuery({
    queryKey: ["my-key-activity", scopeKey, "log", keyId ?? null, range],
    queryFn: async ({ signal }) => {
      const rows: KeyActivityEntry[] = [];
      let total = 0;
      for (let offset = 0; ; ) {
        const page = await myKeysActivityAPI.list({ keyId, since, limit: PAGE, offset }, { signal });
        rows.push(...page.entries);
        total = page.total;
        offset += page.entries.length;
        // note: the empty page ends it too; has_more comes from a count taken before the read.
        if (!page.has_more || page.entries.length === 0 || rows.length >= CEILING) break;
      }
      return { rows, total };
    },
    enabled: allowed,
    staleTime: 30_000,
  });

  const resetPage = useCallback(() => setPage(1), []);
  const filters = useTableFilters(ACTIVITY, data?.rows ?? [], { onFilterChange: resetPage });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    ACTIVITY.sortAccessors,
    ACTIVITY.defaultSort.field,
    ACTIVITY.defaultSort.direction,
    resetPage,
  );
  const sorted = useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const paged = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const exportCsv = async () => {
    setExporting(true);
    try {
      const blob = await myKeysActivityAPI.exportCsv({ keyId, since });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `key-activity-${new Date().toISOString().split("T")[0]}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (failure) {
      toast({ title: "Could not export key activity", description: failure instanceof Error ? failure.message : "Try again.", variant: "destructive" });
    } finally {
      setExporting(false);
    }
  };

  if (!isResolved) return <LoadingLottiePage />;
  if (!allowed) return null;

  return (
    <div className="space-y-6 p-4 sm:p-8">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:gap-4">
          <BackLink href="/my-keys" label="My Keys" />
          <div className="min-w-0">
            <h1 className="text-2xl font-bold sm:text-3xl">Key Activity</h1>
            <p className="mt-1 text-sm text-muted-foreground sm:text-base">
              Who created, downloaded or deleted each key, and what was changed on a cluster with it. Reads such as a list
              leave no record.
            </p>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Select value={range} onValueChange={(value) => { setRange(value); resetPage(); }}>
            <SelectTrigger className="h-9 w-40" aria-label="Time range">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {RANGES.map((option) => (
                <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <RefreshButton label="Refresh key activity" lastUpdatedAt={dataUpdatedAt} onRefresh={refreshNow} />
          <Button size="sm" variant="outline" onClick={() => void exportCsv()} disabled={exporting || !data?.rows.length}>
            <Download className="mr-2 h-4 w-4" aria-hidden="true" />
            {exporting ? "Exporting…" : "Export CSV"}
          </Button>
        </div>
      </div>

      {keyId && (
        <p className="text-sm text-muted-foreground">
          Showing one key.{" "}
          <IntentLink href="/my-keys/activity" className="underline decoration-dotted underline-offset-4 hover:decoration-solid">
            Show every key
          </IntentLink>
        </p>
      )}

      <Card>
        <CardHeader>
          <TableToolbar filters={filters} noun="events" />
        </CardHeader>
        <CardContent className="space-y-4">
          {data && data.total > data.rows.length && (
            <PartialReadNotice data-testid="key-activity-partial">
              Showing the newest {formatCount(data.rows.length)} of {formatCount(data.total)} events.
              Narrow the time range to read the rest, or export them.
            </PartialReadNotice>
          )}
          {isLoading ? (
            <LoadingLottiePage />
          ) : isError && !data ? (
            <ReadErrorState noun="key activity" error={error} onRetry={() => void refetch()} />
          ) : (data?.rows.length ?? 0) === 0 ? (
            <EmptyState icon={Download} title="No key activity" description="Nothing was recorded for these keys in this time range." />
          ) : (
            <>
              <Table data-testid="key-activity-table">
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="Time" field="time" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Key" field="key" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Event" field="event" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Result" field="result" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Cluster" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="By" field="actor" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Client" field="client" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paged.length === 0 ? (
                    <TableEmptyRow colSpan={8} title="No event matches the current filters" />
                  ) : (
                    paged.map((entry) => (
                      <TableRow key={entry.id} data-row-key={entry.id} data-kind={entry.kind}>
                        <TableCell className="whitespace-nowrap">{formatDateTime(entry.ts)}</TableCell>
                        <TableCell>
                          {entry.key_name ?? entry.key_id ?? <TableEmptyValue />}
                          {entry.key_name && entry.key_id && <TableSubText>{entry.key_id}</TableSubText>}
                        </TableCell>
                        <TableCell>
                          {activityLabel(entry)}
                          {entry.kind === "usage" && <TableSubText>With this key&apos;s certificate</TableSubText>}
                        </TableCell>
                        <TableCell>{entry.result ? RESULT_LABEL[entry.result] ?? entry.result : <TableEmptyValue />}</TableCell>
                        <TableCell className="whitespace-nowrap">
                          {entry.cluster_id ? <NodeLink clusterId={entry.cluster_id} /> : <TableEmptyValue />}
                        </TableCell>
                        <TableCell>{entry.project ?? <TableEmptyValue />}</TableCell>
                        <TableCell className="break-all">{entry.actor_email ?? <TableEmptyValue />}</TableCell>
                        <TableCell className="break-all text-muted-foreground">{clientText(entry) ?? <TableEmptyValue />}</TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
              {totalPages > 1 && (
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
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// The role gate stays in this file: check-role-gates.mjs reads page text and does not follow
// imports. Suspense is required because useTableFilters and the key parameter read useSearchParams.
export default function KeyActivityPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <KeyActivityContent />
    </Suspense>
  );
}
