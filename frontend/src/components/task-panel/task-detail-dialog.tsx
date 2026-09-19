"use client";

import * as React from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { EmptyState } from "@/components/ui/empty-state";
import { NodeLink } from "@/components/ui/node-link";
import { Progress } from "@/components/ui/progress";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { TableLink } from "@/components/ui/table-link";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { KeyValueList } from "@/components/used-by/panels/config-panel";
import { usePermissions } from "@/hooks/use-permissions";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { formatDateTime, formatLabel } from "@/lib/utils";
import { Role } from "@/types/rbac";
import type { ClusterLogEntry, TaskRecord } from "@/types/tasks";
import { useKillConsoleSession, useTaskDetail } from "./hooks/use-tasks";
import {
  actorLabel,
  durationMs,
  formatDuration,
  isTerminal,
  isWebsocketClass,
  progressPercent,
  resourceHref,
  taskStatusTone,
  taskSubtitle,
} from "./task-format";

export interface TaskDetailDialogProps {
  taskId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Opens the cancel confirmation for this task. */
  onCancel: (task: TaskRecord) => void;
}

function logLines(entries: ClusterLogEntry[]): string {
  return entries.map((entry) => `${formatDateTime(entry.ts)} [${entry.severity}] ${entry.message}`).join("\n");
}

function metadataEntries(metadata: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(metadata)) {
    out[key] = typeof value === "string" ? value : JSON.stringify(value);
  }
  return out;
}

function OverviewTab({ task }: { task: TaskRecord }) {
  const href = resourceHref(task);
  const percent = progressPercent(task.progress);
  const duration = durationMs(task);
  return (
    <div className="space-y-4">
      <DetailSection title="What and Where">
        <DetailGrid cols={3}>
          <DetailField label="Action" value={task.action} mono />
          <DetailField label="Kind" value={formatLabel(task.kind)} />
          <DetailField
            label="Resource"
            value={
              task.resource_name ? (
                <span className="flex min-w-0 flex-col">
                  {href ? (
                    <TableLink href={href} wrap>
                      {task.resource_name}
                    </TableLink>
                  ) : (
                    <span className="break-all">{task.resource_name}</span>
                  )}
                  <span className="text-xs text-muted-foreground">{formatLabel(task.resource_type)}</span>
                </span>
              ) : undefined
            }
          />
          <DetailField label="Node" value={<NodeLink clusterId={task.cluster_id} wrap />} />
          <DetailField label="Project" value={task.project} />
          <DetailField label="Source" value={task.source} />
          <DetailField
            label="Progress"
            span={3}
            value={
              task.progress ? (
                <span className="flex flex-col gap-1">
                  {percent !== null && task.status === "running" ? (
                    <Progress value={percent} className="h-2" aria-label={`${percent}% complete`} />
                  ) : null}
                  <span className="break-words text-xs text-muted-foreground">{task.progress}</span>
                </span>
              ) : undefined
            }
          />
        </DetailGrid>
      </DetailSection>

      <DetailSection title="Who">
        <DetailGrid cols={2}>
          <DetailField label="Actor" value={actorLabel(task)} />
          <DetailField label="Role" value={task.actor_role ? formatLabel(task.actor_role) : undefined} />
          <DetailField label="API Key ID" value={task.api_key_id ?? undefined} copyable />
          <DetailField label="Request ID" value={task.request_id ?? undefined} copyable />
        </DetailGrid>
      </DetailSection>

      <DetailSection title="When">
        <DetailGrid cols={3}>
          <DetailField label="Started" value={task.started_at ? formatDateTime(task.started_at) : undefined} />
          <DetailField label="Finished" value={task.finished_at ? formatDateTime(task.finished_at) : undefined} />
          <DetailField label="Duration" value={duration === null ? undefined : formatDuration(duration)} />
          <DetailField label="Updated" value={formatDateTime(task.updated_at)} />
          <DetailField label="Cancellable" value={task.may_cancel ? "Yes" : "No"} />
        </DetailGrid>
      </DetailSection>
    </div>
  );
}

