"use client";

import * as React from "react";
import { ListChecks } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyRow } from "@/components/ui/table-text";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { inventoryLinks } from "@/lib/inventory-links";
import { defineTable, fromSort } from "@/lib/table-filter";
import type { TaskRecord } from "@/types/tasks";
import { CollectionNotice } from "./collection-notice";
import { useTaskList } from "./hooks/use-tasks";
import { PanelSkeleton, skeletonRowsFor } from "./panel-skeleton";
import {
  ActorCell,
  DescriptionCell,
  DurationCell,
  KindCell,
  NodeCell,
  ProjectCell,
  ResourceCell,
  StartedCell,
  StatusCell,
  TaskIdCell,
} from "./task-cells";
import {
  STATUS_ORDER,
  actorLabel,
  durationMs,
  kindLabel,
  startedMs,
  taskStatusLabel,
} from "./task-format";
import { useClock } from "./hooks/use-clock";
import { TaskRowActions } from "./task-row-actions";
import type { TaskFeedSnapshot } from "./use-task-feed";

const FIRST_PAGE = 50;
const MAX_ROWS = 200;

// Start order, not update order: sorting on updated_at would reorder rows on every progress
// tick under an operator who is reading them.
const TASKS = defineTable<TaskRecord>()({
  id: "dock",
  searchPlaceholder: "Search tasks",
  defaultSort: { field: "started", direction: "desc" },
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
    {
      field: "duration",
      label: "Duration",
      sort: (t) => durationMs(t),
      // Seconds for the range control, so its bounds read as "30s to 2m" rather than raw ms.
      filter: { kind: "number", value: (t) => { const ms = durationMs(t); return ms === null ? null : ms / 1000; }, unit: "seconds", integer: true },
    },
  ],
});

const COLUMN_COUNT = TASKS.columns.length + 1;

export interface TasksTabProps {
  feed: TaskFeedSnapshot;
  /** The dock height, for the skeleton row count. */
  height: number;
  isPartner: boolean;
  readOnly: boolean;
  onOpenTask: (id: string) => void;
  onCancel: (task: TaskRecord) => void;
  /** Hands the strip's refresh control this tab's refetch while it is mounted. */
  registerRefresh: (refresh: (() => Promise<unknown>) | null) => void;
  collectionIncomplete: boolean;
}

function TasksTabInner({ feed, height, isPartner, readOnly, onOpenTask, onCancel, registerRefresh, collectionIncomplete }: TasksTabProps) {
  const [visibleLimit, setVisibleLimit] = React.useState(FIRST_PAGE);
  const query = useTaskList(feed.state === "connected", visibleLimit);
  const rows = React.useMemo(() => query.data?.tasks ?? [], [query.data]);

  const filters = useTableFilters(TASKS, rows, { syncUrl: false });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    TASKS.sortAccessors,
    TASKS.defaultSort.field,
    TASKS.defaultSort.direction,
  );
  const sorted = React.useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);

  const { refreshNow } = query;
  React.useEffect(() => {
    registerRefresh(refreshNow);
    return () => registerRefresh(null);
  }, [registerRefresh, refreshNow]);

  const hasRunning = sorted.some((task) => task.status === "running");
  const now = useClock(hasRunning);
  const serverNow = now + feed.serverOffsetMs;

  if (query.isLoading && !query.data) return <PanelSkeleton rows={skeletonRowsFor(height)} />;

  const hasMore = query.data?.has_more ?? false;
  const canShowMore = hasMore && visibleLimit < MAX_ROWS;
  const atCap = hasMore && visibleLimit >= MAX_ROWS;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <CollectionNotice incomplete={collectionIncomplete} />
      <TableToolbar filters={filters} noun="tasks" className="px-3 py-2" />
      <Table density="compact" stickyHeader containerClassName="min-h-0 flex-1 border-t" scrollLabel="tasks">
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
            <SortableTableHead label="Duration" field="duration" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <TableHead pinned="end" className="w-12 text-right">
              <span className="sr-only">Actions</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {query.isError && !query.data ? (
            <TableRow className="hover:bg-transparent">
              <TableCell colSpan={COLUMN_COUNT} className="p-0">
                <ReadErrorState noun="tasks" error={query.error} onRetry={() => void query.refetch()} />
              </TableCell>
            </TableRow>
          ) : sorted.length === 0 ? (
            <TableEmptyRow
              colSpan={COLUMN_COUNT}
              icon={ListChecks}
              title={filters.activeFilterCount > 0 ? "No task matches the current filters." : "No tasks yet"}
              description={
                filters.activeFilterCount > 0
                  ? undefined
                  : collectionIncomplete
                    ? "Unavailable hypervisor sources may have tasks that are not recorded here yet."
                  : isPartner
                    ? "Tasks on clusters in your scope appear here, including work started outside the dashboard."
                    : "Tasks for your instances appear here as soon as one starts."
              }
            />
          ) : (
            sorted.map((task) => (
              <TableRow key={task.id} data-task-id={task.id} data-task-status={task.status}>
                <TaskIdCell task={task} onShow={onOpenTask} />
                <DescriptionCell task={task} />
                <ResourceCell task={task} />
                <NodeCell task={task} />
                <ProjectCell task={task} />
                <StatusCell task={task} />
                <ActorCell task={task} />
                <KindCell task={task} />
                <StartedCell task={task} />
                <DurationCell task={task} serverNow={serverNow} />
                <TableCell pinned="end" className="text-right">
                  <TaskRowActions
                    task={task}
                    onShow={onOpenTask}
                    onCancel={onCancel}
                    canCancel={isPartner}
                    readOnly={readOnly}
                  />
                </TableCell>
              </TableRow>
            ))
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
          Showing the newest {MAX_ROWS} tasks.{" "}
          {isPartner ? (
            <>
              Open the{" "}
              <TableLink href={inventoryLinks.operations({})}>Operations page</TableLink> for older ones.
            </>
          ) : (
            "Older tasks are not shown here."
          )}
        </p>
      ) : null}
    </div>
  );
}

// useTableFilters reads useSearchParams even with syncUrl off, so the boundary lives here,
// in the file that reads it, where check-searchparams-suspense.mjs looks for it.
export function TasksTab(props: TasksTabProps) {
  return (
    <React.Suspense fallback={<PanelSkeleton rows={skeletonRowsFor(props.height)} />}>
      <TasksTabInner {...props} />
    </React.Suspense>
  );
}
