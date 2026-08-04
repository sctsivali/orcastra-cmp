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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Search, Plus, AlertTriangle } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useNetworks } from "@/hooks/use-lxd";
import { Button } from "@/components/ui/button";
import { useRouter } from "next/navigation";
import { usePermissions } from "@/hooks/use-permissions";
import { Role } from "@/types/rbac";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

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

  const router = useRouter();
  const { hasRole } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  const canCreateNetwork = hasRole(Role.PARTNER);
  // Nodes we could not reach: their networks are absent from the list, so warn
  // rather than silently showing "0 networks" (indistinguishable from empty).
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];

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
      node: (i: any) => i.node,
      ipv4: (i: any) => i.config?.["ipv4.address"],
      ipv6: (i: any) => i.config?.["ipv6.address"],
      status: (i: any) => i.status,
      managed: (i: any) => i.managed,
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
    const nodeSet = new Set(networks.map((n: any) => n.node).filter((n: any): n is string => Boolean(n)));
    return Array.from(nodeSet).sort();
  }, [networks]);

  const networkTypes = useMemo(() => {
    const typeSet = new Set(networks.map((n: any) => n.type));
    return Array.from(typeSet).sort();
  }, [networks]);

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

    return filtered;
  }, [networks, searchQuery, filterNode, filterType]);

  const sortedNetworks = useMemo(() => sortItems(filteredNetworks), [filteredNetworks, sortItems]);

  const totalPages = Math.ceil(sortedNetworks.length / pageSize);
  const paginatedNetworks = sortedNetworks.slice(
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
            Networks
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor network configurations
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RefreshButton
            disabled={isRefetching}
            isRefreshing={isRefetching}
            label="Refresh networks"
            onClick={() => refetch()}
          />
          {/* Only Partner+ can create; the create page enforces this too. */}
          {canCreateNetwork && (
            <Button
              onClick={() => router.push("/inventory/networks/create")}
              disabled={isReadOnly}
              title={isReadOnly ? "Action disabled in read-only mode" : "Create a new network"}
            >
              <Plus className="h-4 w-4 mr-2" />
              Create network
            </Button>
          )}
        </div>
      </div>

      {unreachableNodes.length > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
          <div>
            <span className="font-medium">
              Could not reach {unreachableNodes.length} node
              {unreachableNodes.length === 1 ? "" : "s"}:
            </span>{" "}
            {unreachableNodes.join(", ")}. Their networks are not shown, so this
            list may be incomplete.
          </div>
        </div>
      )}

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
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && (
                  <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                )}
                <SortableTableHead label="IPv4 Address" field="ipv4" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="IPv6 Address" field="ipv6" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Managed" field="managed" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Projects" field="projects" currentField={sortField} direction={sortDirection} onSort={handleSort} />
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
                  const rawIpv4 = network.config?.["ipv4.address"];
                  const ipv4 = (!rawIpv4 || rawIpv4 === "none") ? "N/A" : rawIpv4;
                  const rawIpv6 = network.config?.["ipv6.address"];
                  const ipv6 = (!rawIpv6 || rawIpv6 === "none") ? "N/A" : rawIpv6;
                  
                  return (
                    <TableRow key={`${network.node}-${network.name}-${idx}`}>
                      <TableCell
                        className="font-medium cursor-pointer hover:text-primary hover:underline"
                        onClick={() =>
                          router.push(
                            `/inventory/networks/${encodeURIComponent(network.name)}?host=${encodeURIComponent(network.node)}`,
                          )
                        }
                      >
                        {network.name}
                      </TableCell>
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
                          {network.status || "N/A"}
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
