"use client";

import { Suspense, useCallback, useState, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, AlertCircle, CheckCircle, Download, Key, ListChecks, Plus, Shield, Trash2 } from "lucide-react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { BackLink } from "@/components/ui/back-link";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Dialog, DialogTrigger } from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { RowActions, type RowAction } from "@/components/ui/row-actions";
import { TableEmptyValue } from "@/components/ui/table-text";
import { EmptyState } from "@/components/ui/empty-state";
import { useToast } from "@/hooks/use-toast";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { useAllProjects } from "@/hooks/use-lxd";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { api } from "@/lib/api";
import { myKeysActivityAPI } from "@/lib/my-keys-activity-api";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { KeyInfo, KeyCreateRequest } from "@/types/api";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { IntentLink } from "@/components/navigation/route-intent";
import { formatDateTime, formatLabel, formatDate } from "@/lib/utils";
import { useTableSort } from "@/hooks/use-table-sort";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { useRowFocus } from "@/hooks/use-row-focus";
import { requestRowFocus } from "@/lib/row-focus";
import { RowFocusNotice } from "@/components/ui/row-focus-notice";
import { NodeLink } from "@/components/ui/node-link";
import { CreateKeyDialog } from "./components/create-key-dialog";
import { KeyActivityDialog } from "./components/key-activity-dialog";
import { DeleteKeyDialog, KeyStats } from "./components/key-stats-and-delete";


// important: byte-identical to SHARED_CLUSTER_DETAIL in backend/app/core/whole_cluster_access.py.
const SHARED_CLUSTER_REFUSAL = "Not permitted on a cluster shared with another organization";

/** A key with when it last changed something on its cluster (GET /my-keys/last-used). */
type KeyRow = KeyInfo & { last_used_at: string | null };

const MY_KEYS = defineTable<KeyRow>()({
  searchPlaceholder: "Search by name, project, or cluster",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "name", label: "Name", sort: (k) => k.name, search: (k) => k.name, filter: fromSort("text") },
    {
      field: "node",
      label: "Cluster",
      sort: (k) => k.node,
      search: (k) => k.node,
      facet: (k) => k.node,
      facetSpec: { role: "node", primary: true } },
    {
      field: "project",
      label: "Project",
      sort: (k) => k.project,
      search: (k) => k.project,
      facet: (k) => k.project,
      facetSpec: { role: "project", primary: true } },
    {
      field: "auth_level",
      label: "Access Level",
      sort: (k) => k.auth_level,
      facet: (k) => k.auth_level,
      facetSpec: { format: formatLabel } },
    { field: "created", label: "Created", sort: (k) => (k.created_at ? new Date(k.created_at).getTime() : null), filter: fromSort("date", { horizon: "past" }) },
    { field: "expires", label: "Expires", sort: (k) => (k.expires_at ? new Date(k.expires_at).getTime() : null), filter: fromSort("date", { horizon: "future", emptyLabel: "Never" }) },
    // The last change LXD reported under the key's certificate; a read (lxc list) leaves none.
    { field: "last_used", label: "Last Used", sort: (k) => (k.last_used_at ? new Date(k.last_used_at).getTime() : null), filter: fromSort("date", { horizon: "past", emptyLabel: "Not recorded" }) },
    {
      // filter-coverage: the tabs above the table are this column's control. A Status facet
      // as well would be two controls for one piece of state, which is the failure this
      // whole change exists to remove.
      field: "status",
      label: "Status",
      sort: (k) => (k.is_expired ? 1 : 0) },
  ] });

