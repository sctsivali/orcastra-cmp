"use client";

import * as React from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { Suspense } from "react";
import { formatHypervisorType } from "@/lib/utils";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { 
  Server, 
  CheckCircle, 
  XCircle, 
  Database,
  RefreshCw,
  Shield,
  Globe
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { toast } from "@/hooks/use-toast";
import { ClusterMember } from "@/types/api";
import { useClusters, useClusterMembers, useMemberResources } from "@/hooks/use-lxd";
import { MemberResourceModal } from "@/components/member-resource-modal";
import { usePermissions } from "@/hooks/use-permissions";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

function ClusterMembersContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const nodeParam = searchParams.get("node");
  
  const [node, setNode] = React.useState<string>(nodeParam || "");
  const [selectedMember, setSelectedMember] = React.useState<string | null>(null);
  const [isModalOpen, setIsModalOpen] = React.useState(false);
  
  const { data: clustersData } = useClusters();
  const clusters = React.useMemo(() => {
    return (clustersData?.clusters || []).filter(
      (c: any) => c.cluster_type?.toLowerCase() === "microcloud" && c.status === "online"
    );
  }, [clustersData]);
  
  const { data: membersData, isLoading, refetch, isRefetching, error } = useClusterMembers(node);
  const members = React.useMemo(() => membersData?.members || [], [membersData]);
  const isClustered = membersData?.is_clustered || false;
  const nodeName = membersData?.node || "";
  
  const { data: memberResources, isLoading: isLoadingResources } = useMemberResources(
    node,
    selectedMember || ""
  );
  
  const handleMemberClick = (memberName: string) => {
    setSelectedMember(memberName);
    setIsModalOpen(true);
  };
  
  const handleCloseModal = () => {
    setIsModalOpen(false);
    setSelectedMember(null);
  };
  
  // Show error toast if fetch fails
  React.useEffect(() => {
    if (error) {
      toast({
        title: "Failed to fetch cluster members",
        description: error instanceof Error ? error.message : "Unknown error occurred",
        variant: "destructive",
      });
    }
  }, [error]);

  React.useEffect(() => {
    if (nodeParam && clusters.some((c: any) => c.cluster_id === nodeParam)) {
      setNode(nodeParam);
    } else if (clusters.length > 0 && !nodeParam) {
      const firstNode = clusters[0].cluster_id;
      setNode(firstNode);
      router.replace(`/inventory/clustering/members?node=${firstNode}`);
    }
  }, [nodeParam, clusters, router]);

  const handleNodeChange = (value: string) => {
    setNode(value);
    router.replace(`/inventory/clustering/members?node=${value}`);
  };

  // Sorting
  const sortAccessors = React.useMemo(() => ({
    server_name: (m: any) => m.server_name,
    url: (m: any) => m.url,
    roles: (m: any) => m.roles?.length ?? 0,
    status: (m: any) => m.status,
    architecture: (m: any) => m.architecture,
    failure_domain: (m: any) => m.failure_domain || "default",
  }), []);
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    sortAccessors,
    "server_name",
    "asc",
  );
  const sortedMembers = React.useMemo(() => sortItems(members), [members, sortItems]);

  // Full-page loading state
  if (isLoading && node) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="container py-8">
      <div className="mb-6">
        <div className="flex items-center gap-4 mb-4">
          <label className="text-sm font-medium">Select MicroCloud Node:</label>
          <Select value={node} onValueChange={handleNodeChange}>
            <SelectTrigger className="w-[300px]">
              <SelectValue placeholder="Select a MicroCloud node" />
            </SelectTrigger>
            <SelectContent>
              {clusters.length === 0 ? (
                <SelectItem value="none" disabled>
                  No MicroCloud clusters available
                </SelectItem>
              ) : (
                clusters.map((cluster: any) => (
                  <SelectItem key={cluster.cluster_id} value={cluster.cluster_id}>
                    {cluster.display_name || cluster.cluster_id} ({formatHypervisorType(cluster.cluster_type)})
                  </SelectItem>
                ))
              )}
            </SelectContent>
          </Select>
        </div>
        <div>
          <h1 className="text-3xl font-bold">Cluster Members</h1>
          <p className="text-muted-foreground mt-1">
            {node ? (
              <>View all members in the {nodeName || node} cluster</>
            ) : (
              <>Please select a MicroCloud node to view its cluster members</>
            )}
          </p>
        </div>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Member Overview</CardTitle>
              <CardDescription>
                {isClustered ? (
                  <>
                    {members.length} {members.length === 1 ? "member" : "members"} in cluster
                  </>
                ) : (
                  "Node is not part of a cluster"
                )}
              </CardDescription>
            </div>
            <RefreshButton
              disabled={isRefetching}
              isRefreshing={isRefetching}
              label="Refresh cluster members"
              onClick={() => refetch()}
            />
          </div>
        </CardHeader>
        <CardContent>
          {!isClustered ? (
            <div className="text-center py-12">
              <Server className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
              <h3 className="text-lg font-semibold mb-2">Not a Clustered Node</h3>
              <p className="text-muted-foreground mb-4">
                This node is not part of a hypervisor cluster
              </p>
            </div>
          ) : members.length === 0 ? (
            <div className="text-center py-12">
              <Server className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
              <h3 className="text-lg font-semibold mb-2">No Members Found</h3>
              <p className="text-muted-foreground">
                Could not retrieve cluster member information
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead label="Server Name" field="server_name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="URL" field="url" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Roles" field="roles" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Architecture" field="architecture" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Failure Domain" field="failure_domain" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedMembers.map((member: any) => (
                  <TableRow key={member.server_name}>
                    <TableCell className="font-medium">
                      <button
                        onClick={() => handleMemberClick(member.server_name)}
                        className="flex items-center gap-2 hover:text-primary transition-colors cursor-pointer"
                      >
                        <Server className="h-4 w-4 text-muted-foreground" />
                        <span className="underline decoration-dotted">{member.server_name}</span>
                      </button>
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1 text-sm">
                        <Globe className="h-3 w-3 text-muted-foreground" />
                        {member.url}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="flex gap-1 flex-wrap">
                        {member.roles && member.roles.length > 0 ? (
                          member.roles.map((role: any) => (
                            <Badge 
                              key={role} 
                              variant={role === "database-leader" ? "default" : "secondary"}
                              className="gap-1"
                            >
                              {role === "database-leader" || role === "database" ? (
                                <Database className="h-3 w-3" />
                              ) : null}
                              {role}
                            </Badge>
                          ))
                        ) : (
                          <span className="text-sm text-muted-foreground">—</span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge 
                        variant={member.status === "Online" ? "default" : "destructive"}
                        className="gap-1"
                      >
                        {member.status === "Online" ? (
                          <CheckCircle className="h-3 w-3" />
                        ) : (
                          <XCircle className="h-3 w-3" />
                        )}
                        {member.status}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <span className="text-sm">
                        {member.architecture || "N/A"}
                      </span>
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1">
                        <Shield className="h-3 w-3 text-muted-foreground" />
                        <span className="text-sm">
                          {member.failure_domain || "default"}
                        </span>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <MemberResourceModal
        isOpen={isModalOpen}
        onClose={handleCloseModal}
        memberName={selectedMember}
        nodeName={node}
        resources={memberResources || null}
        isLoading={isLoadingResources}
      />
    </div>
  );
}

export default function ClusterMembersPage() {
  const router = useRouter();
  const { isPartner } = usePermissions();

  // redirect if not Partner+
  React.useEffect(() => {
    if (!isPartner) {
      router.replace("/");
    }
  }, [isPartner, router]);

  if (!isPartner) return null;

  return (
    <Suspense fallback={
      <div className="container py-8">
        <Card>
          <CardContent className="pt-6">
            <div className="flex items-center justify-center py-12">
              <RefreshCw className="h-8 w-8 animate-spin text-muted-foreground" />
            </div>
          </CardContent>
        </Card>
      </div>
    }>
      <ClusterMembersContent />
    </Suspense>
  );
}
