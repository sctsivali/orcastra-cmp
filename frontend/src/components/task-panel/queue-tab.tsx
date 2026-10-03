"use client";

import * as React from "react";
import { CalendarClock, Inbox } from "lucide-react";
import { IntentLink } from "@/components/navigation/route-intent";
import { Badge } from "@/components/ui/badge";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { NodeLink } from "@/components/ui/node-link";
import { OverflowText } from "@/components/ui/overflow-text";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { ScrollArea } from "@/components/ui/scroll-area";
import { TableLink } from "@/components/ui/table-link";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { usePermissions } from "@/hooks/use-permissions";
import { toMillis } from "@/lib/utils";
import { Role } from "@/types/rbac";
import type { DueSchedule, QueueCluster, TaskRecord } from "@/types/tasks";
import { CollectionNotice } from "./collection-notice";
import { useTaskQueue } from "./hooks/use-tasks";
import { PanelSkeleton, skeletonRowsFor } from "./panel-skeleton";
import { durationMs, formatDuration, startedMs, taskStatusDisplay } from "./task-format";
import type { TaskFeedSnapshot } from "./use-task-feed";

const TICK_MS = 1_000;
const QUEUE_PAGE_SIZE = 100;

export interface QueueTabProps {
  feed: TaskFeedSnapshot;
  height: number;
  onOpenTask: (id: string) => void;
  registerRefresh: (refresh: (() => Promise<unknown>) | null) => void;
  collectionIncomplete: boolean;
}

// A grid row, not a table row. The queue is a grouped list ordered by acceptance, and a
// TableBody here would be a table with no defineTable behind it.
// The first column holds a queue position or the word Running; 2rem clipped the word.
const ROW_CLASS = "grid h-8 grid-cols-[3.5rem_minmax(0,1fr)_auto_auto] items-center gap-2 border-b px-3 text-xs";

function QueueRow({
  task,
  position,
  serverNow,
  onOpenTask,
}: {
  task: TaskRecord;
  position: number | null;
  serverNow: number;
  onOpenTask: (id: string) => void;
}) {
  const status = taskStatusDisplay(task.status);
  // Running rows show how long they have run; waiting rows how long they have waited.
  const elapsed = task.status === "running" ? durationMs(task, serverNow) : null;
  const accepted = startedMs(task);
  const waited = accepted === null ? null : Math.max(0, serverNow - accepted);
  const timing =
    task.status === "running"
      ? elapsed === null
        ? ""
        : formatDuration(elapsed)
      : waited === null
        ? ""
        : `waiting ${formatDuration(waited)}`;
  return (
    <li className={ROW_CLASS} data-task-id={task.id}>
      <span className="tabular-nums text-muted-foreground">
        {task.status === "running" ? "Running" : position ?? "Pending"}
      </span>
      <OverflowText value={task.description} label={`description of task ${task.id}`} lines={1}>
        {(expanded) => (
          <TableLink wrap={expanded} onClick={() => onOpenTask(task.id)} aria-label={`Show details for task ${task.id}`}>
            {task.description}
          </TableLink>
        )}
      </OverflowText>
      <Badge variant={status.tone}>{status.label}</Badge>
      <span className="min-w-[4rem] text-right tabular-nums text-muted-foreground">{timing}</span>
    </li>
  );
}

// A schedule is due in the future; formatRelativeTime reads the past and would say "just now".
function dueLabel(due: number | null, serverNow: number): string {
  if (due === null) return "";
  const ahead = due - serverNow;
  if (ahead <= 0) return "Due now";
  return `Due in ${formatDuration(ahead)}`;
}

function DueRow({ schedule, serverNow }: { schedule: DueSchedule; serverNow: number }) {
  const due = toMillis(schedule.next_run_at);
  return (
    <li className={ROW_CLASS}>
      <CalendarClock className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
      <OverflowText value={`${schedule.label} on ${schedule.instance_name}`} label={`schedule ${schedule.label}`} lines={1} />
      <Badge variant="neutral">Due</Badge>
      <span className="min-w-[4rem] text-right tabular-nums text-muted-foreground">{dueLabel(due, serverNow)}</span>
    </li>
  );
}

function ClusterGroup({
  cluster,
  stale,
  serverNow,
  onOpenTask,
}: {
  cluster: QueueCluster;
  stale: boolean;
  serverNow: number;
  onOpenTask: (id: string) => void;
}) {
  const running = cluster.running.length;
  const pending = cluster.pending.length;
  return (
    <section aria-label={`Queue on ${cluster.cluster_id}`} className="border-b last:border-b-0">
      <header className="flex h-8 items-center gap-2 bg-muted/40 px-3 text-xs">
        <NodeLink clusterId={cluster.cluster_id} />
        <span className="text-muted-foreground">
          On this page: {running} running, {pending} waiting
        </span>
        {stale ? (
          <Badge variant="caution" asChild>
            <span aria-label="Live updates for this cluster are interrupted">Stale</span>
          </Badge>
        ) : null}
      </header>
      <ol>
        {cluster.running.map((task) => (
          <QueueRow key={task.id} task={task} position={null} serverNow={serverNow} onOpenTask={onOpenTask} />
        ))}
        {cluster.pending.map((task) => (
          <QueueRow key={task.id} task={task} position={task.queue_position} serverNow={serverNow} onOpenTask={onOpenTask} />
        ))}
        {cluster.due_schedules.map((schedule) => (
          <DueRow key={schedule.schedule_id} schedule={schedule} serverNow={serverNow} />
        ))}
      </ol>
    </section>
  );
}

