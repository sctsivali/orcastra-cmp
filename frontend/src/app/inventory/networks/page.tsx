"use client";

import { Suspense, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { RefreshButton } from "@/components/ui/refresh-button";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { Badge } from "@/components/ui/badge";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TableToolbar } from "@/components/ui/table-toolbar";
import {
  ProjectsCell,
  UsedByCell,
  UsedByDialog,
  useUsedByDialog,
  type UsedBySubject,
} from "@/components/used-by";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EMPTY_VALUE, TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { formatLabel } from "@/lib/utils";
import { TableLink } from "@/components/ui/table-link";
import { Plus, Network as NetworkIcon } from "lucide-react";
import { useNetworks } from "@/hooks/use-lxd";
import { Button } from "@/components/ui/button";
import { NetworkCreateDialog } from "@/components/networks/network-create-dialog";
import { usePermissions } from "@/hooks/use-permissions";
import { Role } from "@/types/rbac";
import type { NetworkInfo } from "@/types/api";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { useTableFilters } from "@/hooks/use-table-filters";
import { projectsFromUsedBy } from "@/lib/used-by";
import { defineTable, fromSort } from "@/lib/table-filter";
import { inventoryLinks } from "@/lib/inventory-links";
import { PageReadError } from "@/components/ui/read-error-state";
import { useRowFocus } from "@/hooks/use-row-focus";
import { requestRowFocus } from "@/lib/row-focus";
import { RowFocusNotice } from "@/components/ui/row-focus-notice";
import { NodeLink } from "@/components/ui/node-link";
import { networkStatusDisplay } from "@/lib/resource-status";

/** One key for a row and for the create dialog's report of what it made. */
const networkRowKey = (n: { node?: string | null; project?: string | null; name: string }) =>
  `${n.node ?? ""}/${n.project || "default"}/${n.name}`;

const NETWORKS = defineTable<NetworkInfo>()({
  searchPlaceholder: "Search networks",
  defaultSort: { field: "name", direction: "asc" },
  searchExtra: (n) => [n.description],
  columns: [
    { field: "name", label: "Name", sort: (n) => n.name, search: (n) => n.name, filter: fromSort("text") },
    {
      field: "type",
      label: "Type",
      sort: (n) => n.type,
      facet: (n) => n.type,
      // note: `type` means a different thing on five pages. Networks are LXD enums, so
      // formatLabel is right here; instances need formatInstanceType instead.
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      field: "node",
      label: "Node",
      sort: (n) => n.node,
      facet: (n) => n.node,
      facetSpec: { role: "node", primary: true },
    },
    { field: "ipv4", label: "IPv4 Address", sort: (n) => n.config?.["ipv4.address"], filter: fromSort("text") },
    { field: "ipv6", label: "IPv6 Address", sort: (n) => n.config?.["ipv6.address"], filter: fromSort("text") },
    {
      field: "status",
      label: "Status",
      sort: (n) => n.status,
      facet: (n) => n.status,
      facetSpec: { primary: true, format: (status) => networkStatusDisplay(status).label },
    },
    {
      field: "managed",
      label: "Managed",
      sort: (n) => n.managed,
      facet: (n) => (n.managed ? "Managed" : "Unmanaged"),
      facetSpec: {},
    },
    {
      field: "used_by",
      label: "Used By",
      sort: (n) => n.used_by?.length ?? 0,
      filter: fromSort("number", {
        unit: "count",
        presets: [
          { label: "None", min: null, max: 0 },
          { label: "Any", min: 1, max: null },
        ],
      }),
    },
    {
      field: "project",
      label: "Project",
      // The project that HOLDS this network, which is a different question from the
      // `projects` column below it: that one answers who consumes it.
      sort: (n) => n.project ?? "",
      facet: (n) => n.project ?? "",
      facetSpec: { role: "project", primary: true },
    },
    {
      // filter-coverage: this is not the project dimension. It answers which projects
      // CONSUME the record, derived from used_by, while the "project" column above answers
      // which one holds it, and that is the column carrying the project role.
      field: "projects",
      label: "Used By Projects",
      // Was a copy of the used_by accessor, so this column sorted by resource count while the
      // cell showed a project count. It now sorts by what it displays.
      sort: (n) => projectsFromUsedBy(n.used_by).length,
      facet: (n) => projectsFromUsedBy(n.used_by),
      facetSpec: { emptyLabel: "(no project)" },
    },
    {
      field: "usage",
      label: "Usage",
      facet: (n) => (n.used_by?.length ? "In use" : "Unused"),
      facetSpec: {},
    },
  ],
});

