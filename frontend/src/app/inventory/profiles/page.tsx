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
import { TableEmptyValue } from "@/components/ui/table-text";
import { 
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Search } from "lucide-react";
import { ProfileInfo } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { useProfiles } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

export default function ProfilesPage() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useProfiles();
  const profiles = (data as any)?.profiles || [];
  
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
      devices: (i: any) => Object.keys(i.devices || {}).length,
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

  // Get unique nodes
  const nodes = useMemo(() => {
    const nodeSet = new Set(profiles.map((p: any) => p.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [profiles]);

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

    return filtered;
  }, [profiles, searchQuery, filterNode]);

  const sortedProfiles = useMemo(() => sortItems(filteredProfiles), [filteredProfiles, sortItems]);

  // Pagination
  const totalPages = Math.ceil(sortedProfiles.length / pageSize);
  const paginatedProfiles = sortedProfiles.slice(
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
            Profiles
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor configuration profiles
          </p>
        </div>
        <RefreshButton
          label="Refresh profiles"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
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
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Description" field="description" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Devices" field="devices" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
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
                      {profile.description || <TableEmptyValue label="No description" />}
                    </TableCell>
                    <TableCell>
                      {profile.node}
                    </TableCell>
                    <TableCell>
                      <ClickableBadge
                        count={Object.keys(profile.devices || {}).length}
                        label="devices"
                        items={Object.entries(profile.devices || {}).map(([name, config]) => ({
                          name,
                          type: (config as any).type || "N/A",
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
