"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { NodeLink } from "@/components/ui/node-link";
import { OverflowText } from "@/components/ui/overflow-text";
import { TableCell } from "@/components/ui/table";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { formatDateTime, formatLabel, formatRelativeTime } from "@/lib/utils";
import type { LogSeverity, TaskRecord } from "@/types/tasks";
import {
  actorLabel,
  actorSubText,
  durationMs,
  formatDuration,
  kindLabel,
  logSeverityDisplay,
  resourceHref,
  startedMs,
  taskStatusDisplay,
} from "./task-format";

// The cells of a task row. One implementation for the dock and the Operations page, so the
// two never render the same record two ways.
//
// note: every cell here is a whole TableCell, and the Badge lives here rather than in the
// TableBody that mounts these. Status is one of the two chips the table conventions allow in
// a body, and keeping it in a cell component is what the badge ratchet in
// check-table-typography.mjs is designed to leave alone.

interface TaskCellProps {
  task: TaskRecord;
}

/** The id, clipped by CSS with the whole value in the DOM, opening the detail dialog. */
export function TaskIdCell({ task, onShow }: TaskCellProps & { onShow: (id: string) => void }) {
  return (
    <TableCell className="max-w-[12rem] font-mono">
      <OverflowText value={task.id} label={`id for ${task.description}`} reveal="always" mono>
        {(expanded) => (
          <TableLink
            wrap={expanded}
            onClick={() => onShow(task.id)}
            aria-label={`Show details for task ${task.id}`}
          >
            {task.id}
          </TableLink>
        )}
      </OverflowText>
    </TableCell>
  );
}

export function DescriptionCell({ task }: TaskCellProps) {
  return (
    <TableCell className="max-w-[20rem]">
      <OverflowText value={task.description} label={`description of task ${task.id}`} lines={1} />
      {task.status === "failure" && task.error ? <TableSubText>{task.error}</TableSubText> : null}
    </TableCell>
  );
}

export function ResourceCell({ task }: TaskCellProps) {
  const name = task.resource_name;
  if (!name) {
    return (
      <TableCell>
        <TableEmptyValue label="No resource" />
      </TableCell>
    );
  }
  const href = resourceHref(task);
  return (
    <TableCell className="max-w-[16rem] font-medium">
      {href ? <TableLink href={href}>{name}</TableLink> : <span className="min-w-0">{name}</span>}
      <TableSubText>{formatLabel(task.resource_type)}</TableSubText>
    </TableCell>
  );
}

export function NodeCell({ task }: TaskCellProps) {
  return (
    <TableCell className="whitespace-nowrap">
      <NodeLink clusterId={task.cluster_id} />
    </TableCell>
  );
}

export function ProjectCell({ task }: TaskCellProps) {
  return <TableCell className="whitespace-nowrap">{task.project}</TableCell>;
}

export function StatusCell({ task }: TaskCellProps) {
  const status = taskStatusDisplay(task.status);
  return (
    <TableCell className="whitespace-nowrap">
      <Badge variant={status.tone}>{status.label}</Badge>
      {task.status === "running" && task.progress ? (
        <TableSubText className="max-w-[10rem]">{task.progress}</TableSubText>
      ) : null}
      {task.status === "lost" ? <TableSubText>Outcome not observed</TableSubText> : null}
    </TableCell>
  );
}

export function ActorCell({ task }: TaskCellProps) {
  return (
    <TableCell className="max-w-[14rem]">
      <OverflowText value={actorLabel(task)} label={`actor of task ${task.id}`} lines={1} />
      <TableSubText>{actorSubText(task)}</TableSubText>
    </TableCell>
  );
}

export function KindCell({ task }: TaskCellProps) {
  return <TableCell className="whitespace-nowrap">{kindLabel(task.kind)}</TableCell>;
}

export function StartedCell({ task }: TaskCellProps) {
  const started = startedMs(task);
  return (
    <TableCell className="whitespace-nowrap">
      {formatRelativeTime(started)}
      <TableSubText>{formatDateTime(task.started_at ?? task.created_at)}</TableSubText>
    </TableCell>
  );
}

/** Live while running, so the caller re-renders it on its own clock and passes the server time. */
export function DurationCell({ task, serverNow }: TaskCellProps & { serverNow: number }) {
  if (task.status === "pending") {
    return (
      <TableCell className="whitespace-nowrap">
        <TableEmptyValue label="Not started" />
      </TableCell>
    );
  }
  const ms = durationMs(task, serverNow);
  return (
    <TableCell className="whitespace-nowrap">
      {ms === null ? <TableEmptyValue label="No duration" /> : formatDuration(ms)}
    </TableCell>
  );
}

/** A chip for error and warning; plain text for the two that need no attention. */
export function SeverityCell({ severity }: { severity: LogSeverity }) {
  const display = logSeverityDisplay(severity);
  return (
    <TableCell className="whitespace-nowrap">
      {display.tone ? <Badge variant={display.tone}>{display.label}</Badge> : display.label}
    </TableCell>
  );
}
