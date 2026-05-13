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
import { useNetworkACLs } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";

type SortField = "name" | "description" | "used_by";
type SortDirection = "asc" | "desc";

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
  const { data, isLoading, refetch, isRefetching } = useNetworkACLs();
  const acls = (data as any)?.acls || [];
  
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [sortField, setSortField] = useState<SortField>("name");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const { toast } = useToast();

  const nodes = useMemo(() => {
    const nodeSet = new Set(acls.map((a: any) => a.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [acls]);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("asc");
    }
    setCurrentPage(1);
  };

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

    filtered.sort((a: any, b: any) => {
      let aVal, bVal;
      
      switch (sortField) {
        case "name":
          aVal = a.name;
          bVal = b.name;
          break;
        case "description":
          aVal = a.description || "";
          bVal = b.description || "";
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
  }, [acls, searchQuery, filterNode, sortField, sortDirection]);

  const totalPages = Math.ceil(filteredACLs.length / pageSize);
  const paginatedACLs = filteredACLs.slice(
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
            Network ACLs
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor network access control lists
          </p>
        </div>
        <RefreshButton
          disabled={isRefetching}
          isRefreshing={isRefetching}
          label="Refresh network ACLs"
          onClick={() => refetch()}
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
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("name")}
                >
                  Name <SortIcon field="name" />
                </TableHead>
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("description")}
                >
                  Description <SortIcon field="description" />
                </TableHead>
                {nodes.length > 0 && <TableHead>Node</TableHead>}
                <TableHead>Ingress Rules</TableHead>
                <TableHead>Egress Rules</TableHead>
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
                        {acl.node && <Badge variant="secondary">{acl.node}</Badge>}
                      </TableCell>
                    )}
                    <TableCell>
                      <Badge variant="outline">{acl.ingress?.length || 0} rules</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{acl.egress?.length || 0} rules</Badge>
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
