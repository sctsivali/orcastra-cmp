"use client";

import { useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { ClickableBadge } from "@/components/ui/clickable-badge";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { UsedByProjects } from "@/components/used-by-projects";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { 
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Search } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
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

export default function NetworkACLsPage() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useNetworkACLs();
  const acls = (data as any)?.acls || [];
  
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const { toast } = useToast();

  const sortAccessors = useMemo(
    () => ({
      name: (i: any) => i.name,
      description: (i: any) => i.description,
      node: (i: any) => i.node,
      ingress: (i: any) => i.ingress?.length ?? 0,
      egress: (i: any) => i.egress?.length ?? 0,
      used_by: (i: any) => i.used_by?.length ?? 0,
      projects: (i: any) => i.used_by?.length ?? 0,
    }),
    [],
  );
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    sortAccessors,
    "name",
    "asc",
    () => setCurrentPage(1),
  );

  const nodes = useMemo(() => {
    const nodeSet = new Set(acls.map((a: any) => a.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [acls]);

  const filteredACLs = useMemo(() => {
    let filtered = acls;

    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (acl: any) =>
          acl.name.toLowerCase().includes(query) ||
          (acl.description && acl.description.toLowerCase().includes(query))
      );
    }

    if (filterNode !== "all") {
      filtered = filtered.filter((acl: any) => acl.node === filterNode);
    }

    return filtered;
  }, [acls, searchQuery, filterNode]);

  const sortedACLs = useMemo(() => sortItems(filteredACLs), [filteredACLs, sortItems]);

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

      <Card>
        <CardContent className="pt-6">
          <div className="flex flex-col sm:flex-row gap-4">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search network ACLs..."
                value={searchQuery}
                onChange={(e) => {
                  setSearchQuery(e.target.value);
                  setCurrentPage(1);
                }}
                className="pl-10"
              />
            </div>
            {nodes.length > 0 && (
              <Select value={filterNode} onValueChange={(value) => {
                setFilterNode(value);
                setCurrentPage(1);
              }}>
                <SelectTrigger className="w-full sm:w-[180px]">
                  <SelectValue placeholder="All Nodes" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Nodes</SelectItem>
                  {nodes.map((node: any) => (
                    <SelectItem key={node} value={node}>
                      {node}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>
            All Network ACLs ({filteredACLs.length})
            {(searchQuery || filterNode !== "all") ? ` (filtered from ${acls.length})` : ""}
          </CardTitle>
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
                <TableRow>
                  <TableCell colSpan={7} className="text-center text-muted-foreground">
                    No network ACLs found
                  </TableCell>
                </TableRow>
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
                totalItems={filteredACLs.length}
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