function NetworksContent() {
  const { data, isLoading, refreshNow, dataUpdatedAt, isError, error, refetch } = useNetworks();
  const networks: NetworkInfo[] = useMemo(() => (data as any)?.networks || [], [data]);

  const { hasRole } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  const canCreateNetwork = hasRole(Role.PARTNER);
  const [createOpen, setCreateOpen] = useState(false);
  // Nodes we could not reach: their networks are absent from the list, so warn
  // rather than silently showing "0 networks" (indistinguishable from empty).
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];

  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const resetPage = () => setCurrentPage(1);

  // note: derived from the rows, not from the fleet. It is empty exactly when the scope
  // returns nothing, which is the state that used to leave the empty row one column too wide.
  const nodes = useMemo(
    () => Array.from(new Set(networks.map((n) => n.node).filter(Boolean))).sort(),
    [networks],
  );

  // The column appears once any node is known, the facet needs something to choose between.
  // A pinned role does not override this: zero options means zero rows.
  const availableFields = useMemo(
    () => (nodes.length > 0 ? NETWORKS.facetFields : NETWORKS.facetFields.filter((f) => f !== "node")),
    [nodes.length],
  );

  // One dialog for the page, opened from either cell. See components/used-by.
  const usedBy = useUsedByDialog();

  const filters = useTableFilters(NETWORKS, networks, {
    onFilterChange: resetPage,
    availableFields,
  });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    NETWORKS.sortAccessors,
    NETWORKS.defaultSort.field,
    NETWORKS.defaultSort.direction,
    resetPage,
  );

  const sortedNetworks = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );
  const focus = useRowFocus({
    list: "networks",
    rows: networks,
    orderedRows: sortedNetworks,
    keyOf: networkRowKey,
    pageSize,
    setPage: setCurrentPage,
    dataUpdatedAt,
    clearFilters: filters.clearAll,
  });

  const totalPages = Math.ceil(sortedNetworks.length / pageSize);
  const paginatedNetworks = sortedNetworks.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  // Derived from the headers actually rendered, so the empty row cannot span the wrong width
  // when the conditional Node column is absent.
  const columnCount = nodes.length > 0 ? 10 : 9;

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  // A failed read is not an empty list. Rendered before the table so the filtered-empty row
  // cannot blame a filter for a request that did not return.
  if (isError && !data) {
    return (
      <PageReadError
        title="Networks"
        description="Manage hypervisor network configurations"
        noun="networks"
        error={error}
        onRetry={() => void refetch()}
      />
    );
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Networks
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor network configurations
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RefreshButton
            label="Refresh networks"
            lastUpdatedAt={dataUpdatedAt}
            onRefresh={refreshNow}
          />
          {/* Only Partner+ can create; the dialog is mounted for them alone and guards
              read-only mode itself. */}
          {canCreateNetwork && (
            <Button
              onClick={() => setCreateOpen(true)}
              disabled={isReadOnly}
              title={isReadOnly ? "Action disabled in read-only mode" : "Create a new network"}
            >
              <Plus className="h-4 w-4 mr-2" />
              Create Network
            </Button>
          )}
        </div>
      </div>

      <UnreachableNodesBanner noun="networks" nodes={unreachableNodes} />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>All Networks</CardTitle>
          <TableToolbar filters={filters} noun="networks" />
        </CardHeader>
        <CardContent>
          {focus.hidden && (
            <RowFocusNotice label={focus.hidden.label} onShow={focus.showHidden} className="mb-4" />
          )}
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && (
                  <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                )}
                <SortableTableHead label="IPv4 Address" field="ipv4" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="IPv6 Address" field="ipv6" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Managed" field="managed" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedNetworks.length === 0 ? (
                <TableEmptyRow
                  colSpan={columnCount}
                  icon={NetworkIcon}
                  title="No networks found"
                  description={
                    filters.activeFilterCount > 0
                      ? "No network matches the current filters."
                      : undefined
                  }
                />
              ) : (
                paginatedNetworks.map((network: any) => {
                  const rawIpv4 = network.config?.["ipv4.address"];
                  const ipv4 = !rawIpv4 || rawIpv4 === "none" ? null : rawIpv4;
                  const rawIpv6 = network.config?.["ipv6.address"];
                  const ipv6 = !rawIpv6 || rawIpv6 === "none" ? null : rawIpv6;
                  const status = networkStatusDisplay(network.status);

                  return (
                    <TableRow key={networkRowKey(network)} {...focus.rowProps(networkRowKey(network))}>
                      <TableCell className="font-medium">
                        <TableLink
                          href={inventoryLinks.networkDetail(network.name, network.node, network.project)}
                        >
                          {network.name}
                        </TableLink>
                      </TableCell>
                      <TableCell>
                        {formatLabel(network.type)}
                      </TableCell>
                      {nodes.length > 0 && (
                        <TableCell><NodeLink clusterId={network.node} /></TableCell>
                      )}
                      <TableCell className="font-mono">
                        {ipv4 ?? <TableEmptyValue label="No IPv4 address" />}
                      </TableCell>
                      <TableCell className="font-mono">
                        {ipv6 ?? <TableEmptyValue label="No IPv6 address" />}
                      </TableCell>
                      <TableCell>
                        {network.status ? (
                          <Badge variant={status.tone}>
                            {status.label}
                          </Badge>
                        ) : (
                          <TableEmptyValue label="No status" />
                        )}
                      </TableCell>
                      <TableCell>
                        {network.managed ? (
                          <>Yes</>
                        ) : (
                          <>No</>
                        )}
                      </TableCell>
                      <TableCell className="text-center">
                        <UsedByCell
                          usedBy={network.used_by}
                          noun="resources"
                          subjectName={network.name}
                          emptyLabel="Not used"
                          onOpen={() => usedBy.openUsage(networkSubject(network))}
                        />
                      </TableCell>
                      <TableCell>{network.project || EMPTY_VALUE}</TableCell>
                      <TableCell>
                        <ProjectsCell
                          usedBy={network.used_by}
                          subjectName={network.name}
                          onOpen={() => usedBy.openProjects(networkSubject(network))}
                        />
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>

          {totalPages > 1 && (
            <div className="mt-4">
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                pageSize={pageSize}
                totalItems={sortedNetworks.length}
                onPageChange={setCurrentPage}
                onPageSizeChange={(size) => {
                  setPageSize(size);
                  setCurrentPage(1);
                }}
              />
            </div>
          )}
        </CardContent>
      </Card>

      <UsedByDialog {...usedBy.dialogProps} />
      {canCreateNetwork && (
        <NetworkCreateDialog
          open={createOpen}
          onOpenChange={setCreateOpen}
          // the create already succeeded; a refresh that fails is the list's own error state.
          // The focus intent is raised BEFORE the refresh so the list that arrives is newer.
          onSaved={(created) => {
            requestRowFocus("networks", networkRowKey(created), created.name);
            void Promise.resolve(refreshNow()).catch(() => undefined);
          }}
        />
      )}
    </div>
  );
}

// Suspense is required because useTableFilters reads useSearchParams, which opts the
// component out of prerendering.
export default function NetworksPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <NetworksContent />
    </Suspense>
  );
}

// note: the network dialog is the only one whose title links out, because /inventory/networks/
// [name] is the app's one real detail route. The config panel surfaces ipv4.nat, dns.domain,
// bridge.mtu, the OVN uplink and security.acls, none of which the table has a column for.
function networkSubject(network: NetworkInfo): UsedBySubject {
  const config = network.config ?? {};
  return {
    kind: "networks",
    name: network.name,
    node: network.node,
    project: network.project ?? undefined,
    description: network.description || undefined,
    usedBy: network.used_by ?? [],
    facts: [
      { label: "Type", value: formatLabel(network.type) },
      { label: "Status", value: networkStatusDisplay(network.status).label },
      { label: "Managed", value: network.managed ? "Yes" : "No" },
      { label: "Node", value: <NodeLink clusterId={network.node} /> },
      { label: "IPv4", value: config["ipv4.address"] || EMPTY_VALUE },
      { label: "IPv6", value: config["ipv6.address"] || EMPTY_VALUE },
    ],
    panels: [{ id: "config", label: "Configuration", kind: "config", entries: config }],
  };
}
