"use client";

import { useState, useEffect, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
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
import { Network, Loader2, RotateCw, Search, ArrowUp, ArrowDown } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { API_URL } from "@/lib/api";

type SortField = "address" | "type" | "used_by" | "network";
type SortDirection = "asc" | "desc";

interface IPAMAllocation {
  address: string;
  type: string;
  used_by: string;
  hardware_address: string;
  network: string;
  node: string;
  project: string;
}

interface IPAMListResponse {
  total: number;
  allocations: IPAMAllocation[];
  nodes: string[];
}

export default function IPAMPage() {
  const [allocations, setAllocations] = useState<IPAMAllocation[]>([]);
  const [nodes, setNodes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [filterNetwork, setFilterNetwork] = useState<string>("all");
  const [filterType, setFilterType] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [sortField, setSortField] = useState<SortField>("address");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const { toast } = useToast();

  useEffect(() => {
    fetchAllocations();
  }, []);

  const fetchAllocations = async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_URL}/ipam`);
      if (!response.ok) throw new Error("Failed to fetch IPAM allocations");
      const data: IPAMListResponse = await response.json();
      setAllocations(data.allocations);
      setNodes(data.nodes);
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to fetch IPAM allocations",
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const networks = useMemo(() => {
    const networkSet = new Set(allocations.map((a) => a.network));
    return Array.from(networkSet).sort();
  }, [allocations]);

  const types = useMemo(() => {
    const typeSet = new Set(allocations.map((a) => a.type));
    return Array.from(typeSet).sort();
  }, [allocations]);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("asc");
    }
    setCurrentPage(1);
  };

  const filteredAllocations = useMemo(() => {
    let filtered = allocations;

    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (alloc) =>
          alloc.address.toLowerCase().includes(query) ||
          alloc.used_by.toLowerCase().includes(query) ||
          alloc.hardware_address.toLowerCase().includes(query)
      );
    }

    if (filterNode !== "all") {
      filtered = filtered.filter((alloc) => alloc.node === filterNode);
    }

    if (filterNetwork !== "all") {
      filtered = filtered.filter((alloc) => alloc.network === filterNetwork);
    }

    if (filterType !== "all") {
      filtered = filtered.filter((alloc) => alloc.type === filterType);
    }

    filtered.sort((a, b) => {
      let aVal, bVal;

      switch (sortField) {
        case "address":
          aVal = a.address;
          bVal = b.address;
          break;
        case "type":
          aVal = a.type;
          bVal = b.type;
          break;
        case "used_by":
          aVal = a.used_by;
          bVal = b.used_by;
          break;
        case "network":
          aVal = a.network;
          bVal = b.network;
          break;
        default:
          return 0;
      }

      if (aVal < bVal) return sortDirection === "asc" ? -1 : 1;
      if (aVal > bVal) return sortDirection === "asc" ? 1 : -1;
      return 0;
    });

    return filtered;
  }, [allocations, searchQuery, filterNode, filterNetwork, filterType, sortField, sortDirection]);

  const totalPages = Math.ceil(filteredAllocations.length / pageSize);
  const paginatedAllocations = filteredAllocations.slice(
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

  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen">
        <Loader2 className="h-8 w-8 animate-spin" />
      </div>
    );
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-2">
            <Network className="h-8 w-8" />
            IPAM
          </h1>
          <p className="text-muted-foreground mt-2">
            IP Address Management - View and manage IP allocations across networks
          </p>
        </div>
        <Button onClick={fetchAllocations}>
          <RotateCw className="h-4 w-4 mr-2" />
          Refresh
        </Button>
      </div>

      <Card>
        <CardContent className="pt-6">
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search IP, MAC, or resource..."
                value={searchQuery}
                onChange={(e) => {
                  setSearchQuery(e.target.value);
                  setCurrentPage(1);
                }}
                className="pl-10"
              />
            </div>
            {nodes.length > 0 && (
              <Select
                value={filterNode}
                onValueChange={(value) => {
                  setFilterNode(value);
                  setCurrentPage(1);
                }}
              >
                <SelectTrigger>
                  <SelectValue placeholder="All Nodes" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Nodes</SelectItem>
                  {nodes.map((node) => (
                    <SelectItem key={node} value={node}>
                      {node}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <Select
              value={filterNetwork}
              onValueChange={(value) => {
                setFilterNetwork(value);
                setCurrentPage(1);
              }}
            >
              <SelectTrigger>
                <SelectValue placeholder="All Networks" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Networks</SelectItem>
                {networks.map((network) => (
                  <SelectItem key={network} value={network}>
                    {network}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={filterType}
              onValueChange={(value) => {
                setFilterType(value);
                setCurrentPage(1);
              }}
            >
              <SelectTrigger>
                <SelectValue placeholder="All Types" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Types</SelectItem>
                {types.map((type) => (
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
            IP Address Allocations ({filteredAllocations.length})
            {(searchQuery || filterNode !== "all" || filterNetwork !== "all" || filterType !== "all")
              ? ` (filtered from ${allocations.length})`
              : ""}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("address")}
                >
                  IP Address <SortIcon field="address" />
                </TableHead>
                <TableHead
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("type")}
                >
                  Type <SortIcon field="type" />
                </TableHead>
                <TableHead
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("used_by")}
                >
                  Used By <SortIcon field="used_by" />
                </TableHead>
                <TableHead>MAC Address</TableHead>
                <TableHead
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("network")}
                >
                  Network <SortIcon field="network" />
                </TableHead>
                <TableHead>Node</TableHead>
                <TableHead>Project</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedAllocations.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={7}
                    className="text-center text-muted-foreground"
                  >
                    No IP allocations found
                  </TableCell>
                </TableRow>
              ) : (
                paginatedAllocations.map((alloc, idx) => (
                  <TableRow key={`${alloc.node}-${alloc.address}-${idx}`}>
                    <TableCell className="font-mono">
                      {alloc.address}
                    </TableCell>
                    <TableCell>
                      <Badge variant={alloc.type === "instance" ? "default" : "secondary"}>
                        {alloc.type}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-medium">
                      {alloc.used_by}
                    </TableCell>
                    <TableCell>
                      {alloc.hardware_address ? (
                        <code className="text-xs">{alloc.hardware_address}</code>
                      ) : (
                        <span className="text-muted-foreground text-xs">N/A</span>
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{alloc.network}</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">{alloc.node}</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">{alloc.project || 'default'}</Badge>
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
                totalItems={filteredAllocations.length}
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
