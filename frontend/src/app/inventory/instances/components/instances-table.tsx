"use client";

import {
  Table,
  TableBody,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyRow } from "@/components/ui/table-text";
import { Button } from "@/components/ui/button";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { formatInstanceType } from "@/lib/utils";
import { defineTable, fromSort } from "@/lib/table-filter";
import { keyFor } from "@/lib/pending-creates";
import { Box } from "lucide-react";
import type { PendingCreate } from "@/lib/pending-creates";
import type { VMInfo } from "@/types/api";
import type { RowFocusState } from "@/hooks/use-row-focus";
import { PendingCreateRows } from "./pending-create-rows";
import { InstanceTableRow } from "./instance-table-row";
import type { InstanceAction } from "./use-instance-actions";
import { instanceStatusDisplay } from "@/lib/instance-status";


// note: the facet fields are named `host` and `project` deliberately, matching both the
// existing sort fields and the OrcaHub deep-link keys. That contract keeps working with no
// translation layer. Only `?instance=` needs mapping, and it is handled in the component.
export const INSTANCES = defineTable<any>()({
  searchPlaceholder: "Search instances",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "name", label: "Name", sort: (i) => i.name, search: (i) => i.name, filter: fromSort("text") },
    {
      field: "status",
      label: "Status",
      sort: (i) => i.status,
      facet: (i) => i.status,
      facetSpec: { primary: true, format: (status) => instanceStatusDisplay(status).label },
    },
    {
      field: "type",
      label: "Type",
      sort: (i) => i.type,
      facet: (i) => i.type,
      // formatInstanceType, not formatLabel: formatLabel("vm") returns "VM" through the
      // acronym map, which would be a fifth vocabulary for this one enum.
      facetSpec: { format: formatInstanceType },
    },
    {
      field: "host",
      label: "Cluster",
      sort: (i) => i.host,
      search: (i) => i.host,
      facet: (i) => i.host,
      facetSpec: { role: "node", primary: true },
    },
    {
      field: "project",
      label: "Project",
      sort: (i) => i.project || "default",
      search: (i) => i.project,
      facet: (i) => i.project || "default",
      facetSpec: { role: "project", primary: true },
    },
    { field: "ip_addresses", label: "IP Addresses", sort: (i) => i.ip_addresses?.length ?? 0, filter: fromSort("number", { unit: "count" }) },
    { field: "profiles", label: "Profiles", sort: (i) => i.profiles?.length ?? 0, filter: fromSort("number", { unit: "count" }) },
    {
      field: "tags",
      label: "Tags",
      facet: (i) => (i.tags ?? []).map((t: any) => (typeof t === "string" ? t : t?.name)).filter(Boolean),
      facetSpec: { emptyLabel: "(untagged)" },
    },
  ],
});

type InstanceField = (typeof INSTANCES.sortAccessors) extends Record<infer K, unknown> ? K & string : never;

interface InstancesTableProps {
  rows: VMInfo[];
  pending: PendingCreate[];
  sortField: InstanceField;
  sortDirection: "asc" | "desc";
  onSort: (field: InstanceField) => void;
  activeFilterCount: number;
  canCreateInstance: boolean;
  onCreate: () => void;
  onViewPendingInstance: (pending: PendingCreate) => void;
  rowProps: RowFocusState["rowProps"];
  onOpen: (instance: VMInfo) => void;
  onAction: (name: string, host: string, project: string, action: InstanceAction) => void;
  pendingActions: ReadonlySet<string>;
  isReadOnly: boolean;
  writeDisabledReason?: string;
  migrationPlanningUnavailableReason?: string;
  migrationCopyUnavailableReason?: string;
  permissionReason: Parameters<typeof InstanceTableRow>[0]["permissionReason"];
  canDeleteInstance: boolean;
  deletePending: (instance: VMInfo) => boolean;
  onEditSpecification: (instance: VMInfo) => void;
  onCopy: (instance: VMInfo) => void;
  onMove: (instance: VMInfo) => void;
  onRebuild: (instance: VMInfo) => void;
  onDelete: (instance: VMInfo) => void;
}

/** The instance list's table: the placeholders for in-flight creates, then the rows. */
export function InstancesTable(props: InstancesTableProps) {
  const {
    rows: paginatedInstances,
    pending: visiblePendingCreates,
    sortField,
    sortDirection,
    onSort: handleSort,
    activeFilterCount,
    canCreateInstance,
    onCreate,
  } = props;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
          <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
          <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
          <SortableTableHead label="Cluster" field="host" currentField={sortField} direction={sortDirection} onSort={handleSort} />
          <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
          <SortableTableHead label="IP Addresses" field="ip_addresses" currentField={sortField} direction={sortDirection} onSort={handleSort} />
          <SortableTableHead label="Profiles" field="profiles" currentField={sortField} direction={sortDirection} onSort={handleSort} />
          <TableHead>Root Disk</TableHead>
          <TableHead>Tags</TableHead>
          {/* Not sortable and not a value, so it carries its name for a screen reader
              rather than a visible label that would read as a tenth column of data. */}
          <TableHead pinned="end" className="w-12 text-right">
            <span className="sr-only">Actions</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <PendingCreateRows
          pending={visiblePendingCreates}
          onViewInstance={props.onViewPendingInstance}
        />
        {paginatedInstances.length === 0 ? (
          visiblePendingCreates.length === 0 ? (
            <TableEmptyRow
              colSpan={10}
              icon={Box}
              title={
                activeFilterCount > 0
                  ? "No instances found"
                  : "No instances yet"
              }
              // The description was unconditional, so a deployment with no clusters at all
              // was told its filters were wrong. Eight sibling tables already guard this;
              // this is the most-visited one and was the outlier.
              description={
                activeFilterCount > 0
                  ? "No instance matches the current filters."
                  : "Instances appear here once a cluster reports one."
              }
              action={
                activeFilterCount === 0 && canCreateInstance ? (
                  <Button
                    size="sm"
                    onClick={onCreate}
                    disabled={!!props.writeDisabledReason}
                    title={props.writeDisabledReason ?? "Create a new instance"}
                  >
                    Create Instance
                  </Button>
                ) : undefined
              }
            />
          ) : null
        ) : (
          paginatedInstances.map((instance: any) => (
          <InstanceTableRow
            key={keyFor(instance.host, instance.project || "default", instance.name)}
            instance={instance}
            rowProps={props.rowProps(keyFor(instance.host, instance.project || "default", instance.name))}
            onOpen={props.onOpen}
            onAction={props.onAction}
            pendingActions={props.pendingActions}
            isReadOnly={props.isReadOnly}
            writeDisabledReason={props.writeDisabledReason}
            migrationPlanningUnavailableReason={props.migrationPlanningUnavailableReason}
            migrationCopyUnavailableReason={props.migrationCopyUnavailableReason}
            permissionReason={props.permissionReason}
            canDeleteInstance={props.canDeleteInstance}
            deletePending={props.deletePending(instance)}
            onEditSpecification={props.onEditSpecification}
            onCopy={props.onCopy}
            onMove={props.onMove}
            onRebuild={props.onRebuild}
            onDelete={props.onDelete}
          />
        ))
      )}
      </TableBody>
    </Table>
  );
}