export function QueueTab({ feed, height, onOpenTask, registerRefresh, collectionIncomplete }: QueueTabProps) {
  const { scopeKey } = useActorQueryScope();
  const { hasRole } = usePermissions();
  const [pagination, setPagination] = React.useState({ scopeKey, page: 1 });
  const page = pagination.scopeKey === scopeKey ? pagination.page : 1;
  const query = useTaskQueue(true, QUEUE_PAGE_SIZE, (page - 1) * QUEUE_PAGE_SIZE);
  const totalOpen = query.data?.total_open ?? 0;
  const totalPages = Math.max(1, Math.ceil(totalOpen / QUEUE_PAGE_SIZE));
  React.useEffect(() => {
    if (!query.data) return;
    setPagination((previous) => {
      const nextPage = previous.scopeKey === scopeKey ? Math.min(previous.page, totalPages) : 1;
      return previous.scopeKey === scopeKey && previous.page === nextPage
        ? previous
        : { scopeKey, page: nextPage };
    });
  }, [query.data, scopeKey, totalPages]);
  const { refreshNow } = query;
  React.useEffect(() => {
    registerRefresh(refreshNow);
    return () => registerRefresh(null);
  }, [registerRefresh, refreshNow]);

  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") setNow(Date.now());
    }, TICK_MS);
    return () => window.clearInterval(id);
  }, []);
  const serverNow = now + feed.serverOffsetMs;

  if (query.isLoading && !query.data) return <PanelSkeleton rows={skeletonRowsFor(height)} />;
  if (query.isError && !query.data) {
    return <ReadErrorState noun="queue" error={query.error} onRetry={() => void query.refetch()} />;
  }

  const data = query.data;
  const clusters = [...(data?.clusters ?? [])]
    .filter((c) => c.running.length + c.pending.length + c.due_schedules.length > 0)
    .sort((a, b) => a.cluster_id.localeCompare(b.cluster_id));
  const disconnected = new Set(data?.disconnected_clusters ?? []);
  const incomplete = collectionIncomplete || disconnected.size > 0 || data?.follower_active === false;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <CollectionNotice incomplete={incomplete} />
      {query.isError && data && (
        <p role="status" className="shrink-0 border-b px-3 py-1.5 text-xs text-muted-foreground">
          Queue refresh failed. Showing the last successful read.
        </p>
      )}
      <ScrollArea className="min-h-0 flex-1">
        {clusters.length === 0 ? (
          <EmptyState
            icon={Inbox}
            title={incomplete ? "No open tasks recorded" : "Nothing is waiting"}
            description={incomplete
              ? "Unavailable hypervisor sources may have tasks that are not recorded here yet."
              : "No task is running or waiting on any cluster in your scope."}
          />
        ) : (
          clusters.map((cluster) => (
            <ClusterGroup
              key={cluster.cluster_id}
              cluster={cluster}
              stale={disconnected.has(cluster.cluster_id)}
              serverNow={serverNow}
              onOpenTask={onOpenTask}
            />
          ))
        )}
      </ScrollArea>
      {data && totalOpen > QUEUE_PAGE_SIZE && (
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t px-3 py-1">
          <span className="text-xs tabular-nums text-muted-foreground">
            {Math.min(data.offset + 1, totalOpen)} to {Math.min(data.offset + data.limit, totalOpen)} of {totalOpen} open tasks
          </span>
          <DataTablePagination
            variant="compact"
            label="task queue"
            currentPage={Math.min(page, totalPages)}
            totalPages={totalPages}
            onPageChange={(nextPage) => setPagination({ scopeKey, page: nextPage })}
          />
        </div>
      )}
      {data && data.due_schedules_total > 0 && (
        <p className="shrink-0 border-t px-3 py-1.5 text-xs text-muted-foreground">
          {page === 1
            ? data.due_schedules_truncated
              ? `Showing the first 100 of ${data.due_schedules_total} due schedules. `
              : `${data.due_schedules_total} schedules due within an hour. `
            : "Due schedules appear on the first queue page. "}
          {hasRole(Role.PARTNER) && (
            <IntentLink href="/inventory/snapshots" className="underline underline-offset-4">View schedules</IntentLink>
          )}
        </p>
      )}
      <p className="shrink-0 border-t px-3 py-1.5 text-xs text-muted-foreground">
        Positions show the journal acceptance order of visible pending tasks. The hypervisor schedules execution independently.
      </p>
    </div>
  );
}
