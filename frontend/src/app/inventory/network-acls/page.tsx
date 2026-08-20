"use client";

import { Suspense, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { RefreshButton } from "@/components/ui/refresh-button";
import { ClickableBadge } from "@/components/ui/clickable-badge";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { UsedByProjects } from "@/components/used-by-projects";
import {
  Table,
  TableBody,
  TableCell,
  
  TableHeader,
  TableRow } from "@/components/ui/table";
import { ShieldCheck } from "lucide-react";
import { TableEmptyRow } from "@/components/ui/table-text";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { projectsFromUsedBy } from "@/lib/used-by";
import { defineTable, fromSort } from "@/lib/table-filter";
import { useNetworkACLs } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

interface ACLInfo {
  name: string;
  description: string;
  ingress: any[];
  egress: any[];
  config: Record<string, string>;
  used_by: string[];
  node?: string;
}

const ACLS = defineTable<ACLInfo>()({
  searchPlaceholder: "Search network ACLs",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "name", label: "Name", sort: (a) => a.name, search: (a) => a.name, filter: fromSort("text") },
    {
      field: "description",
      label: "Description",
      sort: (a) => a.description,
      search: (a) => a.description, filter: fromSort("text") },
    {
      field: "node",
      label: "Node",
      sort: (a) => a.node,
      facet: (a) => a.node,
      facetSpec: { role: "node", primary: true } },
    { field: "ingress", label: "Ingress Rules", sort: (a) => a.ingress?.length ?? 0, filter: fromSort("number", { unit: "count" }) },
    { field: "egress", label: "Egress Rules", sort: (a) => a.egress?.length ?? 0, filter: fromSort("number", { unit: "count" }) },
    { field: "used_by", label: "Used By", sort: (a) => a.used_by?.length ?? 0, filter: fromSort("number", { unit: "count", presets: [{ label: "None", min: null, max: 0 }, { label: "Any", min: 1, max: null }] }) },
    {
      field: "projects",
      label: "Projects",
      // Was a copy of the used_by accessor, so the header sorted by resource count while the
      // cell displayed a project count. It now sorts by what it displays.
      sort: (a) => projectsFromUsedBy(a.used_by).length,
      facet: (a) => projectsFromUsedBy(a.used_by),
      facetSpec: { role: "project", primary: true, emptyLabel: "(no project)" } },
    {
      field: "usage",
      label: "Usage",
      facet: (a) => (a.used_by?.length ? "In use" : "Unused"),
      facetSpec: {} },
  ] });

function NetworkACLsContent() {
  const { data, isLoading, refreshNow, dataUpdatedAt } = useNetworkACLs();
  const acls = (data as any)?.acls || [];
  
  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const resetPage = () => setCurrentPage(1);

  // note: derived from the rows, not from the fleet. It is empty exactly when the scope
  // returns nothing, which is the state that used to leave the empty row one column too wide.
  const nodes = useMemo(() => {
    const nodeSet = new Set(acls.map((a: any) => a.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [acls]);

  // The column appears once any node is known, the facet once there are two to choose
  // between. The thresholds differ on purpose: one option filters nothing.
  const availableFields = useMemo(
    () => (nodes.length > 0 ? ACLS.facetFields : ACLS.facetFields.filter((f) => f !== "node")),
    [nodes.length],
  );

  const filters = useTableFilters(ACLS, acls as ACLInfo[], {
    onFilterChange: resetPage,
    availableFields });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    ACLS.sortAccessors,
    ACLS.defaultSort.field,
    ACLS.defaultSort.direction,
    resetPage,
  );

  const sortedACLs = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  const totalPages = Math.ceil(sortedACLs.length / pageSize);
  const paginatedACLs = sortedACLs.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize
  );

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Network ACLs
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor network access control lists
          </p>
        </div>
        <RefreshButton
          label="Refresh network ACLs"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner noun="network ACLs" nodes={unreachableNodes} />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>All Network ACLs</CardTitle>
          <TableToolbar filters={filters} noun="network ACLs" />
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Description" field="description" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />}
                <SortableTableHead label="Ingress Rules" field="ingress" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Egress Rules" field="egress" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedACLs.length === 0 ? (
                <TableEmptyRow
                  colSpan={nodes.length > 0 ? 7 : 6}
                  icon={ShieldCheck}
                  title="No network ACLs found"
                  description={
                    filters.activeFilterCount > 0
                      ? "No network ACL matches the current filters."
                      : undefined
                  }
                />
              ) : (
                paginatedACLs.map((acl: any, idx: number) => (
                  <TableRow key={`${acl.node}-${acl.name}-${idx}`}>
                    <TableCell className="font-medium">{acl.name}</TableCell>
                    <TableCell className="max-w-md truncate">
                      {acl.description || <span className="text-muted-foreground">No description</span>}
                    </TableCell>
                    {nodes.length > 0 && (
                      <TableCell>
                        {acl.node}
                      </TableCell>
                    )}
                    <TableCell>
                      {acl.ingress?.length || 0} rules
                    </TableCell>
                    <TableCell>
                      {acl.egress?.length || 0} rules
                    </TableCell>
                    <TableCell>
                      <ClickableBadge
                        count={acl.used_by?.length || 0}
                        label="networks"
                        items={acl.used_by || []}
                        title={`Networks using ${acl.name}`}
                        description={`${acl.used_by?.length || 0} network(s) using this ACL`}
                      />
                    </TableCell>
                    <TableCell>
                      <UsedByProjects
                        usedBy={acl.used_by || []}
                        resourceName={acl.name}
                      />
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>

          {totalPages > 1 && (
            <div className="mt-4">
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                pageSize={pageSize}
                totalItems={sortedACLs.length}
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
    </div>
  );
}

// Suspense is required because useTableFilters reads useSearchParams, which opts the
// component out of prerendering.
export default function NetworkACLsPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <NetworkACLsContent />
    </Suspense>
  );
}
