"use client";

import { Suspense, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { ListChecks, Loader2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { RefreshButton } from "@/components/ui/refresh-button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { NodeLink } from "@/components/ui/node-link";
import { PageReadError } from "@/components/ui/read-error-state";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { LoadingLottiePage } from "@/components/loading-lottie";
import {
  ActorCell,
  DescriptionCell,
  DurationCell,
  KindCell,
  ProjectCell,
  ResourceCell,
  StartedCell,
  StatusCell,
  TaskCancelDialog,
  TaskDetailDialog,
  TaskIdCell,
  TaskRowActions,
  STATUS_ORDER,
  actorLabel,
  durationMs,
  startedMs,
  kindLabel,
  taskStatusLabel,
} from "@/components/task-panel";
import { useTaskWindow } from "@/components/task-panel/hooks/use-tasks";
import { useTaskFeedState } from "@/components/task-panel/use-task-feed";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useRequireRole } from "@/hooks/use-require-role";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { defineTable, fromSort } from "@/lib/table-filter";
import { currentQuery, replaceQuery } from "@/lib/url-state";
import { formatDateTimeSeconds, formatRelativeTime, toMillis } from "@/lib/utils";
import { Role } from "@/types/rbac";
import type { TaskRecord } from "@/types/tasks";

// The full-page view of the task journal. The dock shows the newest 50 to 200 tasks; this page
// reads a time window of up to 500 and filters, sorts and pages them client side like every
// other list page. It never opens a socket of its own: the feed state is read from the dock's
// store, and the window cache is patched by the same frames the dock applies.

type TimeRange = "1h" | "24h" | "7d" | "30d";

const DEFAULT_RANGE: TimeRange = "24h";
const HOUR_MS = 60 * 60 * 1000;
const TIME_RANGES: readonly { value: TimeRange; label: string; ms: number }[] = [
  { value: "1h", label: "Last Hour", ms: HOUR_MS },
  { value: "24h", label: "Last 24 Hours", ms: 24 * HOUR_MS },
  { value: "7d", label: "Last 7 Days", ms: 7 * 24 * HOUR_MS },
  { value: "30d", label: "Last 30 Days", ms: 30 * 24 * HOUR_MS },
];
const WINDOW_LIMIT = 500;
const LIVE_TICK_MS = 1_000;
const IDLE_TICK_MS = 30_000;

function parseRange(raw: string | null): TimeRange {
  return TIME_RANGES.some((option) => option.value === raw) ? (raw as TimeRange) : DEFAULT_RANGE;
}

/**
 * The window's lower bound as an ISO timestamp, floored to the minute.
 *
 * note: the server takes a datetime, so the URL's `1h` has to become one somewhere. Flooring
 * keeps the React Query key stable across re-renders and a re-mount within the same minute;
 * the window then only ever grows, which is what an operator reading a live list expects.
 */
function sinceIso(range: TimeRange): string {
  const ms = TIME_RANGES.find((option) => option.value === range)?.ms ?? 24 * HOUR_MS;
  const floored = Math.floor((Date.now() - ms) / 60_000) * 60_000;
  return new Date(floored).toISOString();
}

// Same columns as the dock's Tasks tab plus Finished, and no `id`: this is the only table on the
// page, so bare `node`, `project` and `q` keys stay the shared-link contract lib/inventory-links.ts
// declares. `since` and `task` are read raw by the page itself, so a column may not claim them.
const OPERATIONS = defineTable<TaskRecord>()({
  searchPlaceholder: "Search tasks",
  defaultSort: { field: "started", direction: "desc" },
  reservedParams: ["since", "task"],
  columns: [
    { field: "id", label: "Task", sort: (t) => t.id, search: (t) => t.id, filter: fromSort("text") },
    { field: "description", label: "Description", sort: (t) => t.description, search: (t) => t.description, filter: fromSort("text") },
    { field: "resource", label: "Resource", sort: (t) => t.resource_name, search: (t) => t.resource_name, filter: fromSort("text") },
    { field: "node", label: "Cluster", sort: (t) => t.cluster_id, facet: (t) => t.cluster_id, facetSpec: { role: "node", primary: true } },
    { field: "project", label: "Project", sort: (t) => t.project, facet: (t) => t.project, facetSpec: { role: "project" } },
    {
      field: "status",
      label: "Status",
      sort: (t) => t.status,
      facet: (t) => t.status,
      facetSpec: { primary: true, format: taskStatusLabel, order: STATUS_ORDER },
    },
    { field: "actor", label: "Actor", sort: (t) => actorLabel(t), search: (t) => actorLabel(t), filter: fromSort("text") },
    { field: "kind", label: "Kind", sort: (t) => t.kind, facet: (t) => t.kind, facetSpec: { format: kindLabel } },
    { field: "started", label: "Started", sort: (t) => startedMs(t), filter: fromSort("date", { horizon: "past" }) },
    { field: "finished", label: "Finished", sort: (t) => toMillis(t.finished_at), filter: fromSort("date", { horizon: "past" }) },
    {
      field: "duration",
      label: "Duration",
      sort: (t) => durationMs(t),
      // Seconds for the range control, so its bounds read as "30s to 2m" rather than raw ms.
      filter: { kind: "number", value: (t) => { const ms = durationMs(t); return ms === null ? null : ms / 1000; }, unit: "seconds", integer: true },
    },
  ],
});

const COLUMN_COUNT = OPERATIONS.columns.length + 1;

/** One second while a running row is on the page and the tab is visible, else a slow tick. */
function useClock(live: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    let id: number | undefined;
    const stop = () => {
      if (id !== undefined) window.clearInterval(id);
      id = undefined;
    };
    const start = () => {
      stop();
      id = window.setInterval(() => setNow(Date.now()), live ? LIVE_TICK_MS : IDLE_TICK_MS);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        setNow(Date.now());
        start();
      } else {
        stop();
      }
    };
    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [live]);
  return now;
}

