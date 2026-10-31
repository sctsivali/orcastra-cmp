"use client";

import type { SnapshotInfo } from "@/types/api";

import { History, Loader2 } from "lucide-react";

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
import { TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { defineTable, fromSort } from "@/lib/table-filter";
import { formatBytes } from "@/lib/utils";

import { SnapshotRowActions } from "./snapshot-row-actions";
import { createdCell, expiryCell, snapshotScope } from "./snapshot-format";
import { ORIGIN_LABEL, ORIGIN_ORDER, ORIGIN_RANK, snapshotOrigin } from "./snapshot-origin";

/**
 * The table definition lives beside the rows it renders.
 *
 * important: not in page.tsx. check-table-filters.mjs counts any file rendering <TableBody>
 * without a defineTable< as an unmanaged table, against a baseline that may fall and never rise,
 * so splitting the two would fail the build. It is also simply where it belongs: the columns and
 * the cells that fill them are one thing.
 */
export const SNAPSHOTS = defineTable<SnapshotInfo>()({
  searchPlaceholder: "Search snapshots",
  defaultSort: { field: "created_at", direction: "desc" },
  columns: [
    {
      field: "name",
      label: "Snapshot",
      sort: (s) => s.name,
      search: (s) => s.name,
      filter: fromSort("text"),
    },
    {
      field: "source_name",
      label: "Instance",
      sort: (s) => s.source_name,
      search: (s) => s.source_name,
      filter: fromSort("text"),
    },
    {
      field: "host",
      // The field name matches SnapshotInfo and the ?host= deep-link key the Instances page
      // already reads, so the same URL means the same thing on both pages. The role is metadata
      // about which dimension this is; it is never a rename.
      label: "Cluster",
      sort: (s) => s.host ?? "",
      search: (s) => s.host,
      facet: (s) => s.host,
      // No `primary`: the page renders its own single-select for this dimension and excludes the
      // field from availableFields, so the flag would be inert.
      facetSpec: { role: "node" },
    },
    {
      field: "project",
      label: "Project",
      sort: (s) => s.project || "default",
      search: (s) => s.project,
      facet: (s) => s.project || "default",
      facetSpec: { role: "project" },
    },
    {
      field: "origin",
      label: "Origin",
      // Three classes, not two. The console used to say "Managed elsewhere" for anything without
      // the ops- prefix, which lumped a snapshot an operator took by hand in with the ones the
      // upstream product owns and bills for.
      //
      // note: the facet's value is the machine value and `format` supplies the label, the same
      // split the warnings page uses for severity. Sorting follows ORIGIN_RANK rather than the
      // alphabet, so the order is "how much of this do we own" rather than an accident of
      // spelling.
      sort: (s) => ORIGIN_RANK[snapshotOrigin(s)],
      facet: (s) => snapshotOrigin(s),
      facetSpec: {
        primary: true,
        format: (v: string) => ORIGIN_LABEL[v as keyof typeof ORIGIN_LABEL] ?? v,
        order: [...ORIGIN_ORDER],
      },
    },
    {
      field: "size",
      label: "Size",
      // note: null, not 0, exactly as created_at below does for a missing timestamp. Folding an
      // unknown size to 0 made a "size at least 1 GB" filter hide the very rows whose size is
      // unknown, presenting an absent measurement as a small one.
      sort: (s) => s.size ?? null,
      filter: fromSort("number", { unit: "bytes" }),
    },
    {
      field: "created_at",
      label: "Created",
      sort: (s) => (s.created_at ? Date.parse(s.created_at) : null),
      filter: fromSort("date", { horizon: "past" }),
    },
    {
      field: "expires_at",
      label: "Expires",
      // A snapshot with no expiry is one this console did not create, so the empty label names
      // the thing rather than leaving a blank an operator has to interpret.
      sort: (s) => (s.expires_at ? Date.parse(s.expires_at) : null),
      filter: fromSort("date", { horizon: "future", emptyLabel: "No expiry" }),
    },
  ],
});

/**
 * The header row as data, so the column count and the empty row's span cannot disagree.
 *
 * The previous version hardcoded colSpan={6} beside five <SortableTableHead> and one <TableCell>,
 * which is both a wrong span and a <td> inside <thead>. zz-colspan-parity.spec.ts counts
 * `thead th`, so the two defects hid each other.
 */
const SNAPSHOT_HEADERS = [
  { label: "Snapshot", field: "name" },
  { label: "Instance", field: "source_name" },
  { label: "Cluster", field: "host" },
  { label: "Project", field: "project" },
  { label: "Origin", field: "origin" },
  { label: "Size", field: "size" },
  { label: "Created", field: "created_at" },
  { label: "Expires", field: "expires_at" },
  { label: "Actions", field: null },
] as const;

export const SNAPSHOT_COLUMN_COUNT = SNAPSHOT_HEADERS.length;

type SnapshotField = keyof typeof SNAPSHOTS.sortAccessors;

interface SnapshotsTableProps {
  rows: SnapshotInfo[];
  sortField: SnapshotField;
  sortDirection: "asc" | "desc";
  onSort: (field: SnapshotField) => void;
  /** Rendered in the empty row. Ordered by the page, which knows why the list is short. */
  empty: { title: string; description: string };
  /** A refetch is in flight over rows already on screen. */
  refreshing: boolean;
}

export function SnapshotsTable({
  rows,
  sortField,
  sortDirection,
  onSort,
  empty,
  refreshing,
}: SnapshotsTableProps) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {SNAPSHOT_HEADERS.map((header) =>
            header.field ? (
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
            colSpan={SNAPSHOT_COLUMN_COUNT}
            icon={refreshing ? Loader2 : History}
            iconClassName={refreshing ? "animate-spin" : undefined}
            title={refreshing ? "Loading snapshots" : empty.title}
            description={refreshing ? undefined : empty.description}
          />
        ) : (
          rows.map((snapshot) => {
            const scope = snapshotScope(snapshot);
            return (
              // The key carries the whole address. Two clusters can each hold an instance of the
              // same name, and so can two projects on one cluster, so the old
              // `source_name/name` collides the moment the read spans either.
              <TableRow key={`${scope.host}/${scope.project}/${snapshot.source_name}/${snapshot.name}`}>
                <TableCell className="font-medium">
                  <span className="font-mono">{snapshot.name}</span>
                </TableCell>
                <TableCell>{snapshot.source_name}</TableCell>
                <TableCell className="whitespace-nowrap">
                  <NodeLink clusterId={snapshot.host} emptyLabel="No cluster" />
                </TableCell>
                <TableCell className="whitespace-nowrap">{snapshot.project || "default"}</TableCell>
                <TableCell>{ORIGIN_LABEL[snapshotOrigin(snapshot)]}</TableCell>
                {/* nowrap: a squeezed table broke "15.89 MB" across two lines. */}
                <TableCell className="whitespace-nowrap">
                  {typeof snapshot.size === "number" ? (
                    formatBytes(snapshot.size)
                  ) : (
                    // Two different facts, two different names. "Not reported" means the cluster
                    // was asked and its storage driver answered nothing; "Not measured" means
                    // this read did not obtain a size for the instance: the holder cap, the
                    // deadline or a failed read. The row says which, so the page does not.
                    <TableEmptyValue
                      label={snapshot.size_measured === false ? "Not measured" : "Not reported"}
                    />
                  )}
                </TableCell>
                <TableCell className="whitespace-nowrap">{createdCell(snapshot.created_at)}</TableCell>
                <TableCell className="whitespace-nowrap">{expiryCell(snapshot.expires_at)}</TableCell>
                <TableCell pinned="end" className="text-right">
                  <SnapshotRowActions
                    snapshot={snapshot}
                    billingLinked={snapshot.source_externally_managed ?? false}
                  />
                </TableCell>
              </TableRow>
            );
          })
        )}
      </TableBody>
    </Table>
  );
}
