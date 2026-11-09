"use client";

import * as React from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { replaceUrl } from "@/lib/url-state";
import { Suspense } from "react";
import { formatHypervisorType, formatLabel, plural } from "@/lib/utils";
import { LoadingLottiePage } from "@/components/loading-lottie";
import {
  Server,
  CheckCircle,
  XCircle,
  
  RefreshCw,
  Shield,
  ShieldAlert,
  Globe,
  AlertTriangle } from "lucide-react";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { clusterStatusMeta, clusterSublabel } from "@/lib/cluster-status";
import { memberStatusDisplay } from "@/lib/instance-status";
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
import { EmptyClustersIllustration } from "@/components/illustrations/scenes/onboarding";
import { Button } from "@/components/ui/button";
import { IntentLink } from "@/components/navigation/route-intent";
import type { ClusterInfo } from "@/types/api";
import { toast } from "@/hooks/use-toast";
import { useClusters, useClusterMembers } from "@/hooks/use-lxd";
import { MemberResourceModal } from "@/components/member-resources/member-resource-modal";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { inventoryLinks } from "@/lib/inventory-links";

const MEMBERS = defineTable<any>()({
  searchPlaceholder: "Search members",
  defaultSort: { field: "server_name", direction: "asc" },
  // important: `node` is reserved. On this page it selects WHICH cluster to query and feeds
  // the react-query key, so a facet of that name would hijack cluster selection. The table
  // has no Node column, so there is nothing to collide with, and this pins that.
  reservedParams: ["node"],
  columns: [
    { field: "server_name", label: "Cluster Member", sort: (m) => m.server_name, search: (m) => m.server_name, filter: fromSort("text") },
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
      facetSpec: { primary: true, format: (value) => memberStatusDisplay(value).label } },
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
  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState(TABLE_PAGE_SIZE);
  // note: declared here rather than beside the filters, because the cluster picker resets the
  // page as well and its handler is defined further up than they are.
  const resetPage = React.useCallback(() => setPage(1), []);
  
  const { data: clustersData } = useClusters();
  const clusters = React.useMemo<ClusterInfo[]>(() => {
    // important: the status filter that used to sit beside the type filter is gone. An offline
    // clustered host simply vanished from this picker, so an operator whose cluster had
    // gone down was left with no row, no status and nothing to explain the absence. It is
    // listed and marked instead, which is the rule every other cluster picker follows.
    //
    // important: filtered on `clustered`, not on the MicroCloud label. Cluster members are a
    // property of clustering, and the clusters page offers the Members button on the same
    // rule - filtering by label here left a plain LXD cluster with a button leading to a
    // picker that did not list it.
    //
    // important: `=== true`, where this used to be `!== false`. Keeping the undetermined case
    // was deliberate and it was wrong: `clustered` reads null for a host that has never
    // completed its trust handshake, so the picker offered clusters that answer 503 the moment
    // they are chosen, while the clusters page only offers its Members button for a host that
    // is both clustered and online. The backend now persists the clustering state whenever a
    // host proves it on a trusted read, so `true` survives an outage and this filter keeps a
    // clustered host that is merely down. See documentation/clusters/hypervisor-type-and-clustering.md.
    const rows = (clustersData?.clusters ?? []).filter(
      // important: never drop the cluster the URL named, even if it fails the rule.
      // SearchableSelect falls back to its placeholder when `value` matches no option, so a
      // filtered-out deep link leaves the trigger reading "Select a clustered node" while the
      // page underneath queries that very cluster. Two surfaces disagreeing is worse than one
      // extra row, and a registration that predates the persistence change reads null until
      // someone presses Check Authentication once.
      (c: ClusterInfo) => c.clustered === true || c.cluster_id === node,
    );
    // Reachable clusters lead, so the list does not open on a row that cannot be read.
    return rows.sort(
      (a: ClusterInfo, b: ClusterInfo) =>
        clusterStatusMeta(a.status).toneRank - clusterStatusMeta(b.status).toneRank ||
        (a.display_name || a.cluster_id).localeCompare(b.display_name || b.cluster_id)
    );
  }, [clustersData, node]);
  
  const { data: membersData, isLoading, refreshNow, dataUpdatedAt, errorUpdatedAt, error } = useClusterMembers(node);
  // important: React Query puts a query with no data back to `pending` (error cleared) on every
  // fetch, so a retry of an unreachable cluster, by the refresh button or the poll, read as a
  // first load: the page swapped to the full loader, the refresh control unmounted mid-click,
  // and the description flipped to "Clustering is not enabled". A query that has answered
  // once, either way, keeps its page; one that last failed keeps saying so while it retries.
  const hasAnswered = dataUpdatedAt > 0 || errorUpdatedAt > 0;
  const unreachable = !!error || (!membersData && errorUpdatedAt > 0);
  const members = React.useMemo(() => membersData?.members || [], [membersData]);
  const isClustered = membersData?.is_clustered || false;
  const nodeName = membersData?.node || "";
  
  const handleMemberClick = (memberName: string) => {
    setSelectedMember(memberName);
    setIsModalOpen(true);
  };

  // important: the member is kept, not cleared. Clearing it moved the query key while Radix
  // was still animating the dialog out, so the body repainted as an empty panel on the way
  // closed. The query is still stopped, because the modal gates it on isOpen, and reopening
  // still refetches: react-query treats a query whose enabled was false as needing a fetch.
  const handleCloseModal = () => {
    setIsModalOpen(false);
  };
  
  // Show error toast if fetch fails
  React.useEffect(() => {
    if (error) {
      toast({
        title: "Could not fetch cluster members",
        description: error instanceof Error ? error.message : "Unknown error occurred.",
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
      router.replace(inventoryLinks.members(firstNode), { scroll: false });
    }
  }, [nodeParam, clusters, router]);

  const handleNodeChange = (value: string) => {
    setNode(value);
    // important: members are scoped to the cluster, so a page number carried over from the
    // previous one addresses nothing. Clamping alone would land page 3 of a long cluster on
    // the last page of a short one instead of at the top of it.
    resetPage();
    replaceUrl(inventoryLinks.members(value));
  };

  // Sorting
  const filters = useTableFilters(MEMBERS, members as any[], {
    onFilterChange: resetPage,
  });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    MEMBERS.sortAccessors,
    MEMBERS.defaultSort.field,
    MEMBERS.defaultSort.direction,
    resetPage,
  );
  const sortedMembers = React.useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  // note: clamped on read rather than corrected in an effect. A member leaving the cluster
  // shrinks this list while the page is open, so the last page can disappear underneath the
  // operator, and an effect would only fire after a page that no longer exists had painted.
  const totalPages = Math.max(1, Math.ceil(sortedMembers.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedMembers = sortedMembers.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  // Full-page loading state
  if (isLoading && node && !hasAnswered) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="container py-8">
      <div className="mb-6">
        <div className="flex items-center gap-4 mb-4">
          <label htmlFor="members-node" className="text-sm font-medium">
            Select Cluster:
          </label>
          <SearchableSelect
            id="members-node"
            value={node}
            onValueChange={handleNodeChange}
            className="w-[300px]"
            placeholder="Select a cluster"
            searchPlaceholder="Search clusters…"
            // important: not disabled when the list is empty. SearchableSelect carries
            // `emptyText` for exactly this, every other picker in the app stays operable, and
            // node-picker-status-coverage.spec.ts opens this control before deciding whether
            // there is anything to assert. A disabled control turns "nothing to choose" into a
            // dead end the operator cannot confirm by looking.
            emptyText="No cluster with members is available"
            options={clusters.map((cluster: any) => {
              const meta = clusterStatusMeta(cluster.status);
              return {
                value: cluster.cluster_id,
                label: cluster.display_name || cluster.cluster_id,
                sublabel: clusterSublabel(cluster.cluster_id, cluster.display_name, cluster.endpoint),
                badge: formatHypervisorType(cluster.cluster_type),
                status: {
                  label: meta.label,
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
              <>Please select a cluster to view its cluster members</>
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
                {!node
                  ? "No cluster selected"
                  : unreachable
                    ? "Cluster did not answer"
                    : isClustered
                      ? "Members in this cluster"
                      : "Standalone server, no members"}
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
          {/* important: selection is checked before clustering. With no node chosen this fell
              through to "Not a clustered node", which told an operator that a cluster they had
              not picked yet is not a cluster. Tightening the filter made that state reachable
              on a fleet where nothing has proven itself clustered. */}
          {!node ? (
            clusters.length === 0 ? (
              <EmptyState
                icon={ShieldAlert}
                illustration={EmptyClustersIllustration}
                title="No clusters with members"
                description="Only a cluster running in clustered mode has members. A standalone server has none, and a cluster still waiting on its trust handshake has not reported its mode yet."
                action={
                  <Button asChild variant="outline" size="sm">
                    <IntentLink href="/inventory/clusters">Go to clusters</IntentLink>
                  </Button>
                }
              />
            ) : (
              <EmptyState
                icon={Server}
                title="No cluster selected"
                description="Pick a cluster above to see its members."
              />
            )
          ) : unreachable ? (
            <EmptyState
              icon={AlertTriangle}
              title="Could not reach this cluster"
              description={`${node} did not answer, so its members cannot be listed. This says nothing about whether clustering is enabled.`}
            />
          ) : !isClustered ? (
            <EmptyState
              icon={Server}
              title="Standalone server"
              description="This cluster runs on one server, so it has no members to list."
            />
          ) : members.length === 0 ? (
            <EmptyState
              icon={Server}
              title="No members found"
              description="Could not retrieve cluster member information"
            />
          ) : (
            <>
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="Cluster Member" field="server_name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="URL" field="url" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Roles" field="roles" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Architecture" field="architecture" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Failure Domain" field="failure_domain" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {sortedMembers.length === 0 ? (
                    // important: this branch is only reachable with members present, because the
                    // outer condition already handled the empty cluster. Reaching it means the
                    // filters hid every member, which is a different fact from an empty cluster
                    // and must not borrow its wording.
                    <TableEmptyRow
                      colSpan={6}
                      icon={Server}
                      title="No member matches the current filters"
                      description={`${plural(filters.activeFilterCount, "filter")} active on ${plural(members.length, "member")} in this cluster.`}
                      action={
                        <Button variant="outline" size="sm" onClick={() => filters.clearAll()}>
                          Clear filters
                        </Button>
                      }
                    />
                  ) : (
                    pagedMembers.map((member: any) => {
                      const status = memberStatusDisplay(member.status);
                      return (
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
                        <Badge variant={status.tone} className="gap-1">
                          {status.online ? (
                            <CheckCircle className="h-3 w-3" />
                          ) : (
                            <XCircle className="h-3 w-3" />
                          )}
                          {status.label}
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
                      );
                    })
                )}
                </TableBody>
              </Table>

              {/* Absent rather than disabled when every member fits on one page. */}
              {totalPages > 1 && (
                <div className="mt-4">
                  <DataTablePagination
                    currentPage={currentPage}
                    totalPages={totalPages}
                    pageSize={pageSize}
                    pageSizeOptions={[...TABLE_PAGE_SIZES]}
                    totalItems={sortedMembers.length}
                    onPageChange={setPage}
                    onPageSizeChange={(size) => {
                      setPageSize(size);
                      setPage(1);
                    }}
                  />
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <MemberResourceModal
        isOpen={isModalOpen}
        onClose={handleCloseModal}
        memberName={selectedMember}
        nodeName={node}
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