/** When the task finished, relative with the absolute time beneath; a placeholder while open. */
function FinishedCell({ task }: { task: TaskRecord }) {
  const finished = toMillis(task.finished_at);
  if (finished === null) {
    return (
      <TableCell className="whitespace-nowrap">
        <TableEmptyValue label="Not finished" />
      </TableCell>
    );
  }
  return (
    <TableCell className="whitespace-nowrap">
      {formatRelativeTime(finished)}
      <TableSubText>{formatDateTimeSeconds(task.finished_at)}</TableSubText>
    </TableCell>
  );
}

function TimeRangeSelect({ value, onChange }: { value: TimeRange; onChange: (next: TimeRange) => void }) {
  const labelId = useId();
  return (
    <div className="flex items-center gap-2">
      {/* note: a Radix trigger is a button, so htmlFor would associate with nothing;
          aria-labelledby is what names it, the shape sort-select.tsx uses. */}
      <span id={labelId} className="whitespace-nowrap text-sm font-medium">
        Time Range
      </span>
      <Select value={value} onValueChange={(next) => onChange(parseRange(next))}>
        <SelectTrigger aria-labelledby={labelId} className="h-9 w-[160px]">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {TIME_RANGES.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function OperationsContent() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const feed = useTaskFeedState();
  const { isReadOnly } = useReadOnlyMode();
  const { scopeKey } = useActorQueryScope();

  // important: both keys are URL state, read on every render and written on change through
  // lib/url-state, never a navigation. The default range is left out of the URL so a plain
  // /inventory/operations link and the default view are the same address.
  const range = parseRange(searchParams.get("since"));
  const setRange = useCallback(
    (next: TimeRange) => {
      const params = currentQuery();
      if (next === DEFAULT_RANGE) params.delete("since");
      else params.set("since", next);
      replaceQuery(pathname, params);
    },
    [pathname],
  );
  const since = useMemo(() => sinceIso(range), [range]);

  // note: the id, not the row. The window is patched by feed frames and re-read on a poll, so an
  // open dialog tracks status through its own detail query and never snaps shut when the row
  // moves. Seeded from ?task= so a shared link opens straight onto the task, and kept in the
  // URL while open so a refresh mid-read lands back on it.
  const [detailTaskId, setDetailTaskId] = useState<string | null>(() => searchParams.get("task"));
  const openTask = useCallback(
    (id: string) => {
      setDetailTaskId(id);
      const params = currentQuery();
      params.set("task", id);
      replaceQuery(pathname, params);
    },
    [pathname],
  );
  const closeTask = useCallback(() => {
    setDetailTaskId(null);
    const params = currentQuery();
    if (params.has("task")) {
      params.delete("task");
      replaceQuery(pathname, params);
    }
  }, [pathname]);
  const [cancelTask, setCancelTask] = useState<TaskRecord | null>(null);
  const [taskJournalUiActorScope, setTaskJournalUiActorScope] = useState(scopeKey);
  const previousTaskJournalActorScopeRef = useRef(scopeKey);
  const taskJournalUiIsCurrent = taskJournalUiActorScope === scopeKey;
  const currentDetailTaskId = taskJournalUiIsCurrent ? detailTaskId : null;
  const currentCancelTask = taskJournalUiIsCurrent ? cancelTask : null;

  // This route can remain mounted through sign-out/sign-in. Hide actor-owned task ids during
  // the render before effects run, then discard both detail and destructive confirmation state.
  useEffect(() => {
    if (previousTaskJournalActorScopeRef.current === scopeKey) return;
    previousTaskJournalActorScopeRef.current = scopeKey;
    setTaskJournalUiActorScope(scopeKey);
    setDetailTaskId(null);
    setCancelTask(null);
  }, [scopeKey]);

  const query = useTaskWindow(since, feed.state === "connected");
  const rows = useMemo(() => query.data?.tasks ?? [], [query.data]);
  // Clusters the journal follower is not hearing from. What the journal already holds for them
  // is listed; what happened since the stream dropped is not, which is what the noun says.
  const disconnectedClusters = query.data?.disconnected_clusters ?? [];

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  const filters = useTableFilters(OPERATIONS, rows, { onFilterChange: resetPage });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    OPERATIONS.sortAccessors,
    OPERATIONS.defaultSort.field,
    OPERATIONS.defaultSort.direction,
    resetPage,
  );
  const sorted = useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);

  // Clamped on read rather than corrected in an effect: rows arrive and leave live, so the page
  // under the operator can shrink on its own, and an effect would fire after a wrong frame.
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const paged = useMemo(
    () => sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize),
    [sorted, currentPage, pageSize],
  );

  const hasRunning = paged.some((task) => task.status === "running");
  const now = useClock(hasRunning);
  const serverNow = now + feed.serverOffsetMs;

  // The house loader only before the first window lands. A range change swaps the query key
  // and empties the data for a moment, and the Time Range control must stay mounted through it.
  const seeded = useRef(false);
  if (query.data) seeded.current = true;
  const switching = !query.data && query.isLoading;

  if (!seeded.current && query.isLoading) {
    return <LoadingLottiePage />;
  }

  // A failed read is not an empty list. Rendered before the table so the filtered-empty row
  // cannot blame a filter for a request that did not return.
  if (query.isError && !query.data) {
    return (
      <PageReadError
        title="Operations"
        description="Every task on your clusters, live and historical"
        noun="tasks"
        error={query.error}
        onRetry={() => void query.refetch()}
      />
    );
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-3xl font-bold">Operations</h1>
          <p className="text-muted-foreground mt-2">Every task on your clusters, live and historical</p>
        </div>
        <RefreshButton label="Refresh tasks" lastUpdatedAt={query.dataUpdatedAt} onRefresh={query.refreshNow} />
      </div>

      <UnreachableNodesBanner
        noun="newest tasks"
        nodes={disconnectedClusters}
        reasons={Object.fromEntries(
          disconnectedClusters.map((cluster) => [cluster, "live task collection is interrupted"]),
        )}
      />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>Tasks</CardTitle>
          <TableToolbar
            filters={filters}
            noun="tasks"
            actions={<TimeRangeSelect value={range} onChange={setRange} />}
          />
        </CardHeader>
        <CardContent className="space-y-4">
          {query.data?.has_more ? (
            <PartialReadNotice data-testid="operations-window-cap">
              Showing the newest {WINDOW_LIMIT} tasks in this range. Older tasks in the range are not listed.
            </PartialReadNotice>
          ) : null}

          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Task" field="id" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Description" field="description" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Resource" field="resource" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Cluster" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Actor" field="actor" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Kind" field="kind" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Started" field="started" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Finished" field="finished" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Duration" field="duration" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <TableHead pinned="end" className="w-12 text-right">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {switching ? (
                <TableEmptyRow colSpan={COLUMN_COUNT} icon={Loader2} iconClassName="animate-spin" title="Loading tasks" />
              ) : sorted.length === 0 ? (
                <TableEmptyRow
                  colSpan={COLUMN_COUNT}
                  icon={ListChecks}
                  title={filters.activeFilterCount > 0 ? "No task matches the current filters." : "No tasks in this range"}
                  description={
                    filters.activeFilterCount > 0
                      ? undefined
                      : "Tasks appear here as soon as something starts on a cluster in your scope, including work started from the lxc command line. Widen the time range to see older ones."
                  }
                />
              ) : (
                paged.map((task) => (
                  <TableRow key={task.id} data-task-id={task.id} data-task-status={task.status}>
                    <TaskIdCell task={task} onShow={openTask} />
                    <DescriptionCell task={task} />
                    <ResourceCell task={task} />
                    {/* Inline rather than the shared NodeCell: check-node-link.mjs counts the
                        literal tag per feature directory against the Node column above. */}
                    <TableCell className="whitespace-nowrap">
                      <NodeLink clusterId={task.cluster_id} />
                    </TableCell>
                    <ProjectCell task={task} />
                    <StatusCell task={task} />
                    <ActorCell task={task} />
                    <KindCell task={task} />
                    <StartedCell task={task} />
                    <FinishedCell task={task} />
                    <DurationCell task={task} serverNow={serverNow} />
                    <TableCell pinned="end" className="text-right">
                      {/* canCancel is unconditional: the page is gated to Partner and above. */}
                      <TaskRowActions
                        task={task}
                        onShow={openTask}
                        onCancel={setCancelTask}
                        canCancel
                        readOnly={isReadOnly}
                      />
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>

          {/* Absent rather than disabled when everything fits on one page. */}
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
        </CardContent>
      </Card>

      <TaskDetailDialog
        taskId={currentDetailTaskId}
        open={taskJournalUiIsCurrent && currentDetailTaskId !== null}
        onOpenChange={(next) => {
          if (!next) closeTask();
        }}
        onCancel={setCancelTask}
      />
      <TaskCancelDialog
        task={currentCancelTask}
        open={taskJournalUiIsCurrent && currentCancelTask !== null}
        onOpenChange={(next) => {
          if (!next) setCancelTask(null);
        }}
      />
    </div>
  );
}

// The role gate stays in this file: check-role-gates.mjs reads page text and does not follow
// imports. Suspense is required because useTableFilters and the page read useSearchParams.
export default function OperationsPage() {
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <OperationsContent />
    </Suspense>
  );
}
