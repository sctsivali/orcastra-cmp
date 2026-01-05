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
import { Settings, RotateCw, Search, ArrowUp, ArrowDown } from "lucide-react";
import { ProfileInfo } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { useProfiles } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";

type SortField = "name" | "description" | "devices" | "used_by";
type SortDirection = "asc" | "desc";

export default function ProfilesPage() {
  const { data, isLoading, refetch, isRefetching } = useProfiles();
  const profiles = (data as any)?.profiles || [];
  
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [sortField, setSortField] = useState<SortField>("name");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const { toast } = useToast();

  // Get unique nodes
  const nodes = useMemo(() => {
    const nodeSet = new Set(profiles.map((p: any) => p.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [profiles]);

  // Handle column sort
  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("asc");
    }
    setCurrentPage(1);
  };

  // Filter and search
  const filteredProfiles = useMemo(() => {
    let filtered = profiles;

    // Search
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (profile: any) =>
          profile.name.toLowerCase().includes(query) ||
          (profile.description && profile.description.toLowerCase().includes(query))
      );
    }

    // Filter by node
    if (filterNode !== "all") {
      filtered = filtered.filter((profile: any) => profile.node === filterNode);
    }

    // Sort
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
        case "devices":
          aVal = Object.keys(a.devices || {}).length;
          bVal = Object.keys(b.devices || {}).length;
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
  }, [profiles, searchQuery, filterNode, sortField, sortDirection]);

  // Pagination
  const totalPages = Math.ceil(filteredProfiles.length / pageSize);
  const paginatedProfiles = filteredProfiles.slice(
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
            <Settings className="h-8 w-8" />
            Profiles
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor configuration profiles
          </p>
        </div>
        <Button onClick={() => refetch()} disabled={isRefetching} size="sm" className="gap-2">
          <RotateCw className={`h-4 w-4 ${isRefetching ? "animate-spin" : ""}`} />
          <span className="hidden sm:inline">Refresh</span>
        </Button>
      </div>

      {/* Search and Filters */}
      <Card>
        <CardContent className="pt-6">
          <div className="flex flex-col sm:flex-row gap-4">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search profiles..."
                value={searchQuery}
                onChange={(e) => {
                  setSearchQuery(e.target.value);
                  setCurrentPage(1);
                }}
                className="pl-10"
              />
            </div>
            <Select value={filterNode} onValueChange={(value) => {
              setFilterNode(value);
              setCurrentPage(1);
            }}>
              <SelectTrigger className="w-full sm:w-[200px]">
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
          </div>
        </CardContent>
      </Card>

      {/* Profiles Table */}
      <Card>
        <CardHeader>
          <CardTitle>
            All Profiles ({filteredProfiles.length})
            {searchQuery || filterNode !== "all" ? ` (filtered from ${profiles.length})` : ""}
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
                <TableHead>Node</TableHead>
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("devices")}
                >
                  Devices <SortIcon field="devices" />
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
              {paginatedProfiles.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground">
                    No profiles found
                  </TableCell>
                </TableRow>
              ) : (
                paginatedProfiles.map((profile: any) => (
                  <TableRow key={`${profile.node}-${profile.name}`}>
                    <TableCell className="font-medium">{profile.name}</TableCell>
                    <TableCell className="max-w-xs truncate">
                      {profile.description || "N/A"}
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{profile.node}</Badge>
                    </TableCell>
                    <TableCell>
                      <ClickableBadge
                        count={Object.keys(profile.devices || {}).length}
                        label="devices"
                        items={Object.entries(profile.devices || {}).map(([name, config]) => ({
                          name,
                          type: (config as any).type || "unknown",
                          config
                        }))}
                        title={`Devices in ${profile.name}`}
                        description={`${Object.keys(profile.devices || {}).length} device(s) configured`}
                      />
                    </TableCell>
                    <TableCell>
                      <ClickableBadge
                        count={profile.used_by?.length || 0}
                        label="instances"
                        items={profile.used_by || []}
                        title={`Instances using ${profile.name}`}
                        description={`${profile.used_by?.length || 0} instance(s) using this profile`}
                      />
                    </TableCell>
                    <TableCell>
                      <UsedByProjects
                        usedBy={profile.used_by || []}
                        resourceName={profile.name}
                      />
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="mt-4">
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                pageSize={pageSize}
                totalItems={filteredProfiles.length}
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
