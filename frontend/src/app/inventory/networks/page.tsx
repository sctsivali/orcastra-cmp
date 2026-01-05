"use client";

import { useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
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
import { Network, RotateCw, Search, ArrowUp, ArrowDown } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useNetworks } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";

type SortField = "name" | "type" | "status" | "used_by";
type SortDirection = "asc" | "desc";

interface NetworkInfo {
  name: string;
  description: string;
  type: string;
  managed: boolean;
  config: Record<string, string>;
  used_by: string[];
  status: string;
  node?: string;
}

export default function NetworksPage() {
  const { data, isLoading, refetch, isRefetching } = useNetworks();
  const networks = (data as any)?.networks || [];
  
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [filterType, setFilterType] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [sortField, setSortField] = useState<SortField>("name");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const { toast } = useToast();

  const nodes = useMemo(() => {
    const nodeSet = new Set(networks.map((n: any) => n.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [networks]);

  const networkTypes = useMemo(() => {
    const typeSet = new Set(networks.map((n: any) => n.type));
    return Array.from(typeSet).sort();
  }, [networks]);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("asc");
    }
    setCurrentPage(1);
  };

  const filteredNetworks = useMemo(() => {
    let filtered = networks;

    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (network: any) =>
          network.name.toLowerCase().includes(query) ||
          (network.description && network.description.toLowerCase().includes(query))
      );
    }

    if (filterNode !== "all") {
      filtered = filtered.filter((network: any) => network.node === filterNode);
    }

    if (filterType !== "all") {
      filtered = filtered.filter((network: any) => network.type === filterType);
    }

    filtered.sort((a: any, b: any) => {
      let aVal, bVal;
      
      switch (sortField) {
        case "name":
          aVal = a.name;
          bVal = b.name;
          break;
        case "type":
          aVal = a.type || "";
          bVal = b.type || "";
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
  }, [networks, searchQuery, filterNode, filterType, sortField, sortDirection]);

  const totalPages = Math.ceil(filteredNetworks.length / pageSize);
  const paginatedNetworks = filteredNetworks.slice(
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
          <h1 className="text-3xl font-bold flex items-center gap-2">
            <Network className="h-8 w-8" />
            Networks
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor network configurations
          </p>
        </div>
        <Button onClick={() => refetch()} disabled={isRefetching} size="sm" className="gap-2">
          <RotateCw className={`h-4 w-4 ${isRefetching ? "animate-spin" : ""}`} />
          <span className="hidden sm:inline">Refresh</span>
        </Button>
      </div>

      <Card>
        <CardContent className="pt-6">
          <div className="flex flex-col sm:flex-row gap-4">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search networks..."
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
            <Select value={filterType} onValueChange={(value) => {
              setFilterType(value);
              setCurrentPage(1);
            }}>
              <SelectTrigger className="w-full sm:w-[180px]">
                <SelectValue placeholder="All Types" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Types</SelectItem>
                {networkTypes.map((type: any) => (
                  <SelectItem key={type} value={type}>
                    {type}
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
            All Networks ({filteredNetworks.length})
            {(searchQuery || filterNode !== "all" || filterType !== "all") ? ` (filtered from ${networks.length})` : ""}
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
                  onClick={() => handleSort("type")}
                >
                  Type <SortIcon field="type" />
                </TableHead>
                {nodes.length > 0 && <TableHead>Node</TableHead>}
                <TableHead>IPv4 Address</TableHead>
                <TableHead>IPv6 Address</TableHead>
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("status")}
                >
                  Status <SortIcon field="status" />
                </TableHead>
                <TableHead>Managed</TableHead>
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
              {paginatedNetworks.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={9} className="text-center text-muted-foreground">
                    No networks found
                  </TableCell>
                </TableRow>
              ) : (
                paginatedNetworks.map((network: any, idx: number) => {
                  const ipv4 = network.config?.["ipv4.address"] || "N/A";
                  const ipv6 = network.config?.["ipv6.address"] || "N/A";
                  
                  return (
                    <TableRow key={`${network.node}-${network.name}-${idx}`}>
                      <TableCell className="font-medium">{network.name}</TableCell>
                      <TableCell>
                        <Badge variant="outline">{network.type}</Badge>
                      </TableCell>
                      {nodes.length > 0 && (
                        <TableCell>
                          {network.node && <Badge variant="secondary">{network.node}</Badge>}
                        </TableCell>
                      )}
                      <TableCell>
                        <code className="text-xs">{ipv4}</code>
                      </TableCell>
                      <TableCell>
                        <code className="text-xs">{ipv6}</code>
                      </TableCell>
                      <TableCell>
                        <Badge variant={network.status === "Created" ? "default" : "secondary"}>
                          {network.status}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        {network.managed ? (
                          <Badge variant="default">Yes</Badge>
                        ) : (
                          <Badge variant="outline">No</Badge>
                        )}
                      </TableCell>
                      <TableCell className="text-center">
                        <ClickableBadge
                          count={network.used_by?.length || 0}
                          label="resources"
                          items={network.used_by || []}
                          title={`Resources using ${network.name}`}
                          description={`${network.used_by?.length || 0} resource(s) using this network`}
                        />
                      </TableCell>
                      <TableCell>
                        <UsedByProjects
                          usedBy={network.used_by || []}
                          resourceName={network.name}
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
                totalItems={filteredNetworks.length}
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
