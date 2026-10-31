"use client";

import { CollectionNotice } from "./collection-notice";

import * as React from "react";
import { Copy, ScrollText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { NodeLink } from "@/components/ui/node-link";
import { OverflowText } from "@/components/ui/overflow-text";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyRow, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { toast } from "@/hooks/use-toast";
import { copyText } from "@/lib/clipboard";
import { defineTable, fromSort } from "@/lib/table-filter";
import { formatClockTime, formatDate, formatDateTime, formatLabel, toMillis } from "@/lib/utils";
import type { ClusterLogEntry } from "@/types/tasks";
import { useClusterLog } from "./hooks/use-tasks";
import { PanelSkeleton, skeletonRowsFor } from "./panel-skeleton";
import { SeverityCell } from "./task-cells";
import { SEVERITY_ORDER, logActorLabel, logSeverityLabel } from "./task-format";
import type { TaskFeedSnapshot } from "./use-task-feed";

const FIRST_PAGE = 200;
const MAX_ROWS = 500;
/** How far from the top still counts as following the tail. */
const FOLLOW_SLACK_PX = 8;

const LOG = defineTable<ClusterLogEntry>()({
  id: "docklog",
  searchPlaceholder: "Search log",
  defaultSort: { field: "time", direction: "desc" },
  columns: [
    { field: "time", label: "Time", sort: (e) => toMillis(e.ts), filter: fromSort("date", { horizon: "past" }) },
    {
      field: "severity",
      label: "Severity",
      sort: (e) => e.severity,
      facet: (e) => e.severity,
      facetSpec: { primary: true, format: logSeverityLabel, order: SEVERITY_ORDER },
    },
    { field: "kind", label: "Kind", sort: (e) => e.kind, facet: (e) => e.kind, facetSpec: { format: formatLabel } },
    {
      field: "node",
      label: "Cluster",
      sort: (e) => e.cluster_id,
      facet: (e) => e.cluster_id,
      facetSpec: { role: "node", primary: true, emptyLabel: "No cluster" },
    },
    {
      field: "project",
      label: "Project",
      sort: (e) => e.project,
      facet: (e) => e.project,
      facetSpec: { role: "project", emptyLabel: "No project" },
    },
    { field: "actor", label: "Actor", sort: (e) => logActorLabel(e), search: (e) => logActorLabel(e), filter: fromSort("text") },
    { field: "message", label: "Message", sort: (e) => e.message, search: (e) => e.message, filter: fromSort("text") },
  ],
});

const COLUMN_COUNT = LOG.columns.length + 1;

export interface LogTabProps {
  feed: TaskFeedSnapshot;
  height: number;
  onOpenTask: (id: string) => void;
  registerRefresh: (refresh: (() => Promise<unknown>) | null) => void;
  collectionIncomplete: boolean;
}

function logLine(entry: ClusterLogEntry): string {
  return `${formatDateTime(entry.ts)} [${logSeverityLabel(entry.severity)}] ${entry.message}`;
}

function dayOf(ts: string): string {
  const ms = toMillis(ts);
  return ms === null ? "" : new Date(ms).toDateString();
}

function LogRow({
  entry,
  showDate,
  onOpenTask,
}: {
  entry: ClusterLogEntry;
  showDate: boolean;
  onOpenTask: (id: string) => void;
}) {
  const copyLine = async () => {
    const ok = await copyText(logLine(entry));
    toast(
      ok
        ? { title: "Copied", description: "Log line copied to clipboard." }
        : { title: "Copy failed", description: "Select the line and copy it manually.", variant: "destructive" },
    );
  };
  return (
    <TableRow data-log-id={entry.id} data-log-severity={entry.severity}>
      <TableCell className="whitespace-nowrap">
        {formatClockTime(toMillis(entry.ts))}
        {showDate ? <TableSubText>{formatDate(entry.ts)}</TableSubText> : null}
      </TableCell>
      <SeverityCell severity={entry.severity} />
      <TableCell className="whitespace-nowrap">{formatLabel(entry.kind)}</TableCell>
      <TableCell className="whitespace-nowrap">
        <NodeLink clusterId={entry.cluster_id} />
      </TableCell>
      <TableCell className="whitespace-nowrap">
        {entry.project ?? <TableEmptyValue label="No project" />}
      </TableCell>
      <TableCell className="max-w-[12rem]">
        <OverflowText value={logActorLabel(entry)} label={`actor of log entry ${entry.id}`} lines={1} />
      </TableCell>
      <TableCell className="max-w-[28rem]">
        <OverflowText value={entry.message} label={`message of log entry ${entry.id}`} lines={1}>
          {entry.task_id
            ? (expanded) => (
                <TableLink
                  wrap={expanded}
                  onClick={() => onOpenTask(entry.task_id as string)}
                  aria-label={`Show the task behind: ${entry.message}`}
                >
                  {entry.message}
                </TableLink>
              )
            : undefined}
        </OverflowText>
      </TableCell>
      <TableCell pinned="end" className="text-right">
        <Button type="button" variant="ghost" size="icon" className="h-6 w-6" aria-label="Copy log line" onClick={() => void copyLine()}>
          <Copy className="h-3.5 w-3.5" aria-hidden="true" />
        </Button>
      </TableCell>
    </TableRow>
  );
}

function LogTabInner({ feed, height, onOpenTask, registerRefresh, collectionIncomplete }: LogTabProps) {
  const [visibleLimit, setVisibleLimit] = React.useState(FIRST_PAGE);
  const query = useClusterLog(true, feed.state === "connected", visibleLimit);
  const rows = React.useMemo(() => query.data?.entries ?? [], [query.data]);

  const filters = useTableFilters(LOG, rows, { syncUrl: false });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    LOG.sortAccessors,
    LOG.defaultSort.field,
    LOG.defaultSort.direction,
  );
  const sorted = React.useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);

  const { refreshNow } = query;
  React.useEffect(() => {
    registerRefresh(refreshNow);
    return () => registerRefresh(null);
  }, [registerRefresh, refreshNow]);

  // Follow the tail: newest on top, so following means sitting at the top. Scrolled away, new
  // entries still enter the cache and are counted against the newest id seen while following.
  const wrapperRef = React.useRef<HTMLDivElement>(null);
  const [follow, setFollow] = React.useState(true);
  const [anchorId, setAnchorId] = React.useState<number | null>(null);
  const newestId = rows.length ? Math.max(...rows.map((e) => e.id)) : null;

  React.useEffect(() => {
    if (follow) setAnchorId(newestId);
  }, [follow, newestId]);

  React.useEffect(() => {
    const container = wrapperRef.current?.querySelector<HTMLDivElement>('[data-slot="table-container"]');
    if (!container) return;
    const onScroll = () => setFollow(container.scrollTop <= FOLLOW_SLACK_PX);
    container.addEventListener("scroll", onScroll, { passive: true });
    return () => container.removeEventListener("scroll", onScroll);
  }, [query.isLoading]);

  const newCount = follow || anchorId === null ? 0 : rows.filter((e) => e.id > anchorId).length;

  const jumpToTop = () => {
    const container = wrapperRef.current?.querySelector<HTMLDivElement>('[data-slot="table-container"]');
    container?.scrollTo({ top: 0 });
    setFollow(true);
  };

  if (query.isLoading && !query.data) return <PanelSkeleton rows={skeletonRowsFor(height)} />;

  const hasMore = query.data?.has_more ?? false;
  const canShowMore = hasMore && visibleLimit < MAX_ROWS;
  const atCap = hasMore && visibleLimit >= MAX_ROWS;
  let previousDay = "";

  return (
    <div ref={wrapperRef} className="relative flex min-h-0 flex-1 flex-col">
      <CollectionNotice incomplete={collectionIncomplete} />
      <TableToolbar filters={filters} noun="entries" className="px-3 py-2" />
      {newCount > 0 ? (
        <div className="pointer-events-none absolute inset-x-0 top-24 z-20 flex justify-center">
          <Button type="button" size="sm" variant="secondary" className="pointer-events-auto shadow-md" onClick={jumpToTop}>
            {newCount} new {newCount === 1 ? "entry" : "entries"}
          </Button>
        </div>
      ) : null}
      <Table density="compact" stickyHeader containerClassName="min-h-0 flex-1 border-t [--pinned-ground:hsl(var(--background))]" scrollLabel="log entries">
        <TableHeader>
          <TableRow>
            <SortableTableHead label="Time" field="time" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Severity" field="severity" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Kind" field="kind" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Cluster" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Actor" field="actor" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Message" field="message" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <TableHead pinned="end" className="w-12 text-right">
              <span className="sr-only">Actions</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {query.isError && !query.data ? (
            <TableRow className="hover:bg-transparent">
              <TableCell colSpan={COLUMN_COUNT} className="p-0">
                <ReadErrorState noun="log entries" error={query.error} onRetry={() => void query.refetch()} />
              </TableCell>
            </TableRow>
          ) : sorted.length === 0 ? (
            <TableEmptyRow
              colSpan={COLUMN_COUNT}
              icon={ScrollText}
              title={filters.activeFilterCount > 0 ? "No log entry matches the current filters." : "No log entries yet"}
              description={filters.activeFilterCount > 0 ? undefined : collectionIncomplete
                ? "Unavailable hypervisor sources may have activity that is not recorded here yet."
                : "Recorded activity on clusters in your scope appears here."}
            />
          ) : (
            sorted.map((entry) => {
              const day = dayOf(entry.ts);
              const showDate = day !== previousDay;
              previousDay = day;
              return <LogRow key={entry.id} entry={entry} showDate={showDate} onOpenTask={onOpenTask} />;
            })
          )}
        </TableBody>
      </Table>
      {canShowMore ? (
        <div className="flex shrink-0 justify-center border-t py-1">
          <Button type="button" variant="ghost" size="sm" onClick={() => setVisibleLimit(MAX_ROWS)}>
            Show More
          </Button>
        </div>
      ) : null}
      {atCap ? (
        <p className="shrink-0 border-t px-3 py-1.5 text-xs text-muted-foreground">
          Showing the newest {MAX_ROWS} entries in the last 24 hours. Older entries stay in the journal and are not listed here.
        </p>
      ) : null}
    </div>
  );
}

// useTableFilters reads useSearchParams even with syncUrl off, so the boundary lives here,
// in the file that reads it, where check-searchparams-suspense.mjs looks for it.
export function LogTab(props: LogTabProps) {
  return (
    <React.Suspense fallback={<PanelSkeleton rows={skeletonRowsFor(props.height)} />}>
      <LogTabInner {...props} />
    </React.Suspense>
  );
}
