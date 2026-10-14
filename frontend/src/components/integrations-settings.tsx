"use client";

import * as React from "react";
import { Suspense, useCallback, useMemo, useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Key,
  Copy,
  CheckCircle2,
  XCircle,
  Ban,
  Shield,
  Server,
  Eye,
  EyeOff,
  ShieldAlert,
  AlertTriangle,
  CalendarClock,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { RefreshButton } from "@/components/ui/refresh-button";
import { IntegrationClusterAccessList } from "@/components/integration-cluster-access-list";
import { OverflowText } from "@/components/ui/overflow-text";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { toast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import type { APIKeyStatusLabel } from "@/lib/integration-display";
import {
  API_KEY_STATUS_ORDER,
  formatApplicationType,
  getAPIKeyStatus,
  getClusterAccessDisplay,
  isLegacyUnscopedKey,
  summarizeClusterAccessLevel,
} from "@/lib/integration-display";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { SortSelect } from "@/components/ui/sort-select";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { defineTable, fromSort } from "@/lib/table-filter";
import { GRID_PAGE_SIZE, GRID_PAGE_SIZES } from "@/lib/pagination";
import { VaultUnavailableNotice } from "@/components/ui/vault-unavailable-notice";
import { EditAPIKeyPermissionsDialog } from "@/components/integrations/edit-api-key-permissions-dialog";
import { EditAPIKeyCapabilitiesDialog } from "@/components/integrations/edit-api-key-capabilities-dialog";
import { CreateAPIKeyDialog } from "@/components/integrations/create-api-key-dialog";
import { ApiKeyConnectionGuide } from "@/components/integrations/api-key-connection-guide";
import { RotateSecretButton } from "@/components/integrations/rotate-secret-button";
import { KeyOwnershipSummary, TransferOwnershipFirst } from "@/components/integrations/key-ownership-summary";
import { usePermissions } from "@/hooks/use-permissions";
import { keyOwnsOrganizationsDetail, provisioningWouldStopDetail } from "@/lib/key-ownership-display";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import type {
  APIKeyInfo,
  APIKeyCreateRequest,
  APIKeyCreateResponse,
  AvailableCluster,
  ClusterPermission,
} from "@/types";
import { copyText } from "@/lib/clipboard";
import { isVaultUnavailable, retryUnlessUnavailable } from "@/lib/read-error";
import { ReadErrorState } from "@/components/ui/read-error-state";

// API Key Secret Dialog
function APIKeySecretDialog({
  open,
  onClose,
  response,
  title = "API Key Created Successfully",
  description = "Save these credentials now. The secret will not be shown again.",
}: {
  open: boolean;
  onClose: () => void;
  response: APIKeyCreateResponse | null;
  title?: string;
  description?: string;
}) {
  const [showSecret, setShowSecret] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  const copyToClipboard = async (text: string, type: string) => {
    const ok = await copyText(text);
    if (!ok) {
      toast({
        title: "Copy failed",
        description: "Please select and copy the value manually.",
        variant: "destructive",
      });
      return;
    }
    setCopied(type);
    setTimeout(() => setCopied(null), 2000);
    toast({
      title: "Copied!",
      description: `API Key ${type} copied to clipboard.`,
    });
  };

  if (!response) return null;

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogBody className="-mr-2 pr-2">
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label>API Key ID</Label>
              <div className="flex gap-2">
                <Input 
                  value={response.api_key_id} 
                  readOnly 
                  className="font-mono text-sm"
                />
                <Button
                  size="icon"
                  variant="outline"
                  onClick={() => copyToClipboard(response.api_key_id, "ID")}
                >
                  {copied === "ID" ? <CheckCircle2 className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
                </Button>
              </div>
            </div>
            <div className="space-y-2">
              <Label>API Key Secret</Label>
              <div className="flex gap-2">
                <Input 
                  value={showSecret ? response.api_key_secret : "••••••••••••••••••••••••"} 
                  readOnly 
                  className="font-mono text-sm"
                />
                <Button
                  size="icon"
                  variant="outline"
                  onClick={() => setShowSecret(!showSecret)}
                >
                  {showSecret ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </Button>
                <Button
                  size="icon"
                  variant="outline"
                  onClick={() => copyToClipboard(response.api_key_secret, "Secret")}
                >
                  {copied === "Secret" ? <CheckCircle2 className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
                </Button>
              </div>
            </div>
            <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-md p-3 text-sm">
              <p className="flex items-center gap-2 font-medium text-yellow-600">
                <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden />
                <span>Important</span>
              </p>
              <p className="text-muted-foreground mt-1">
                Copy and store these credentials securely. The secret cannot be retrieved later.
              </p>
            </div>
            {/* The credentials alone were the whole dialog, and a key holder had no way to learn
                the base URL, the header names, or which endpoints the key reaches. The obvious
                first call is a JWT route and answers 401, which reads as "this needs a login". */}
            <ApiKeyConnectionGuide response={response} />
          </div>
        </DialogBody>
        <DialogFooter className="shrink-0 border-t pt-3">
          <Button onClick={onClose}>I have saved the credentials</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// Main Integrations Settings Component (for embedding in Settings page)
/**
 * A credential list is a security surface, so the questions it has to answer are narrow ones:
 * which keys are lapsed, which still reach a cluster, which predate capabilities. Sorting alone
 * answered none of them, and nineteen full-height cards in one scroll answered them badly.
 *
 * important: an id. The settings screen carries more than one filtered list, and without a
 * namespace they would share `q` and narrow each other on every keystroke.
 */
const API_KEYS = defineTable<APIKeyInfo>()({
  id: "apikeys",
  searchPlaceholder: "Search keys by name, application, or id",
  defaultSort: { field: "name", direction: "asc" },
  // The key id and the capability names are on the card but are not columns of their own.
  searchExtra: (key) => [
    key.id,
    key.app_name ?? "",
    ...(key.capabilities ?? []),
  ],
  columns: [
    {
      field: "name",
      label: "Name",
      sort: (key) => key.name,
      search: (key) => key.name,
      filter: fromSort("text"),
    },
    {
      field: "application",
      label: "Application",
      sort: (key) => formatApplicationType(key.app_type),
      facet: (key) => formatApplicationType(key.app_type),
      facetSpec: { primary: true },
    },
    {
      field: "status",
      label: "Status",
      sort: (key) => getAPIKeyStatus(key),
      facet: (key) => getAPIKeyStatus(key),
      // Working first, then the two ways a key stops working, rather than by today's counts.
      facetSpec: { primary: true, order: API_KEY_STATUS_ORDER },
    },
    {
      field: "clusters",
      label: "Clusters",
      sort: (key) => key.cluster_permissions?.length ?? 0,
      filter: fromSort("number", {
        unit: "count",
        // A key reaching nothing is the one an operator wants to find and revoke.
        presets: [
          { label: "None", min: null, max: 0 },
          { label: "Any", min: 1, max: null },
        ],
      }),
    },
    {
      field: "access",
      label: "Access",
      sort: (key) => summarizeClusterAccessLevel(getClusterAccessDisplay(key.cluster_permissions)),
      facet: (key) => summarizeClusterAccessLevel(getClusterAccessDisplay(key.cluster_permissions)),
    },
    {
      // note: null capabilities and an empty list are opposites here. A key predating
      // capabilities keeps unrestricted access to the external router, so it gets its own
      // dimension rather than being read off a count of zero.
      field: "scope",
      label: "Capability Scope",
      facet: (key) => (isLegacyUnscopedKey(key.capabilities) ? "Unscoped legacy" : "Scoped"),
    },
    {
      field: "expires",
      label: "Expires",
      sort: (key) => (key.expires_at ? new Date(key.expires_at).getTime() : null),
      filter: fromSort("date", { horizon: "future" }),
    },
  ],
});

/** The badge tone each status wears. Paired here so the word and the colour move together. */
const API_KEY_STATUS_VARIANTS: Record<APIKeyStatusLabel, "destructive" | "negative" | "positive" | "secondary"> = {
  Revoked: "destructive",
  Expired: "negative",
  Active: "positive",
  Inactive: "secondary",
};

const KEY_SORT_OPTIONS = [
  { value: "name", label: API_KEYS.byField.name.label },
  { value: "application", label: API_KEYS.byField.application.label },
  { value: "status", label: API_KEYS.byField.status.label },
  { value: "clusters", label: API_KEYS.byField.clusters.label },
  { value: "expires", label: API_KEYS.byField.expires.label },
] as const;

function IntegrationsSettingsContent() {
  const queryClient = useQueryClient();
  const { isAdmin } = usePermissions();
  const [mounted, setMounted] = useState(false);
  const [createdKey, setCreatedKey] = useState<APIKeyCreateResponse | null>(null);
  // note: a queue, so two rotations that overlap each show their own secret once
  const [rotatedKeys, setRotatedKeys] = useState<APIKeyCreateResponse[]>([]);

  useEffect(() => {
    setMounted(true);
  }, []);

  // Fetch API keys
  const {
    data: apiKeysData,
    isLoading: keysLoading,
    error: keysError,
    refetch: refetchKeys,
    refreshNow: refreshKeys,
    dataUpdatedAt: keysUpdatedAt,
  } = useRefreshableQuery({
    queryKey: ["integrations", "api-keys"],
    queryFn: ({ fresh, signal }) => api.integrations.listAPIKeys({ fresh, signal }),
    enabled: mounted,
    staleTime: 30000,
    // A 503 (Vault not configured, or the identity provider unreachable) does not clear within a
    // retry, and the page says which one it is.
    retry: retryUnlessUnavailable,
  });

  // Vault-not-configured, told apart from every other 503 (an identity-provider outage too).
  const vaultUnavailable = isVaultUnavailable(keysError);

  // Fetch available clusters
  const { data: clustersData } = useQuery({
    queryKey: ["integrations", "available-clusters"],
    queryFn: () => api.integrations.getAvailableClusters(),
    enabled: mounted,
    staleTime: 60000,
  });

  // Fetch the capability vocabulary. Served rather than hardcoded so the picker always matches
  // what the external router enforces. It only changes on deploy, so it is cached for an hour.
  const { data: capabilityData } = useQuery({
    queryKey: ["integrations", "capabilities"],
    queryFn: () => api.integrations.getCapabilities(),
    enabled: mounted,
    staleTime: 3600000,
  });

  // Create mutation
  const createMutation = useMutation({
    mutationFn: (request: APIKeyCreateRequest) => api.integrations.createAPIKey(request),
    onSuccess: (response) => {
      setCreatedKey(response);
      queryClient.invalidateQueries({ queryKey: ["integrations", "api-keys"] });
      toast({
        title: "API Key Created",
        description: "Do not forget to save the secret!",
      });
    },
    onError: (error: Error) => {
      toast({
        title: "Failed to create API key",
        description: isVaultUnavailable(error)
          ? "Vault is not configured. Enable Vault in backend settings for API key management."
          : error.message,
        variant: "destructive",
      });
    },
  });

  // Permissions update. Narrowing an over-broad grant used to mean revoking the credential and
  // asking the integrator for a new one, because this endpoint had no caller.
  const updatePermissionsMutation = useMutation({
    mutationFn: ({ keyId, permissions }: { keyId: string; permissions: ClusterPermission[] }) =>
      api.integrations.updateAPIKeyPermissions(keyId, permissions),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["integrations", "api-keys"] });
      toast({ title: "Cluster Access Updated" });
    },
    onError: (error: Error) => {
      toast({
        title: "Failed to update access",
        description: error.message,
        variant: "destructive",
      });
    },
  });

  // Capability edits. Withdrawing tenant.provision from a key that owns organizations answers 409
  // with what would stop, and withdrawing org.sync or inventory.read from one is refused; the
  // dialog shows both, and resends the first with force when the operator confirms.
  const updateCapabilitiesMutation = useMutation({
    mutationFn: ({ keyId, capabilities, force }: { keyId: string; capabilities: string[]; force: boolean }) =>
      api.integrations.updateAPIKeyCapabilities(keyId, { capabilities, force }),
    onSuccess: () => {
      void reconcileMutationImpact(queryClient, "integration.write").catch(() => undefined);
      toast({ title: "Capabilities Updated" });
    },
    onError: (error: Error) => {
      if (provisioningWouldStopDetail(error) || keyOwnsOrganizationsDetail(error)) return;
      toast({ title: "Failed to update capabilities", description: error.message, variant: "destructive" });
    },
  });

  // Revoke mutation
  const revokeMutation = useMutation({
    mutationFn: (keyId: string) => api.integrations.revokeAPIKey(keyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["integrations", "api-keys"] });
      toast({ title: "API Key Revoked" });
    },
    onError: (error: Error) => {
      // A key that came to own an organization after this list loaded is refused by the server,
      // and the reread puts its ownership on the card.
      if (keyOwnsOrganizationsDetail(error)) {
        queryClient.invalidateQueries({ queryKey: ["integrations", "api-keys"] });
      }
      toast({ title: "Failed to revoke", description: error.message, variant: "destructive" });
    },
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (keyId: string) => api.integrations.deleteAPIKey(keyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["integrations", "api-keys"] });
      toast({ title: "API Key Deleted" });
    },
    onError: (error: Error) => {
      if (keyOwnsOrganizationsDetail(error)) {
        queryClient.invalidateQueries({ queryKey: ["integrations", "api-keys"] });
      }
      toast({ title: "Failed to delete", description: error.message, variant: "destructive" });
    },
  });

  // important: derived above the loading return, not below it. The filter, sort and page hooks
  // read these, and a hook below a conditional return is a hook that does not always run.
  const apiKeys: APIKeyInfo[] = useMemo(
    () => (Array.isArray(apiKeysData?.keys) ? apiKeysData.keys : []),
    [apiKeysData],
  );
  const clusters: AvailableCluster[] = useMemo(
    () => (Array.isArray(clustersData?.clusters) ? clustersData.clusters : []),
    [clustersData],
  );
  const totalKeys = typeof apiKeysData?.total === 'number' ? apiKeysData.total : 0;
  const activeKeys = typeof apiKeysData?.active === 'number' ? apiKeysData.active : 0;
  const revokedKeys = typeof apiKeysData?.revoked === 'number' ? apiKeysData.revoked : 0;
  const expiredKeys = typeof apiKeysData?.expired === 'number' ? apiKeysData.expired : 0;

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(GRID_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  const filters = useTableFilters(API_KEYS, apiKeys, {
    onFilterChange: resetPage,
    // A settings tab, not a route of its own: leaving these keys in the address bar would
    // filter a list the operator has already navigated away from.
    syncUrl: false,
  });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    API_KEYS.sortAccessors,
    API_KEYS.defaultSort.field,
    API_KEYS.defaultSort.direction,
    resetPage,
  );

  // Filter, then sort, then slice. Sorting covers the whole filtered set, not the page shown.
  const sortedKeys = useMemo(
    () => sortItems(filters.filteredRows),
    [sortItems, filters.filteredRows],
  );
  // Clamped on read rather than corrected in an effect, which would snap the operator back a
  // page mid-task and paint one wrong frame on the way.
  const totalPages = Math.max(1, Math.ceil(sortedKeys.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedKeys = sortedKeys.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // Show loading skeleton
  if (!mounted || keysLoading) {
    return (
      <div className="space-y-4">
        <div className="h-8 w-48 bg-muted animate-pulse rounded" />
        <div className="grid grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-24 bg-muted animate-pulse rounded-lg" />
          ))}
        </div>
        <div className="h-48 bg-muted animate-pulse rounded-lg" />
      </div>
    );
  }

  // Vault not configured, so show an informative banner instead of the full UI
  if (vaultUnavailable) {
    return (
      <div className="space-y-6">
        <div>
          <h2 className="text-2xl font-bold">Integrations</h2>
          <p className="text-sm text-muted-foreground">
            Manage API keys for external applications
          </p>
        </div>
        <VaultUnavailableNotice surface="API key management" />
      </div>
    );
  }

  // Any other failed read says so, rather than rendering zero totals over an empty table.
  if (keysError && !apiKeysData) {
    return (
      <div className="space-y-6">
        <div>
          <h2 className="text-2xl font-bold">Integrations</h2>
          <p className="text-sm text-muted-foreground">
            Manage API keys for external applications
          </p>
        </div>
        <ReadErrorState noun="API keys" error={keysError} onRetry={() => refetchKeys()} />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header with actions */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-2xl font-bold">Integrations</h2>
          <p className="text-sm text-muted-foreground mt-1">
            Manage API keys for external applications
          </p>
        </div>
        <div className="flex gap-2">
          <RefreshButton
            label="Refresh API keys"
            lastUpdatedAt={keysUpdatedAt}
            onRefresh={refreshKeys}
          />
          <CreateAPIKeyDialog
            clusters={clusters}
            capabilities={capabilityData?.capabilities ?? []}
            applicationTypes={capabilityData?.application_types ?? []}
            onSubmit={(data) => createMutation.mutateAsync(data)}
            isSubmitting={createMutation.isPending}
          />
        </div>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
        <Card>
          <CardContent className="pt-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-muted-foreground">Total Keys</p>
                <p className="text-2xl font-bold">{totalKeys}</p>
              </div>
              <Key className="h-8 w-8 text-muted-foreground" />
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-muted-foreground">Active Keys</p>
                <p className="text-2xl font-bold text-muted-foreground">{activeKeys}</p>
              </div>
              <CheckCircle2 className="h-8 w-8 text-muted-foreground" />
            </div>
          </CardContent>
        </Card>
        {/* An expired key is no longer counted as active, so without this tile the figures stop
            adding up and the drop reads as keys having been deleted. */}
        <Card>
          <CardContent className="pt-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-muted-foreground">Expired Keys</p>
                <p className="text-2xl font-bold text-muted-foreground">{expiredKeys}</p>
              </div>
              <CalendarClock className="h-8 w-8 text-muted-foreground" />
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-muted-foreground">Revoked Keys</p>
                <p className="text-2xl font-bold text-muted-foreground">{revokedKeys}</p>
              </div>
              <XCircle className="h-8 w-8 text-muted-foreground" />
            </div>
          </CardContent>
        </Card>
      </div>

      {/* API Keys List */}
      {apiKeys.length === 0 ? (
        <Card>
          <CardContent className="py-12">
            <div className="text-center space-y-4">
              <Key className="h-12 w-12 mx-auto text-muted-foreground" />
              <div>
                <h3 className="text-lg font-medium">No API keys</h3>
                <p className="text-muted-foreground">
                  Create an API key to allow external applications to connect.
                </p>
              </div>
              <CreateAPIKeyDialog
                clusters={clusters}
                capabilities={capabilityData?.capabilities ?? []}
                applicationTypes={capabilityData?.application_types ?? []}
                onSubmit={(data) => createMutation.mutateAsync(data)}
                isSubmitting={createMutation.isPending}
              />
            </div>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          <TableToolbar
            filters={filters}
            noun="API keys"
            actions={
              <SortSelect
                options={KEY_SORT_OPTIONS}
                value={sortField}
                direction={sortDirection}
                onSort={handleSort}
              />
            }
          />

          {pagedKeys.length === 0 ? (
            <EmptyState
              icon={Key}
              title="No API key matches the current filters"
              description={`${filters.activeFilterCount} filter${filters.activeFilterCount === 1 ? "" : "s"} active on ${apiKeys.length} keys.`}
              action={
                <Button size="sm" variant="outline" onClick={() => filters.clearAll()}>
                  Clear filters
                </Button>
              }
            />
          ) : (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {pagedKeys.map((apiKey) => {
            if (!apiKey || typeof apiKey.id !== 'string') return null;
            const clusterAccess = getClusterAccessDisplay(apiKey.cluster_permissions, clusters);
            const clusterCount = clusterAccess.length;
            
            const permLevel = summarizeClusterAccessLevel(clusterAccess);

            const expiryLabel = apiKey.expires_at
              ? new Date(apiKey.expires_at).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" })
              : "Never";
            // note: the state is computed on the server, from the same helper the authentication
            // path uses, so the badge and the refusal cannot disagree. Deriving it here would
            // mean parsing the stored string in the browser, where an offsetless value is read
            // as local time and lands a day out either side of midnight.
            const expiryState = apiKey.expiry_state ?? (apiKey.expires_at ? "active" : "never");
            const keyStatus = getAPIKeyStatus(apiKey);
            // An owner key is not revoked or deleted by the server, so its dialogs explain why and
            // offer the way forward instead of a destructive action that would only be refused.
            // note: only the organizations no other key co-owns stop the end; the server re-checks
            // that the co-owner can still sync them.
            const ownsOrganizations = (apiKey.sole_owned_organization_count ?? apiKey.owned_organization_count ?? 0) > 0;
            
            return (
              <Card key={apiKey.id}>
                <CardHeader className="pb-3">
                  {/* important: min-w-0 on the text block and shrink-0 on the badge. The key id
                      is a 34 character unbreakable token, so without these the description set
                      the card's width and pushed the status badge 127px outside its own card at
                      the two-column breakpoint. The id itself reveals rather than clipping
                      silently, because it is the one string that identifies the credential. */}
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 space-y-1">
                      <CardTitle className="text-lg">
                        {String(apiKey.name || 'N/A')}
                      </CardTitle>
                      <CardDescription className="flex items-center gap-2">
                        <span className="min-w-0 font-mono text-xs bg-muted px-2 py-0.5 rounded">
                          {/* note: auto, not always. The "always" preset is for a value whose
                              answer is known, ex; a 64 character fingerprint that never fits.
                              A key id is 34 characters and fits or does not depending on the
                              card width, so measuring is the honest answer and a chevron on a
                              value that is fully visible is noise. */}
                          <OverflowText value={String(apiKey.id)} label="API key id" />
                        </span>
                        <span>•</span>
                        <span className="min-w-0">
                          <OverflowText
                            value={String(apiKey.app_name || '')}
                            label={`application for ${String(apiKey.name || 'N/A')}`}
                          />
                        </span>
                      </CardDescription>
                    </div>
                    <div className="shrink-0">
                      {/* Six keys on this deployment read a green "Active" while lapsed, which
                          is the distinction that was actually missing.

                          note: Expired shares the red family with Revoked deliberately. Both
                          mean the key does not work, and giving the same outcome two colours
                          would be decoration; the word carries the cause, which is the part
                          that differs. The tones are the solid --status-* ones whose ratios
                          ui/badge.tsx records for both themes, not the translucent tints it
                          measures at 2.73:1 on --card in light mode.

                          important: the word comes from getAPIKeyStatus, the same helper the
                          Status filter reads. Deciding it twice is how a chip and the facet
                          beside it end up disagreeing about the same key. */}
                      <Badge variant={API_KEY_STATUS_VARIANTS[keyStatus]}>{keyStatus}</Badge>
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
                    <div className="flex items-center gap-2">
                      <Server className="h-4 w-4 text-muted-foreground" />
                      <span className="text-muted-foreground">Clusters:</span>
                      <span className="font-medium">{clusterCount}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <Shield className="h-4 w-4 text-muted-foreground" />
                      <span className="text-muted-foreground">Access:</span>
                      <span className="font-medium">{permLevel}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <CalendarClock className="h-4 w-4 shrink-0 text-muted-foreground" />
                      <span className="text-muted-foreground">Expires:</span>
                      <span className="font-medium whitespace-nowrap">{expiryLabel}</span>
                      {expiryState === "expiring" && (
                        <Badge variant="caution">Expiring soon</Badge>
                      )}
                    </div>
                  </div>

                  {/* What the key may DO, as opposed to which clusters it may reach. A legacy key
                      carries no capability field and still reaches every external endpoint, so it
                      is called out rather than rendered as an empty list, which would read as the
                      safe case while being the broad one. */}
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline">{formatApplicationType(apiKey.app_type)}</Badge>
                    {isLegacyUnscopedKey(apiKey.capabilities) ? (
                      <Badge variant="caution" className="gap-1">
                        <ShieldAlert className="h-3 w-3" aria-hidden="true" />
                        Unscoped legacy key
                      </Badge>
                    ) : (
                      (apiKey.capabilities ?? []).map((capability) => (
                        <Badge key={capability} variant="muted" className="font-mono font-normal">
                          {capability}
                        </Badge>
                      ))
                    )}
                  </div>
                  <IntegrationClusterAccessList
                    clusters={clusterAccess}
                    keyName={String(apiKey.name || "N/A")}
                  />
                  <KeyOwnershipSummary apiKey={apiKey} />
                  
                  <div className="flex flex-wrap items-center justify-end gap-2 border-t pt-3">
                    {/* The server refuses a revoked, inactive or expired key and an add-on's
                        credential, so the control is offered only where a rotation can work. */}
                    {keyStatus === "Active" && !apiKey.is_add_on_credential && (
                      <RotateSecretButton
                        keyId={apiKey.id}
                        keyName={String(apiKey.name || apiKey.id)}
                        onRotated={(response) => setRotatedKeys((queue) => [...queue, response])}
                      />
                    )}
                    {/* An add-on's installation owns its grant, so the server refuses this edit. */}
                    {!apiKey.is_revoked && !apiKey.is_add_on_credential && (
                      <EditAPIKeyCapabilitiesDialog
                        apiKey={apiKey}
                        capabilities={capabilityData?.capabilities ?? []}
                        onSubmit={(keyId, capabilities, force) =>
                          updateCapabilitiesMutation.mutateAsync({ keyId, capabilities, force })
                        }
                        isSubmitting={updateCapabilitiesMutation.isPending}
                      />
                    )}
                    {!apiKey.is_revoked && (
                      <EditAPIKeyPermissionsDialog
                        apiKey={apiKey}
                        clusters={clusters}
                        onSubmit={(keyId, permissions) =>
                          updatePermissionsMutation.mutateAsync({ keyId, permissions })
                        }
                        isSubmitting={updatePermissionsMutation.isPending}
                      />
                    )}
                    {!apiKey.is_revoked && (
                      <AlertDialog>
                        <AlertDialogTrigger asChild>
                          <Button variant="outline" size="sm" className="text-yellow-600">
                            <Ban className="h-4 w-4 mr-1" />
                            Revoke
                          </Button>
                        </AlertDialogTrigger>
                        {ownsOrganizations ? (
                          <TransferOwnershipFirst apiKey={apiKey} action="revoke" isAdmin={isAdmin} />
                        ) : (
                          <AlertDialogContent>
                            <AlertDialogHeader>
                              <AlertDialogTitle>Revoke API Key?</AlertDialogTitle>
                              <AlertDialogDescription>
                                This will immediately revoke access. This action cannot be undone.
                              </AlertDialogDescription>
                            </AlertDialogHeader>
                            <AlertDialogFooter>
                              <AlertDialogCancel>Cancel</AlertDialogCancel>
                              <AlertDialogAction
                                onClick={() => revokeMutation.mutate(apiKey.id)}
                                className="bg-yellow-600"
                              >
                                Revoke
                              </AlertDialogAction>
                            </AlertDialogFooter>
                          </AlertDialogContent>
                        )}
                      </AlertDialog>
                    )}
                    
                    <AlertDialog>
                      <AlertDialogTrigger asChild>
                        <Button variant="outline" size="sm" className="text-red-600">
                          Delete
                        </Button>
                      </AlertDialogTrigger>
                      {ownsOrganizations ? (
                        <TransferOwnershipFirst apiKey={apiKey} action="delete" isAdmin={isAdmin} />
                      ) : (
                        <AlertDialogContent>
                          <AlertDialogHeader>
                            <AlertDialogTitle>Delete API Key?</AlertDialogTitle>
                            <AlertDialogDescription>
                              This will permanently delete this API key.
                            </AlertDialogDescription>
                          </AlertDialogHeader>
                          <AlertDialogFooter>
                            <AlertDialogCancel>Cancel</AlertDialogCancel>
                            <AlertDialogAction
                              onClick={() => deleteMutation.mutate(apiKey.id)}
                              className="bg-red-600"
                            >
                              Delete
                            </AlertDialogAction>
                          </AlertDialogFooter>
                        </AlertDialogContent>
                      )}
                    </AlertDialog>
                  </div>
                </CardContent>
              </Card>
            );
          })}
          </div>
          )}

          {/* note: 12, 24, 48 and 96 rather than the table set. The grid is one or two columns
              wide and every one of these divides by both, so a full page never ends in a lone
              card on its own row.
              Absent rather than disabled when everything fits. */}
          {totalPages > 1 && (
            <DataTablePagination
              currentPage={currentPage}
              totalPages={totalPages}
              totalItems={sortedKeys.length}
              pageSize={pageSize}
              pageSizeOptions={[...GRID_PAGE_SIZES]}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPage(1);
              }}
            />
          )}
        </div>
      )}

      {/* Secret Dialog */}
      <APIKeySecretDialog
        open={createdKey !== null}
        onClose={() => setCreatedKey(null)}
        response={createdKey}
      />
      <APIKeySecretDialog
        key={rotatedKeys[0]?.api_key_id ?? "none"}
        open={rotatedKeys.length > 0}
        onClose={() => setRotatedKeys((queue) => queue.slice(1))}
        response={rotatedKeys[0] ?? null}
        title="Secret Rotated"
        description="The previous secret no longer works, and the key's callback URL was cleared until the connection syncs again. Update the integration that uses this key, such as the OrcaHub connection, with this secret now. It will not be shown again."
      />
    </div>
  );
}

/**
 * The boundary the filter hook requires.
 *
 * useTableFilters reads the query string even with syncUrl off, which opts a client component
 * out of prerendering; Next then fails the build on the route unless a boundary sits above it.
 */
export function IntegrationsSettings() {
  return (
    <Suspense fallback={<Skeleton className="h-48 rounded-lg" />}>
      <IntegrationsSettingsContent />
    </Suspense>
  );
}
