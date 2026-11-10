"use client";

import type { SnapshotSchedule } from "@/types/api";

import { CalendarClock, Loader2 } from "lucide-react";

import { OverflowText } from "@/components/ui/overflow-text";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { NodeLink } from "@/components/ui/node-link";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import { defineTable, fromSort } from "@/lib/table-filter";

import { ScheduleRowActions } from "./snapshot-schedule-row-actions";
import { lastRunCell, nextRunCell } from "./snapshot-format";
import {
  STATE_LABEL,
  STATE_ORDER,
  STATE_RANK,
  frequencyLabel,
  scheduleState,
} from "./snapshot-schedule-vocabulary";
import { plural } from "@/lib/utils";

/**
 * The definition lives beside the rows it renders, for the reason snapshots-table.tsx records.
 *
 * important: keep `id: "schedules"`. useTableFilters namespaces every URL key by the table id,
 * and SNAPSHOTS has none, so it occupies the bare keys, which the page also reads (`host`,
 * `project`) to scope the fleet read. Without the id, filtering the schedules by cluster would
 * re-scope the snapshot list underneath it.
 */
export const SNAPSHOT_SCHEDULES = defineTable<SnapshotSchedule>()({
  id: "schedules",
  searchPlaceholder: "Search schedules",
  defaultSort: { field: "next_run_at", direction: "asc" },
  columns: [
    {
      field: "label",
      label: "Schedule",
      sort: (s) => s.label,
      search: (s) => s.label,
      filter: fromSort("text"),
    },
    {
      field: "instance_name",
      label: "Instance",
      sort: (s) => s.instance_name,
      search: (s) => s.instance_name,
      filter: fromSort("text"),
    },
    {
      field: "host",
      label: "Cluster",
      sort: (s) => s.host,
      search: (s) => s.host,
      facet: (s) => s.host,
      facetSpec: { role: "node" },
    },
    {
      field: "project",
      label: "Project",
      sort: (s) => s.project || "default",
      facet: (s) => s.project || "default",
      facetSpec: { role: "project" },
    },
    {
      field: "state",
      label: "State",
      sort: (s) => STATE_RANK[scheduleState(s)],
      facet: (s) => scheduleState(s),
      facetSpec: {
        primary: true,
        format: (value) => STATE_LABEL[value as keyof typeof STATE_LABEL] ?? value,
        order: STATE_ORDER,
      },
    },
    {
      field: "frequency",
      label: "Repeats",
      sort: (s) => s.frequency,
      facet: (s) => s.frequency,
      facetSpec: { primary: true, format: frequencyLabel },
    },
    {
      field: "keep_last",
      label: "Keeps",
      sort: (s) => s.keep_last,
      filter: fromSort("number"),
    },
    {
      field: "next_run_at",
      label: "Next Run",
      sort: (s) => (s.next_run_at ? Date.parse(s.next_run_at) : null),
      filter: fromSort("date", { horizon: "future", emptyLabel: "Paused" }),
    },
    {
      field: "last_run_at",
      label: "Last Run",
      sort: (s) => (s.last_run_at ? Date.parse(s.last_run_at) : null),
      filter: fromSort("date", { horizon: "past", emptyLabel: "Never run" }),
    },
  ],
});

/**
 * The header row as data, so the column count cannot drift from what the empty row spans.
 * Same arrangement as the snapshots table beside it, and for the same reason.
 */
export const SCHEDULE_HEADERS = [
  { field: "label", label: "Schedule", sortable: true },
  { field: "instance_name", label: "Instance", sortable: true },
  { field: "host", label: "Cluster", sortable: true },
  { field: "project", label: "Project", sortable: true },
  { field: "state", label: "State", sortable: true },
  { field: "frequency", label: "Repeats", sortable: true },
  { field: "keep_last", label: "Keeps", sortable: true },
  { field: "next_run_at", label: "Next Run", sortable: true },
  { field: "last_run_at", label: "Last Run", sortable: true },
  { field: "actions", label: "Actions", sortable: false },
] as const;

export const SCHEDULE_COLUMN_COUNT = SCHEDULE_HEADERS.length;

type ScheduleField = (typeof SNAPSHOT_SCHEDULES.columns)[number]["field"];

interface SchedulesTableProps {
  rows: SnapshotSchedule[];
  sortField: ScheduleField;
  sortDirection: "asc" | "desc";
  onSort: (field: ScheduleField) => void;
  refreshing?: boolean;
  empty: { title: string; description?: string };
}

export function SnapshotSchedulesTable({
  rows,
  sortField,
  sortDirection,
  onSort,
  refreshing = false,
  empty,
}: SchedulesTableProps) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {SCHEDULE_HEADERS.map((header) =>
            header.sortable ? (
              <SortableTableHead
                key={header.field}
                label={header.label}
                field={header.field}
                currentField={sortField}
                direction={sortDirection}
                onSort={onSort}
              />
            ) : (
              <TableHead key="actions" pinned="end" className="w-12 text-right">
                <span className="sr-only">{header.label}</span>
              </TableHead>
            ),
          )}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.length === 0 ? (
          <TableEmptyRow
            colSpan={SCHEDULE_COLUMN_COUNT}
            icon={refreshing ? Loader2 : CalendarClock}
            iconClassName={refreshing ? "animate-spin" : undefined}
            title={refreshing ? "Loading schedules" : empty.title}
            description={refreshing ? undefined : empty.description}
          />
        ) : (
          rows.map((schedule) => {
            const state = scheduleState(schedule);
            return (
              <TableRow key={schedule.id}>
                <TableCell className="font-medium">
                  <span className="font-mono">{schedule.label}</span>
                </TableCell>
                <TableCell>{schedule.instance_name}</TableCell>
                <TableCell className="whitespace-nowrap">
                  <NodeLink clusterId={schedule.host} emptyLabel="No cluster" />
                </TableCell>
                <TableCell className="whitespace-nowrap">{schedule.project || "default"}</TableCell>
                <TableCell>
                  {/* Plain text rather than a Badge: a data cell carries the value and one
                      subordinate tier. The tone chip belongs in the instance banner. */}
                  {STATE_LABEL[state]}
                  {schedule.last_error ? (
                    <TableSubText>
                      <OverflowText
                        value={schedule.last_error}
                        label={`error on schedule ${schedule.label}`}
                        lines={1}
                      />
                    </TableSubText>
                  ) : schedule.missed_runs > 0 ? (
                    <TableSubText>{plural(schedule.missed_runs, "run")} missed</TableSubText>
                  ) : null}
                </TableCell>
                <TableCell className="whitespace-nowrap">
                  {frequencyLabel(schedule.frequency)}
                  <TableSubText>{schedule.summary}</TableSubText>
                </TableCell>
                <TableCell>{schedule.keep_last}</TableCell>
                <TableCell className="whitespace-nowrap">
                  {nextRunCell(schedule.next_run_at)}
                </TableCell>
                <TableCell className="whitespace-nowrap">
                  {lastRunCell(schedule.last_run_at)}
                </TableCell>
                <TableCell pinned="end" className="text-right">
                  <ScheduleRowActions schedule={schedule} />
                </TableCell>
              </TableRow>
            );
          })
        )}
      </TableBody>
    </Table>
  );
}