function MyKeysContent() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [activeTab, setActiveTab] = useState("active");
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [keyToDelete, setKeyToDelete] = useState<{ id: string; name: string } | null>(null);
  const { isReadOnly } = useReadOnlyMode();
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  // Fetch keys
  const {
    data: keysData,
    isLoading,
    refetch,
    refreshNow,
    dataUpdatedAt } = useRefreshableQuery({
    // important: one unfiltered fetch. The server was doing a lowercase substring match over
    // a list it had already materialised and cached, so every keystroke was a Vault round
    // trip AND its own react-query cache entry. Filtering client-side removes both, and it
    // is what makes the tab counts and the facet counts honest.
    queryKey: ["my-keys"],
    queryFn: ({ fresh, signal }) => api.myKeys.list(undefined, undefined, { fresh, signal }) });

  // When each key last changed something; a separate read, so a slow journal never holds the list.
  const { scopeKey } = useActorQueryScope();
  const { data: lastUsedData } = useQuery({
    queryKey: ["my-key-last-used", scopeKey],
    queryFn: ({ signal }) => myKeysActivityAPI.lastUsed({ signal }),
    staleTime: 60_000,
    retry: false,
  });
  const keys: KeyRow[] = useMemo(() => {
    const used = new Map((lastUsedData?.items ?? []).map((item) => [item.key_id, item.last_used_at]));
    return ((keysData as { keys?: KeyInfo[] } | undefined)?.keys ?? []).map((k) => ({
      ...k,
      last_used_at: used.get(k.id) ?? null,
    }));
  }, [keysData, lastUsedData]);
  const [activityKey, setActivityKey] = useState<KeyRow | null>(null);
  // Derived from the full list rather than the response counters, so a tab label cannot
  // disagree with what the other tab holds.
  const activeCount = useMemo(() => keys.filter((k) => !k.is_expired).length, [keys]);
  const expiredCount = useMemo(() => keys.filter((k) => k.is_expired).length, [keys]);

  const tabKeys = useMemo(() => {
    if (activeTab === "active") return keys.filter((k) => !k.is_expired);
    if (activeTab === "expired") return keys.filter((k) => k.is_expired);
    return keys;
  }, [keys, activeTab]);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  // note: the tabs are a filter over the same list, so they owe the same page reset the search
  // and facet controls give. Without it, moving to a shorter tab leaves the operator on a page
  // that tab does not have.
  const handleTabChange = useCallback(
    (value: string) => {
      setActiveTab(value);
      resetPage();
    },
    [resetPage],
  );

  const filters = useTableFilters(MY_KEYS, tabKeys, {
    onFilterChange: resetPage,
  });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    MY_KEYS.sortAccessors,
    MY_KEYS.defaultSort.field,
    MY_KEYS.defaultSort.direction,
    resetPage,
  );
  const sortedKeys = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );
  // note: the tabs are a filter too. A key created while the Expired tab is showing is hidden
  // by the tab, so "Show it" leaves the tab as well as clearing the toolbar.
  const showAllKeys = useCallback(() => {
    filters.clearAll();
    if (activeTab !== "active") handleTabChange("active");
  }, [filters, activeTab, handleTabChange]);
  const focus = useRowFocus({
    list: "my-keys",
    rows: keys,
    orderedRows: sortedKeys,
    keyOf: (k: KeyRow) => k.id,
    pageSize,
    setPage,
    dataUpdatedAt,
    clearFilters: showAllKeys,
  });

  // Clamped on read rather than corrected in an effect. Deleting the last key on the final page
  // shrinks the list under the operator, and an effect would fire after a wrong frame painted.
  const totalPages = Math.max(1, Math.ceil(sortedKeys.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedKeys = sortedKeys.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // note: the node list is built inside CreateKeyDialog by useNodePickerOptions, the one place
  // node-picker options are assembled. This page used to pass down
  // nodes.filter(n => n.connected).map(n => n.name), which silently dropped every node that was
  // not answering, so an operator whose node had gone offline saw it disappear from the picker
  // with nothing said. It is now listed and marked instead.

  // Fetch projects for dropdown
  const { data: projectsData, isLoading: projectsLoading } = useAllProjects();

  // Flatten projects from all hosts
  const projects = projectsData 
    ? (projectsData as any[]).flatMap((hostData: any) => hostData.projects || [])
    : [];

  // Generate key mutation
  const createMutation = useMutation({
    mutationFn: (request: KeyCreateRequest) => api.myKeys.create(request),
    onSuccess: (created) => {
      toast({
        title: "Key generated" });
      setCreateDialogOpen(false);
      requestRowFocus("my-keys", created.id, created.name);
      queryClient.invalidateQueries({ queryKey: ["my-keys"] });
      refetch();
    },
    onError: (error: any) => {
      toast({
        title: "Could not generate key",
        // The API answers this refusal for an Admin key on a cluster another organization also
        // holds (backend core/whole_cluster_access). On its own it does not say what to do next.
        description: error?.message === SHARED_CLUSTER_REFUSAL
          ? "Another organization also holds this cluster, and an Admin key would reach its projects too. Generate a Standard key for one of your projects, or ask an Administrator."
          : error.message || "Try again.",
        variant: "destructive" });
    } });

  // Delete key mutation
  const deleteMutation = useMutation({
    mutationFn: (keyId: string) => api.myKeys.delete(keyId),
    onSuccess: () => {
      toast({
        title: "Key deleted" });
      queryClient.invalidateQueries({ queryKey: ["my-keys"] });
      refetch();
    },
    onError: (error: any) => {
      toast({
        title: "Could not delete key",
        description: error.message || "Try again.",
        variant: "destructive" });
    } });

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  // Download key handler
  const handleDownload = async (keyId: string) => {
    try {
      const blob = await api.myKeys.download(keyId);
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${keyId}.pfx`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
      toast({
        title: "Key downloaded" });
      // The download is on the key's trail now; an open activity view should show it.
      void queryClient.invalidateQueries({ queryKey: ["my-key-activity"] });
    } catch (error: any) {
      toast({
        title: "Could not download key",
        description: error.message || "Try again.",
        variant: "destructive" });
    }
  };

  // Delete key handler
  const handleDeleteClick = (keyId: string, keyName: string) => {
    setKeyToDelete({ id: keyId, name: keyName });
    setDeleteDialogOpen(true);
  };

  const handleDeleteConfirm = () => {
    if (keyToDelete) {
      deleteMutation.mutate(keyToDelete.id);
    }
    setDeleteDialogOpen(false);
    setKeyToDelete(null);
  };

  const keyActions = (key: KeyRow): RowAction[] => [
    { id: "download", label: "Download PFX", icon: Download, onSelect: () => void handleDownload(key.id) },
    { id: "activity", label: "View Activity", icon: Activity, onSelect: () => setActivityKey(key) },
    {
      id: "delete",
      label: "Delete Key",
      icon: Trash2,
      destructive: true,
      separatorBefore: true,
      unavailableReason: isReadOnly ? "Disabled in read-only mode" : undefined,
      onSelect: () => handleDeleteClick(key.id, key.name),
    },
  ];

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-4 sm:p-8 space-y-6">
      <ReadOnlyBanner />
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div className="flex flex-col sm:flex-row items-start sm:items-center gap-2 sm:gap-4">
          <BackLink href="/settings" label="Settings" />
          <div className="min-w-0">
            <h1 className="text-2xl sm:text-3xl font-bold">
              My Keys
            </h1>
            <p className="text-muted-foreground mt-1 text-sm sm:text-base">
              Manage TLS identity keys for accessing hypervisor resources
            </p>
          </div>
        </div>
        <div className="flex gap-2 shrink-0">
          <RefreshButton
            label="Refresh keys"
            lastUpdatedAt={dataUpdatedAt}
            onRefresh={refreshNow}
          />
          <Button asChild size="sm" variant="outline" className="gap-1.5 shrink-0">
            <IntentLink href="/my-keys/activity">
              <ListChecks className="h-4 w-4" aria-hidden="true" />
              <span className="hidden sm:inline">Activity Log</span>
              <span className="sr-only sm:hidden">Activity Log</span>
            </IntentLink>
          </Button>
          <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
            <DialogTrigger asChild>
              <Button 
                size="sm" 
                className="gap-1.5 shrink-0"
                disabled={isReadOnly}
                title={isReadOnly ? "Disabled in read-only mode" : undefined}
                aria-label="Generate Key"
              >
                <Plus className="h-4 w-4" />
                {/* note: this was two spans, "Generate" gated on xs: and " Key" on sm:. There is
                    no xs breakpoint in tailwind.config.ts and this was its only use in the
                    codebase, so the class compiled to nothing and "hidden" won at every width:
                    the button read " Key" everywhere and the verb was never shown. */}
                <span className="hidden sm:inline">Generate Key</span>
              </Button>
            </DialogTrigger>
            <CreateKeyDialog
              projects={projects}
              projectsLoading={projectsLoading}
              onSubmit={(data) => createMutation.mutate(data)}
              isSubmitting={createMutation.isPending}
            />
          </Dialog>
        </div>
      </div>

      <KeyStats activeCount={activeCount} expiredCount={expiredCount} />

      {/* Keys Table with Tabs */}
      <Card>
        <CardHeader className="space-y-4">
          <Tabs value={activeTab} onValueChange={handleTabChange}>
            <TabsList className="grid w-full max-w-md grid-cols-3 h-auto p-1">
              <TabsTrigger value="active" className="text-xs sm:text-sm px-2 py-1.5">
                Active ({activeCount})
              </TabsTrigger>
              <TabsTrigger value="expired" className="text-xs sm:text-sm px-2 py-1.5">
                Expired ({expiredCount})
              </TabsTrigger>
              <TabsTrigger value="" className="text-xs sm:text-sm px-2 py-1.5">All ({keys.length})</TabsTrigger>
            </TabsList>
          </Tabs>
          <TableToolbar filters={filters} noun="keys" />
        </CardHeader>
        <CardContent>
          {focus.hidden && (
            <RowFocusNotice label={focus.hidden.label} onShow={focus.showHidden} className="mb-4" />
          )}
          {/* note: gate on the rendered rows, not on the whole key set. Filtering, searching or
              the Expired tab can empty the table while `keys` is still populated, and gating on
              `keys.length` rendered a headers-only table with no message at all. The copy keys
              off the full set, so it stays right whichever of the three did the emptying. */}
          {sortedKeys.length === 0 ? (
            <EmptyState
              icon={Key}
              title="No keys found"
              description={
                filters.activeFilterCount > 0
                  ? "Try adjusting your search or filters"
                  : keys.length > 0
                    ? "No key on this tab. The other tabs may still have some."
                    : "Generate your first key to get started"
              }
            />
          ) : (
            <>
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Cluster" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Access Level" field="auth_level" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Created" field="created" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Expires" field="expires" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Last Used" field="last_used" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <TableHead pinned="end" className="w-12 text-right"><span className="sr-only">Actions</span></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pagedKeys.map((key) => (
                  <TableRow key={key.id} {...focus.rowProps(key.id)}>
                    <TableCell className="font-medium">{key.name}</TableCell>
                    <TableCell><NodeLink clusterId={key.node} /></TableCell>
                    <TableCell>
                      {key.project}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          key.auth_level === "admin"
                            ? "default"
                            : "outline"
                        }
                        className="gap-1"
                      >
                        <Shield className="h-3 w-3" />
                        {key.auth_level === "admin"
                          ? "Admin"
                          : "Standard"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatDate(key.created_at)}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {key.expires_at
                        ? formatDate(key.expires_at)
                        : "Never"}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {key.last_used_at ? formatDateTime(key.last_used_at) : <TableEmptyValue label="Not recorded" />}
                    </TableCell>
                    <TableCell>
                      {key.is_expired ? (
                        <Badge variant="negative" className="gap-1">
                          <AlertCircle className="h-3 w-3" />
                          Expired
                        </Badge>
                      ) : (
                        <Badge variant="positive" className="gap-1">
                          <CheckCircle className="h-3 w-3" />
                          Active
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell pinned="end" className="text-right">
                      <RowActions triggerLabel={`Actions for key ${key.name} on ${key.node}`} actions={keyActions(key)} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {/* Absent rather than disabled when everything fits on one page. Outside the
                horizontal scroller, so the controls stay put when a wide table scrolls. */}
            {totalPages > 1 && (
              <div className="mt-4">
                <DataTablePagination
                  currentPage={currentPage}
                  totalPages={totalPages}
                  pageSize={pageSize}
                  pageSizeOptions={[...TABLE_PAGE_SIZES]}
                  totalItems={sortedKeys.length}
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

      <KeyActivityDialog keyRow={activityKey} onOpenChange={(open) => { if (!open) setActivityKey(null); }} />

      <DeleteKeyDialog
        keyName={keyToDelete?.name ?? null}
        open={deleteDialogOpen}
        onOpenChange={(open) => {
          setDeleteDialogOpen(open);
          if (!open) setKeyToDelete(null);
        }}
        onConfirm={handleDeleteConfirm}
      />
    </div>
  );
}

// The role gate stays in this file: check-role-gates.mjs reads page text and does not follow
// imports. Suspense is required because useTableFilters reads useSearchParams.

export default function MyKeysPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <MyKeysContent />
    </Suspense>
  );
}