function LogTab({ entries }: { entries: ClusterLogEntry[] }) {
  if (entries.length === 0) {
    return <EmptyState title="No log lines for this task" description="Nothing has been recorded against it yet." />;
  }
  return <CodeBlock value={logLines(entries)} copyLabel="Task log" />;
}

function MetadataTab({ task }: { task: TaskRecord }) {
  const entries = metadataEntries(task.metadata ?? {});
  const hasEntries = Object.keys(entries).length > 0;
  return (
    <div className="space-y-4">
      {task.operation_id ? (
        <DetailGrid cols={2}>
          <DetailField label="Operation ID" value={task.operation_id} copyable />
          <DetailField label="Operation Class" value={task.operation_class ?? undefined} />
        </DetailGrid>
      ) : null}
      {hasEntries ? (
        <KeyValueList entries={entries} />
      ) : (
        <EmptyState title="No metadata recorded" description="The cluster reported nothing beyond the fields above." />
      )}
    </div>
  );
}

/**
 * Everything about one task. The detail query is patched by feed frames, so the status here
 * tracks the row without a second poll, and the dialog never snaps shut when the task ends.
 */
export function TaskDetailDialog({ taskId, open, onOpenChange, onCancel }: TaskDetailDialogProps) {
  const { data, isError, error, refetch } = useTaskDetail(open ? taskId : null);
  const { hasRole } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  const killSession = useKillConsoleSession();
  const [tab, setTab] = React.useState("overview");

  const task = data?.task ?? null;
  const isPartner = hasRole(Role.PARTNER);
  const finished = task ? isTerminal(task.status) : true;
  const websocket = task ? isWebsocketClass(task) : false;

  // The mutation toasts both outcomes itself; the dialog only tracks status through the feed.
  const killConsoleSession = () => {
    if (!task || !task.operation_id || !task.resource_name) return;
    killSession.mutate({
      instanceName: task.resource_name,
      operationId: task.operation_id,
      host: task.cluster_id,
    });
  };

  let footer: React.ReactNode = null;
  if (task && isPartner && !finished) {
    if (websocket && task.operation_id && task.resource_name) {
      footer = (
        <>
          {isReadOnly ? <p className="self-center text-xs text-muted-foreground">Read-only mode is on.</p> : null}
          <Button
            type="button"
            variant="destructive"
            disabled={isReadOnly || killSession.isPending}
            onClick={killConsoleSession}
          >
            {killSession.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" /> : null}
            Kill Console Session
          </Button>
        </>
      );
    } else if (task.may_cancel) {
      footer = (
        <>
          {isReadOnly ? <p className="self-center text-xs text-muted-foreground">Read-only mode is on.</p> : null}
          <Button type="button" variant="destructive" disabled={isReadOnly} onClick={() => onCancel(task)}>
            Cancel Task
          </Button>
        </>
      );
    }
  }

  return (
    <RecordDetailDialog
      open={open}
      onOpenChange={onOpenChange}
      title={task?.description ?? "Task"}
      recordId={task?.id ?? taskId ?? ""}
      recordIdLabel="Task"
      subtitle={task ? taskSubtitle(task) : undefined}
      status={task ? <Badge variant={taskStatusTone(task.status)}>{formatLabel(task.status)}</Badge> : null}
      error={task?.status === "failure" ? task.error : null}
      isLoading={!task && !isError}
      size="lg"
      tab={tab}
      onTabChange={setTab}
      defaultTab="overview"
      footer={footer}
      tabs={
        task
          ? [
              { value: "overview", label: "Overview", content: <OverviewTab task={task} /> },
              { value: "log", label: "Log", content: <LogTab entries={data?.log ?? []} /> },
              { value: "metadata", label: "Metadata", content: <MetadataTab task={task} /> },
            ]
          : []
      }
    >
      {/* Reached only when the read failed: RecordDetailDialog renders children when tabs is empty. */}
      {!task && isError ? (
        <ReadErrorState noun="task" error={error} onRetry={() => void refetch()} />
      ) : null}
    </RecordDetailDialog>
  );
}
