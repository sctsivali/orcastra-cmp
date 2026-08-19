"use client";

import { useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
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
import { TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { formatLabel } from "@/lib/utils";
import { 
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Search, Database } from "lucide-react";
import { useStoragePools } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

interface PoolInfo {
  name: string;
  description: string;
  driver: string;
  config: Record<string, string>;
  used_by: string[];
  status: string;
  node?: string;
}

interface PoolResources {
  space: {
    used: number;
    total: number;
  };
  inodes: {
    used: number;
    total: number;
  };
}

export default function StoragePoolsPage() {
  const { data, isLoading, refreshNow, dataUpdatedAt } = useStoragePools();
  const pools = (data as any)?.pools || [];
  const poolResources = (data as any)?.resources || {};
  
  const [searchQuery, setSearchQuery] = useState("");
  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];
  const [filterNode, setFilterNode] = useState<string>("all");
  const [filterDriver, setFilterDriver] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  const sortAccessors = useMemo(
    () => ({
      name: (i: any) => i.name,
      driver: (i: any) => i.driver,
      node: (i: any) => i.node,
      status: (i: any) => i.status,
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
    const nodeSet = new Set(pools.map((p: any) => p.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [pools]);

  const drivers = useMemo(() => {
    const driverSet = new Set(pools.map((p: any) => p.driver));
    return Array.from(driverSet).sort();
  }, [pools]);

  const formatBytes = (bytes: number) => {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB", "PB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + " " + sizes[i];
  };

  const filteredPools = useMemo(() => {
    let filtered = pools;

    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (pool: any) =>
          pool.name.toLowerCase().includes(query) ||
          (pool.description && pool.description.toLowerCase().includes(query))
      );
    }

    if (filterNode !== "all") {
      filtered = filtered.filter((pool: any) => pool.node === filterNode);
    }

    if (filterDriver !== "all") {
      filtered = filtered.filter((pool: any) => pool.driver === filterDriver);
    }

    return filtered;
  }, [pools, searchQuery, filterNode, filterDriver]);

  const sortedPools = useMemo(() => sortItems(filteredPools), [filteredPools, sortItems]);

  const totalPages = Math.ceil(sortedPools.length / pageSize);
  const paginatedPools = sortedPools.slice(
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
            Storage Pools
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor storage pools
          </p>
        </div>
        <RefreshButton
          label="Refresh storage pools"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner noun="storage pools" nodes={unreachableNodes} />

      <Card>
        <CardContent className="pt-6">
          <div className="flex flex-col sm:flex-row gap-4">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search storage pools..."
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
            <Select value={filterDriver} onValueChange={(value) => {
              setFilterDriver(value);
              setCurrentPage(1);
            }}>
              <SelectTrigger className="w-full sm:w-[180px]">
                <SelectValue placeholder="All Drivers" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Drivers</SelectItem>
                {drivers.map((driver: any) => (
                  <SelectItem key={driver} value={driver}>
                    {driver}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>
            All Storage Pools ({filteredPools.length})
            {(searchQuery || filterNode !== "all" || filterDriver !== "all") ? ` (filtered from ${pools.length})` : ""}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Driver" field="driver" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && (
                  <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                )}
                <TableHead>Size (Used / Total)</TableHead>
                <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedPools.length === 0 ? (
                <TableEmptyRow colSpan={7} icon={Database} title="No storage pools found" />
              ) : (
                paginatedPools.map((pool: any, idx: number) => {
                  const poolKey = `${pool.node}-${pool.name}`;
                  const resources = poolResources[poolKey];
                  
                  return (
                    <TableRow key={`${poolKey}-${idx}`}>
                      <TableCell className="font-medium">{pool.name}</TableCell>
                      <TableCell>
                        {formatLabel(pool.driver)}
                      </TableCell>
                      {nodes.length > 0 && (
                        <TableCell>
                          {pool.node}
                        </TableCell>
                      )}
                      <TableCell>
                        {resources ? (
                          <>
                            {formatBytes(resources.space.used)} / {formatBytes(resources.space.total)}
                          </>
                        ) : (
                          <TableEmptyValue label="Usage unavailable" />
                        )}
                      </TableCell>
                      <TableCell>
                        <Badge variant={pool.status === "Created" ? "default" : "secondary"}>
                          {pool.status}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <ClickableBadge
                          count={pool.used_by?.length || 0}
                          label="volumes"
                          items={pool.used_by || []}
                          title={`Volumes in ${pool.name}`}
                          description={`${pool.used_by?.length || 0} volume(s) in this pool`}
                        />
                      </TableCell>
                      <TableCell>
                        <UsedByProjects
                          usedBy={pool.used_by || []}
                          resourceName={pool.name}
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
                totalItems={filteredPools.length}
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
