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
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatLabel } from "@/lib/utils";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Search } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useStorageVolumes } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

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
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useStorageVolumes();
  const volumes = (data as any)?.volumes || [];
  
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [filterType, setFilterType] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const { toast } = useToast();

  const sortAccessors = useMemo(
    () => ({
      name: (i: any) => i.name,
      type: (i: any) => i.type,
      content_type: (i: any) => i.content_type,
      node: (i: any) => i.node,
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
    const nodeSet = new Set(volumes.map((v: any) => v.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [volumes]);

  const volumeTypes = useMemo(() => {
    const typeSet = new Set(volumes.map((v: any) => v.type));
    return Array.from(typeSet).sort();
  }, [volumes]);

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

    return filtered;
  }, [volumes, searchQuery, filterNode, filterType]);

  const sortedVolumes = useMemo(() => sortItems(filteredVolumes), [filteredVolumes, sortItems]);

  const totalPages = Math.ceil(sortedVolumes.length / pageSize);
  const paginatedVolumes = sortedVolumes.slice(
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
            Storage Volumes
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor storage volumes
          </p>
        </div>
        <RefreshButton
          label="Refresh storage volumes"
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
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Content Type" field="content_type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && (
                  <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                )}
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
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
                      {formatLabel(volume.type)}
                    </TableCell>
                    <TableCell>
                      {formatLabel(volume.content_type)}
                    </TableCell>
                    {nodes.length > 0 && (
                      <TableCell>
                        {volume.node}
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
