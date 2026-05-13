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
import { Search, ArrowUp, ArrowDown } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useStoragePools } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";

type SortField = "name" | "driver" | "status" | "used_by";
type SortDirection = "asc" | "desc";

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
  const { data, isLoading, refetch, isRefetching } = useStoragePools();
  const pools = (data as any)?.pools || [];
  const poolResources = (data as any)?.resources || {};
  
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [filterDriver, setFilterDriver] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [sortField, setSortField] = useState<SortField>("name");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const { toast } = useToast();

  const nodes = useMemo(() => {
    const nodeSet = new Set(pools.map((p: any) => p.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [pools]);

  const drivers = useMemo(() => {
    const driverSet = new Set(pools.map((p: any) => p.driver));
    return Array.from(driverSet).sort();
  }, [pools]);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("asc");
    }
    setCurrentPage(1);
  };

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

    filtered.sort((a: any, b: any) => {
      let aVal, bVal;
      
      switch (sortField) {
        case "name":
          aVal = a.name;
          bVal = b.name;
          break;
        case "driver":
          aVal = a.driver || "";
          bVal = b.driver || "";
          break;
        case "status":
          aVal = a.status || "";
          bVal = b.status || "";
          break;
        case "used_by":
          aVal = a.used_by?.length || 0;
          bVal = b.used_by?.length || 0;
          break;
        default:
          return 0;
      }

      if (aVal < bVal) return sortDirection === "asc" ? -1 : 1;
      if (aVal > bVal) return sortDirection === "asc" ? 1 : -1;
      return 0;
    });

    return filtered;
  }, [pools, searchQuery, filterNode, filterDriver, sortField, sortDirection]);

  const totalPages = Math.ceil(filteredPools.length / pageSize);
  const paginatedPools = filteredPools.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize
  );

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return null;
    return sortDirection === "asc" ? (
      <ArrowUp className="h-3 w-3 inline ml-1" />
    ) : (
      <ArrowDown className="h-3 w-3 inline ml-1" />
    );
  };

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
          disabled={isRefetching}
          isRefreshing={isRefetching}
          label="Refresh storage pools"
          onClick={() => refetch()}
        />
      </div>

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
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("name")}
                >
                  Name <SortIcon field="name" />
                </TableHead>
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("driver")}
                >
                  Driver <SortIcon field="driver" />
                </TableHead>
                {nodes.length > 0 && <TableHead>Node</TableHead>}
                <TableHead>Size (Used / Total)</TableHead>
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("status")}
                >
                  Status <SortIcon field="status" />
                </TableHead>
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("used_by")}
                >
                  Used By <SortIcon field="used_by" />
                </TableHead>
                <TableHead>Projects</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedPools.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} className="text-center text-muted-foreground">
                    No storage pools found
                  </TableCell>
                </TableRow>
              ) : (
                paginatedPools.map((pool: any, idx: number) => {
                  const poolKey = `${pool.node}-${pool.name}`;
                  const resources = poolResources[poolKey];
                  
                  return (
                    <TableRow key={`${poolKey}-${idx}`}>
                      <TableCell className="font-medium">{pool.name}</TableCell>
                      <TableCell>
                        <Badge variant="outline">{pool.driver}</Badge>
                      </TableCell>
                      {nodes.length > 0 && (
                        <TableCell>
                          {pool.node && <Badge variant="secondary">{pool.node}</Badge>}
                        </TableCell>
                      )}
                      <TableCell>
                        {resources ? (
                          <span className="text-xs">
                            {formatBytes(resources.space.used)} / {formatBytes(resources.space.total)}
                          </span>
                        ) : (
                          <span className="text-muted-foreground text-xs">N/A</span>
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
