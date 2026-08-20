"use client";

import * as React from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { Suspense } from "react";
import { formatHypervisorType, formatLabel} from "@/lib/utils";
import { LoadingLottiePage } from "@/components/loading-lottie";
import {
  Server,
  CheckCircle,
  XCircle,
  Database,
  RefreshCw,
  Shield,
  Globe,
  AlertTriangle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { SearchableSelect } from "@/components/ui/searchable-select";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyValue } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { EmptyState } from "@/components/ui/empty-state";
import { toast } from "@/hooks/use-toast";
import { ClusterMember } from "@/types/api";
import { useClusters, useClusterMembers, useMemberResources } from "@/hooks/use-lxd";
import { MemberResourceModal } from "@/components/member-resource-modal";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
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
  
  const { data: membersData, isLoading, refreshNow, dataUpdatedAt, error } = useClusterMembers(node);
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
          <SearchableSelect
            value={node}
            onValueChange={handleNodeChange}
            className="w-[300px]"
            placeholder="Select a MicroCloud node"
            searchPlaceholder="Search clusters..."
            emptyText="No MicroCloud clusters available"
            options={clusters.map((cluster: any) => ({
              value: cluster.cluster_id,
              label: cluster.display_name || cluster.cluster_id,
              badge: formatHypervisorType(cluster.cluster_type),
            }))}
          />
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
                {error
                  ? "Cluster did not answer"
                  : isClustered
                    ? `${members.length} ${members.length === 1 ? "member" : "members"} in cluster`
                    : "Node is not part of a cluster"}
              </CardDescription>
            </div>
            {/* important: no node means nothing to refresh. React Query's refetch() runs the
                query function even when the query is disabled, so this control sent
                `?node=` and the backend went on to ask Vault for a cluster named "".
                A control that cannot act is also a promise the page cannot keep. */}
            {node && (
              <RefreshButton
                label="Refresh cluster members"
                lastUpdatedAt={dataUpdatedAt}
                onRefresh={refreshNow}
              />
            )}
          </div>
        </CardHeader>
        <CardContent>
          {/* important: an unreachable cluster answers 503, which leaves membersData
              undefined and is_clustered false. Rendering that as "Not a Clustered Node"
              told the operator their cluster is not a cluster, presenting an outage as a
              configuration fact. Reachability is checked before clustering. */}
          {error ? (
            <EmptyState
              icon={AlertTriangle}
              title="Could not reach this cluster"
              description={`${node} did not answer, so its members cannot be listed. This says nothing about whether the node is clustered.`}
            />
          ) : !isClustered ? (
            <EmptyState
              icon={Server}
              title="Not a Clustered Node"
              description="This node is not part of a hypervisor cluster"
            />
          ) : members.length === 0 ? (
            <EmptyState
              icon={Server}
              title="No Members Found"
              description="Could not retrieve cluster member information"
            />
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
                      <span className="flex items-center gap-2">
                        <Server className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                        <TableLink onClick={() => handleMemberClick(member.server_name)}>
                          {member.server_name}
                        </TableLink>
                      </span>
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
                          member.roles.map((role: string) => formatLabel(role)).join(", ")
                        ) : (
                          <TableEmptyValue label="No roles" />
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
                        {member.architecture || <TableEmptyValue label="No architecture" />}
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
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  if (!isResolved) return <LoadingLottiePage />;
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
