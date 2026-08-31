"use client";

import * as React from "react";
import { useState, useEffect } from "react";
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
import {
  formatApplicationType,
  getClusterAccessDisplay,
  isLegacyUnscopedKey,
  summarizeClusterAccessLevel,
} from "@/lib/integration-display";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { EditAPIKeyPermissionsDialog } from "@/components/integrations/edit-api-key-permissions-dialog";
import { CreateAPIKeyDialog } from "@/components/integrations/create-api-key-dialog";
import { ApiKeyConnectionGuide } from "@/components/integrations/api-key-connection-guide";
import type {
  APIKeyInfo,
  APIKeyCreateRequest,
  APIKeyCreateResponse,
  AvailableCluster,
  ClusterPermission,
} from "@/types";
import { copyText } from "@/lib/clipboard";

// API Key Secret Dialog
function APIKeySecretDialog({
  open,
  onClose,
  response,
}: {
  open: boolean;
  onClose: () => void;
  response: APIKeyCreateResponse | null;
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
          <DialogTitle>
            API Key Created Successfully
          </DialogTitle>
          <DialogDescription>
            Save these credentials now. The secret will not be shown again.
          </DialogDescription>
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
              <p className="font-medium text-yellow-600">⚠️ Important</p>
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
export function IntegrationsSettings() {
  const queryClient = useQueryClient();
  const [mounted, setMounted] = useState(false);
  const [createdKey, setCreatedKey] = useState<APIKeyCreateResponse | null>(null);

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
    queryFn: ({ fresh }) => api.integrations.listAPIKeys({ fresh }),
    enabled: mounted,
    staleTime: 30000,
    retry: (failureCount, error) => {
      // Don't retry on 503 (Vault not configured) — it won't resolve on its own
      if (error instanceof Error && "status" in error && (error as Record<string, unknown>).status === 503) return false;
      return failureCount < 2;
    },
  });

  // Detect Vault-not-configured state (duck-type check — instanceof breaks across chunks)
  const vaultUnavailable =
    keysError instanceof Error &&
    "status" in keysError &&
    (keysError as Record<string, unknown>).status === 503;

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
      const is503 = "status" in error && (error as Record<string, unknown>).status === 503;
      toast({
        title: "Failed to create API key",
        description: is503
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

  // Revoke mutation
  const revokeMutation = useMutation({
    mutationFn: (keyId: string) => api.integrations.revokeAPIKey(keyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["integrations", "api-keys"] });
      toast({ title: "API Key Revoked" });
    },
    onError: (error: Error) => {
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
      toast({ title: "Failed to delete", description: error.message, variant: "destructive" });
    },
  });

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

  // Safely extract data with defaults
  const apiKeys: APIKeyInfo[] = Array.isArray(apiKeysData?.keys) ? apiKeysData.keys : [];
  const clusters: AvailableCluster[] = Array.isArray(clustersData?.clusters) ? clustersData.clusters : [];
  const totalKeys = typeof apiKeysData?.total === 'number' ? apiKeysData.total : 0;
  const activeKeys = typeof apiKeysData?.active === 'number' ? apiKeysData.active : 0;
  const revokedKeys = typeof apiKeysData?.revoked === 'number' ? apiKeysData.revoked : 0;
  const expiredKeys = typeof apiKeysData?.expired === 'number' ? apiKeysData.expired : 0;

  // Vault not configured — show informative banner instead of full UI
  if (vaultUnavailable) {
    return (
      <div className="space-y-6">
        <div>
          <h2 className="text-2xl font-bold">Integrations</h2>
          <p className="text-sm text-muted-foreground">
            Manage API keys for external applications
          </p>
        </div>
        <Alert variant="destructive" className="border-yellow-500/50 bg-yellow-500/10 text-yellow-200 [&>svg]:text-yellow-500">
          <ShieldAlert className="h-5 w-5" />
          <AlertTitle className="text-yellow-400 font-semibold">Vault Not Configured</AlertTitle>
          <AlertDescription className="text-yellow-200/80 mt-1 space-y-2">
            <p>
              API key management requires HashiCorp Vault to be enabled and configured.
              Keys are securely stored in Vault&apos;s encrypted secret engine.
            </p>
            <p className="text-xs text-yellow-200/60">
              Set <code className="bg-yellow-500/20 px-1.5 py-0.5 rounded text-yellow-300">VAULT_ENABLED=true</code>,{" "}
              <code className="bg-yellow-500/20 px-1.5 py-0.5 rounded text-yellow-300">VAULT_ADDR</code>, and{" "}
              <code className="bg-yellow-500/20 px-1.5 py-0.5 rounded text-yellow-300">VAULT_TOKEN</code>{" "}
              in the backend environment, then restart the backend service.
            </p>
          </AlertDescription>
        </Alert>
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
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {apiKeys.map((apiKey) => {
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
                      {apiKey.is_revoked ? (
                        <Badge variant="destructive">Revoked</Badge>
                      ) : expiryState === "expired" ? (
                        // Six keys on this deployment read a green "Active" while lapsed, which
                        // is the distinction that was actually missing.
                        //
                        // note: Expired shares the red family with Revoked deliberately. Both
                        // mean the key does not work, and giving the same outcome two colours
                        // would be decoration; the word carries the cause, which is the part
                        // that differs. The tones are the solid --status-* ones whose ratios
                        // ui/badge.tsx records for both themes, not the translucent tints it
                        // measures at 2.73:1 on --card in light mode.
                        <Badge variant="negative">Expired</Badge>
                      ) : apiKey.is_active ? (
                        <Badge variant="positive">Active</Badge>
                      ) : (
                        <Badge variant="secondary">Inactive</Badge>
                      )}
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
                      <Badge variant="warning" className="gap-1">
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
                  
                  <div className="flex items-center justify-end gap-2 border-t pt-3">
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
                      </AlertDialog>
                    )}
                    
                    <AlertDialog>
                      <AlertDialogTrigger asChild>
                        <Button variant="outline" size="sm" className="text-red-600">
                          Delete
                        </Button>
                      </AlertDialogTrigger>
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
                    </AlertDialog>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* Secret Dialog */}
      <APIKeySecretDialog
        open={createdKey !== null}
        onClose={() => setCreatedKey(null)}
        response={createdKey}
      />
    </div>
  );
}
