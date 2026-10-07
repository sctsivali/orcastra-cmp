"use client";

import { Suspense, useCallback, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { BackLink } from "@/components/ui/back-link";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow } from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger } from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle } from "@/components/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Key,
  Plus,
  Download,
  Trash2,
  
  
  Shield,
  
  
  AlertCircle,
  CheckCircle,
  ChevronLeft } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useAllProjects } from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { api } from "@/lib/api";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { KeyInfo, KeyCreateRequest } from "@/types/api";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { IntentLink } from "@/components/navigation/route-intent";
import { Input } from "@/components/ui/input";
import { formatLabel } from "@/lib/utils";
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

const MY_KEYS = defineTable<KeyInfo>()({
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

  const keys = useMemo(() => (keysData as any)?.keys || [], [keysData]);
  // Derived from the full list rather than the response counters, so a tab label cannot
  // disagree with what the other tab holds.
  const activeCount = useMemo(() => keys.filter((k: KeyInfo) => !k.is_expired).length, [keys]);
  const expiredCount = useMemo(() => keys.filter((k: KeyInfo) => k.is_expired).length, [keys]);

  const tabKeys = useMemo(() => {
    if (activeTab === "active") return keys.filter((k: KeyInfo) => !k.is_expired);
    if (activeTab === "expired") return keys.filter((k: KeyInfo) => k.is_expired);
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

  const filters = useTableFilters(MY_KEYS, tabKeys as KeyInfo[], {
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
    keyOf: (k: KeyInfo) => k.id,
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
        title: "Success",
        description: "Key generated successfully" });
      setCreateDialogOpen(false);
      requestRowFocus("my-keys", created.id, created.name);
      queryClient.invalidateQueries({ queryKey: ["my-keys"] });
      refetch();
    },
    onError: (error: any) => {
      toast({
        title: "Error",
        description: error.message || "Failed to generate key",
        variant: "destructive" });
    } });

  // Delete key mutation
  const deleteMutation = useMutation({
    mutationFn: (keyId: string) => api.myKeys.delete(keyId),
    onSuccess: () => {
      toast({
        title: "Success",
        description: "Key deleted successfully" });
      queryClient.invalidateQueries({ queryKey: ["my-keys"] });
      refetch();
    },
    onError: (error: any) => {
      toast({
        title: "Error",
        description: error.message || "Failed to delete key",
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
        title: "Success",
        description: "Key downloaded successfully" });
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to download key",
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

      {/* Stats Cards */}
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Active Keys</CardTitle>
            <CheckCircle className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{activeCount}</div>
            <p className="text-xs text-muted-foreground">Valid and usable keys</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Expired Keys</CardTitle>
            <AlertCircle className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{expiredCount}</div>
            <p className="text-xs text-muted-foreground">Keys past expiration</p>
          </CardContent>
        </Card>
      </div>

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
            <div className="text-center py-12 text-muted-foreground">
              <Key className="h-12 w-12 mx-auto mb-4 opacity-50" />
              <p className="text-lg font-medium">No keys found</p>
              <p className="text-sm mt-2">
                {filters.activeFilterCount > 0
                  ? "Try adjusting your search or filters"
                  : keys.length > 0
                    ? "No key on this tab. The other tabs may still have some."
                    : "Generate your first key to get started"}
              </p>
            </div>
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
                  <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pagedKeys.map((key: KeyInfo) => (
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
                      {new Date(key.created_at).toLocaleDateString('en-GB')}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {key.expires_at
                        ? new Date(key.expires_at).toLocaleDateString('en-GB')
                        : "Never"}
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
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => handleDownload(key.id)}
                          title="Download PFX"
                        >
                          <Download className="h-4 w-4" />
                        </Button>
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => handleDeleteClick(key.id, key.name)}
                          title={isReadOnly ? "Disabled in read-only mode" : "Delete Key"}
                          disabled={isReadOnly}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
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

      {/* Delete Confirmation Dialog */}
      <AlertDialog open={deleteDialogOpen} onOpenChange={(open) => {
        setDeleteDialogOpen(open);
        if (!open) setKeyToDelete(null);
      }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Key</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete the key <strong className="text-foreground">&quot;{keyToDelete?.name}&quot;</strong>?
              This action cannot be undone and will permanently remove this TLS identity key.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setKeyToDelete(null)}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteConfirm}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

// Generate Key Dialog Component
function CreateKeyDialog({
  projects,
  projectsLoading,
  onSubmit,
  isSubmitting }: {
  projects: any[];
  projectsLoading: boolean;
  onSubmit: (data: KeyCreateRequest) => void;
  isSubmitting: boolean;
}) {
  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: loadingNodes } = nodePicker;

  const [formData, setFormData] = useState<KeyCreateRequest>({
    name: "",
    node: "",  // Don't auto-select - let user choose
    project: "",  // Don't auto-select - let user choose
    auth_level: "standard",
    expire_days: 365 });

  // Get unique projects for the selected node
  const filteredProjects = formData.node 
    ? projects.filter((p: any) => !p.host || p.host === formData.node)
    : [];
  
  // Get unique project names to avoid duplicates
  const uniqueProjects = filteredProjects.reduce((acc: any[], project: any) => {
    if (!acc.find(p => p.name === project.name)) {
      acc.push(project);
    }
    return acc;
  }, []);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.node) {
      return; // Don't submit without node
    }
    if (formData.auth_level === "standard" && !formData.project) {
      return; // Don't submit without project for standard auth
    }
    onSubmit(formData);
  };

  // Reset project when node changes
  const handleNodeChange = (value: string) => {
    setFormData({ ...formData, node: value, project: "" });
  };

  return (
    <DialogContent className="sm:max-w-[550px]">
      <DialogHeader>
        <DialogTitle>Generate New Key</DialogTitle>
        <DialogDescription>
          Generate a new TLS identity key for accessing hypervisor resources. The key will
          be generated with specific permissions for the selected project.
        </DialogDescription>
      </DialogHeader>
      <form onSubmit={handleSubmit}>
        <div className="grid gap-4 py-4">
          <div className="grid gap-2">
            <Label htmlFor="name">Key Name</Label>
            <Input
              id="name"
              placeholder="e.g., Production Access Key"
              value={formData.name}
              onChange={(e) =>
                setFormData({ ...formData, name: e.target.value })
              }
              required
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="node">Cluster</Label>
            <SearchableSelect
              id="node"
              value={formData.node}
              onValueChange={handleNodeChange}
              options={nodeOptions}
              placeholder={loadingNodes ? "Loading..." : "Select a cluster"}
              searchPlaceholder="Search clusters..."
              emptyText="No clusters available."
              disabled={loadingNodes}
            />
            <NodeStatusNotice
              meta={formData.node ? nodePicker.statusOf(formData.node) : undefined}
              reason={
                formData.node
                  ? nodePicker.availabilityOf(formData.node)?.reason ?? undefined
                  : undefined
              }
              nodeLabel={formData.node ? nodePicker.labelOf(formData.node) : undefined}
              unavailable={nodePicker.statusUnavailable}
              noSelectableNodes={nodePicker.selectableCount === 0}
              updatedAt={nodePicker.statusUpdatedAt}
              onRecheck={nodePicker.refreshNow}
            />
          </div>
          {formData.auth_level === "standard" && (
            <div className="grid gap-2">
              <Label htmlFor="project">Project</Label>
              <SearchableSelect
                id="project"
                value={formData.project}
                onValueChange={(value) =>
                  setFormData({ ...formData, project: value })
                }
                options={uniqueProjects.map((project: any) => ({
                  value: project.name,
                  label: project.name,
                }))}
                placeholder={
                  !formData.node
                    ? "Select a cluster first"
                    : projectsLoading
                      ? "Loading projects..."
                      : uniqueProjects.length === 0
                        ? `No projects available for ${formData.node}`
                        : "Select a project"
                }
                searchPlaceholder="Search projects..."
                emptyText="No projects found."
                disabled={projectsLoading || !formData.node || uniqueProjects.length === 0}
              />
            </div>
          )}
          <div className="grid gap-2">
            <Label htmlFor="auth_level">Authorization Level</Label>
            <Select
              value={formData.auth_level}
              onValueChange={(value) =>
                setFormData({ ...formData, auth_level: value })
              }
            >
              <SelectTrigger id="auth_level">
                <SelectValue placeholder="Select access level" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="standard">Standard</SelectItem>
                <SelectItem value="admin">Admin</SelectItem>
              </SelectContent>
            </Select>
            <div className="text-xs text-muted-foreground space-y-1">
              {formData.auth_level === "standard" ? (
                <p>Project-level access: manage instances and resources within the selected project only.</p>
              ) : (
                <p>Server-level access: full administrative control across all projects and server configurations.</p>
              )}
            </div>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="expire_days">
              Expiration (days)
            </Label>
            <Input
              id="expire_days"
              type="number"
              min={1}
              max={3650}
              value={formData.expire_days}
              onChange={(e) => {
                const value = e.target.value;
                // Allow empty string for clearing
                if (value === '') {
                  setFormData({
                    ...formData,
                    expire_days: '' as any });
                } else {
                  const numValue = parseInt(value);
                  if (!isNaN(numValue) && numValue >= 1 && numValue <= 3650) {
                    setFormData({
                      ...formData,
                      expire_days: numValue });
                  }
                }
              }}
              onBlur={(e) => {
                // Set to 365 if empty on blur
                if (e.target.value === '' || parseInt(e.target.value) < 1) {
                  setFormData({
                    ...formData,
                    expire_days: 365 });
                }
              }}
              placeholder="365"
              required
            />
            <p className="text-xs text-muted-foreground">
              Key will expire on{" "}
              {new Date(
                Date.now() + (formData.expire_days || 365) * 24 * 60 * 60 * 1000
              ).toLocaleDateString('en-GB')}
            </p>
          </div>
        </div>
        <DialogFooter>
          <Button type="submit" disabled={isSubmitting}>
            {isSubmitting ? "Generating..." : "Generate Key"}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
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
