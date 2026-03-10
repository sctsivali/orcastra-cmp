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
import { RotateCw, Search, ArrowUp, ArrowDown } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useStorageVolumes } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";

type SortField = "name" | "type" | "content_type" | "used_by";
type SortDirection = "asc" | "desc";

interface VolumeInfo {
  name: string;
  type: string;
  content_type: string;
  description: string;
  config: Record<string, string>;
  used_by: string[];
  node?: string;
}

export default function StorageVolumesPage() {
  const { data, isLoading, refetch, isRefetching } = useStorageVolumes();
  const volumes = (data as any)?.volumes || [];
  
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [filterType, setFilterType] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [sortField, setSortField] = useState<SortField>("name");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const { toast } = useToast();

  const nodes = useMemo(() => {
    const nodeSet = new Set(volumes.map((v: any) => v.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [volumes]);

  const volumeTypes = useMemo(() => {
    const typeSet = new Set(volumes.map((v: any) => v.type));
    return Array.from(typeSet).sort();
  }, [volumes]);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("asc");
    }
    setCurrentPage(1);
  };

  const filteredVolumes = useMemo(() => {
    let filtered = volumes;

    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (volume: any) =>
          volume.name.toLowerCase().includes(query) ||
          (volume.description && volume.description.toLowerCase().includes(query))
      );
    }

    if (filterNode !== "all") {
      filtered = filtered.filter((volume: any) => volume.node === filterNode);
    }

    if (filterType !== "all") {
      filtered = filtered.filter((volume: any) => volume.type === filterType);
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
        case "content_type":
          aVal = a.content_type || "";
          bVal = b.content_type || "";
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
  }, [volumes, searchQuery, filterNode, filterType, sortField, sortDirection]);

  const totalPages = Math.ceil(filteredVolumes.length / pageSize);
  const paginatedVolumes = filteredVolumes.slice(
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
            Storage Volumes
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor storage volumes
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
                placeholder="Search storage volumes..."
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
                {volumeTypes.map((type: any) => (
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
            All Storage Volumes ({filteredVolumes.length})
            {(searchQuery || filterNode !== "all" || filterType !== "all") ? ` (filtered from ${volumes.length})` : ""}
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
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("content_type")}
                >
                  Content Type <SortIcon field="content_type" />
                </TableHead>
                {nodes.length > 0 && <TableHead>Node</TableHead>}
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
              {paginatedVolumes.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground">
                    No storage volumes found
                  </TableCell>
                </TableRow>
              ) : (
                paginatedVolumes.map((volume: any, idx: number) => (
                  <TableRow key={`${volume.node}-${volume.name}-${idx}`}>
                    <TableCell className="font-medium">{volume.name}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{volume.type}</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">{volume.content_type}</Badge>
                    </TableCell>
                    {nodes.length > 0 && (
                      <TableCell>
                        {volume.node && <Badge variant="secondary">{volume.node}</Badge>}
                      </TableCell>
                    )}
                    <TableCell className="text-center">
                      <ClickableBadge
                        count={volume.used_by?.length || 0}
                        label="resources"
                        items={volume.used_by || []}
                        title={`Resources using ${volume.name}`}
                        description={`${volume.used_by?.length || 0} resource(s) using this volume`}
                      />
                    </TableCell>
                    <TableCell>
                      <UsedByProjects
                        usedBy={volume.used_by || []}
                        resourceName={volume.name}
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
                totalItems={filteredVolumes.length}
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
