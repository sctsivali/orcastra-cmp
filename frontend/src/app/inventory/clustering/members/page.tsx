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
  
  RefreshCw,
  Shield,
  Globe,
  AlertTriangle } from "lucide-react";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow } from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { EmptyState } from "@/components/ui/empty-state";
import { toast } from "@/hooks/use-toast";
import { useClusters, useClusterMembers, useMemberResources } from "@/hooks/use-lxd";
import { MemberResourceModal } from "@/components/member-resource-modal";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

const MEMBERS = defineTable<any>()({
  searchPlaceholder: "Search members",
  defaultSort: { field: "server_name", direction: "asc" },
  // important: `node` is reserved. On this page it selects WHICH cluster to query and feeds
  // the react-query key, so a facet of that name would hijack cluster selection. The table
  // has no Node column, so there is nothing to collide with, and this pins that.
  reservedParams: ["node"],
  columns: [
    { field: "server_name", label: "Server Name", sort: (m) => m.server_name, search: (m) => m.server_name, filter: fromSort("text") },
    { field: "url", label: "URL", sort: (m) => m.url, search: (m) => m.url, filter: fromSort("text") },
    {
      field: "roles",
      label: "Roles",
      sort: (m) => m.roles?.length ?? 0,
      // Multi-value: a member matches when any of its roles is selected.
      facet: (m) => m.roles ?? [],
      facetSpec: { primary: true, format: formatLabel, emptyLabel: "(no role)" } },
    {
      field: "status",
      label: "Status",
      sort: (m) => m.status,
      facet: (m) => m.status,
      facetSpec: { primary: true } },
    {
      field: "architecture",
      label: "Architecture",
      sort: (m) => m.architecture,
      // note: never formatLabel here. architecture arrives as x86_64 and would render as
      // "X86 64".
      facet: (m) => m.architecture,
      facetSpec: {} },
    {
      field: "failure_domain",
      label: "Failure Domain",
      sort: (m) => m.failure_domain || "default",
      facet: (m) => m.failure_domain || "default",
      facetSpec: {} },
  ] });

function ClusterMembersContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const nodeParam = searchParams.get("node");
  
  const [node, setNode] = React.useState<string>(nodeParam || "");
  const [selectedMember, setSelectedMember] = React.useState<string | null>(null);
  const [isModalOpen, setIsModalOpen] = React.useState(false);
  
  const { data: clustersData } = useClusters();
  const clusters = React.useMemo(() => {
    // important: the status filter that used to sit beside the type filter is gone. An offline
    // MicroCloud cluster simply vanished from this picker, so an operator whose cluster had
    // gone down was left with no row, no status and nothing to explain the absence. It is
    // listed and marked instead, which is the rule every other cluster picker follows.
    const rows = (clustersData?.clusters || []).filter(
      (c: any) => c.cluster_type?.toLowerCase() === "microcloud"
    );
    // Reachable clusters lead, so the list does not open on a row that cannot be read.
    return rows.sort(
      (a: any, b: any) =>
        clusterStatusMeta(a.status).toneRank - clusterStatusMeta(b.status).toneRank ||
        (a.display_name || a.cluster_id).localeCompare(b.display_name || b.cluster_id)
    );
  }, [clustersData]);
  
  const { data: membersData, isLoading, refreshNow, dataUpdatedAt, error } = useClusterMembers(node);
  const members = React.useMemo(() => membersData?.members || [], [membersData]);
  const isClustered = membersData?.is_clustered || false;
  const nodeName = membersData?.node || "";
  
  // note: gated on the modal. Without it this kept polling a live per-member read in the
  // background for a panel nobody had open.
  const { data: memberResources, isLoading: isLoadingResources } = useMemberResources(
    node,
    selectedMember || "",
    { enabled: isModalOpen },
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
        variant: "destructive" });
    }
  }, [error]);

  React.useEffect(() => {
    if (nodeParam && clusters.some((c: any) => c.cluster_id === nodeParam)) {
      setNode(nodeParam);
    } else if (clusters.length > 0 && !nodeParam) {
      // important: prefer a cluster that will answer. Now that unreachable clusters are listed
      // rather than filtered out, defaulting to the first row would open this page on an error
      // toast on every visit whenever the alphabetically first cluster happened to be down.
      const first =
        clusters.find((c: any) => clusterStatusMeta(c.status).reachable) ?? clusters[0];
      const firstNode = first.cluster_id;
      setNode(firstNode);
      router.replace(`/inventory/clustering/members?node=${firstNode}`);
    }
  }, [nodeParam, clusters, router]);

  const handleNodeChange = (value: string) => {
    setNode(value);
    router.replace(`/inventory/clustering/members?node=${value}`);
  };

  // Sorting
  const filters = useTableFilters(MEMBERS, members as any[]);
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    MEMBERS.sortAccessors,
    MEMBERS.defaultSort.field,
    MEMBERS.defaultSort.direction,
  );
  const sortedMembers = React.useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

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
            options={clusters.map((cluster: any) => {
              const meta = clusterStatusMeta(cluster.status);
              return {
                value: cluster.cluster_id,
                label: cluster.display_name || cluster.cluster_id,
                badge: formatHypervisorType(cluster.cluster_type),
                status: {
                  label: meta.label,
                  dotClassName: meta.dotClassName,
                  tone: meta.tone,
                },
              };
            })}
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
        <CardHeader className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Member Overview</CardTitle>
              <CardDescription>
                {error
                  ? "Cluster did not answer"
                  : isClustered
                    ? "Members in this cluster"
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
          {isClustered && <TableToolbar filters={filters} noun="members" />}
        </CardHeader>
        <CardContent>
          {/* important: an unreachable cluster answers 503, which leaves membersData
              undefined and is_clustered false. Rendering that as "Not a clustered node"
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
              title="Not a clustered node"
              description="This node is not part of a hypervisor cluster"
            />
          ) : members.length === 0 ? (
            <EmptyState
              icon={Server}
              title="No members found"
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
                {sortedMembers.length === 0 ? (
                  <TableEmptyRow
                    colSpan={6}
                    icon={Server}
                    title="No members found"
                    description={
                      filters.activeFilterCount > 0
                        ? "No member matches the current filters."
                        : undefined
                    }
                  />
                ) : (
                  sortedMembers.map((member: any) => (
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
                        variant={member.status === "Online" ? "positive" : "negative"}
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
                ))
              )}
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
