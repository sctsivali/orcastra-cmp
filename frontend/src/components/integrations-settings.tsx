"use client";

import * as React from "react";
import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Key,
  Plus,
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
import { RotateSecretButton, isRotatable } from "@/components/integrations/rotate-secret-button";
import { KeyOwnershipSummary, TransferOwnershipFirst } from "@/components/integrations/key-ownership-summary";
import { OverflowText } from "@/components/ui/overflow-text";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
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
import { usePermissions } from "@/hooks/use-permissions";
import { api } from "@/lib/api";
import { getClusterAccessDisplay, summarizeClusterAccessLevel } from "@/lib/integration-display";
import { invalidateIntegrationQueries } from "@/lib/integration-queries";
import { keyOwnsOrganizationsDetail } from "@/lib/key-ownership-display";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ClusterAccessSelector } from "@/components/cluster-access-selector";
import type {
  APIKeyInfo,
  APIKeyCreateRequest,
  APIKeyCreateResponse,
  ClusterPermission,
  AvailableCluster,
} from "@/types";
import { copyText } from "@/lib/clipboard";

// Create API Key Dialog Component
function CreateAPIKeyDialog({
  clusters,
  onSubmit,
  isSubmitting,
}: {
  clusters: AvailableCluster[];
  onSubmit: (data: APIKeyCreateRequest) => void;
  isSubmitting: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [appName, setAppName] = useState("");
  const [appType, setAppType] = useState("orcahub");
  const [expireDays, setExpireDays] = useState<string>("never");
  const [selectedClusterIds, setSelectedClusterIds] = useState<string[]>([]);
  const [permissionLevel, setPermissionLevel] = useState("read");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    
    const permissions: ClusterPermission[] = selectedClusterIds.map(clusterId => ({
      cluster_id: clusterId,
      permissions: [permissionLevel],
      projects: ["*"],
    }));

    onSubmit({
      name,
      app_name: appName,
      app_type: appType,
      cluster_permissions: permissions,
      expire_days: expireDays === "never" ? null : parseInt(expireDays),
    });
    
    // Reset form
    setName("");
    setAppName("");
    setAppType("orcahub");
    setExpireDays("never");
    setSelectedClusterIds([]);
    setPermissionLevel("read");
    setOpen(false);
  };

  const toggleCluster = (clusterId: string) => {
    setSelectedClusterIds(prev => 
      prev.includes(clusterId) 
        ? prev.filter(id => id !== clusterId)
        : [...prev, clusterId]
    );
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm">
          <Plus className="h-4 w-4 mr-2" />
          Create API Key
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Create New API Key</DialogTitle>
          <DialogDescription>
            Generate an API key for external application integration.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit}>
          <div className="grid gap-4 py-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="name">Key Name</Label>
                <Input
                  id="name"
                  placeholder="e.g., Production Orcahub"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="app_name">Application Name</Label>
                <Input
                  id="app_name"
                  placeholder="e.g., Orcahub Production"
                  value={appName}
                  onChange={(e) => setAppName(e.target.value)}
                  required
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>Application Type</Label>
                <Select value={appType} onValueChange={setAppType}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="orcahub">Orcahub</SelectItem>
                    <SelectItem value="custom">Custom Application</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label>Expiration</Label>
                <Select value={expireDays} onValueChange={setExpireDays}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="never">Never expires</SelectItem>
                    <SelectItem value="30">30 days</SelectItem>
                    <SelectItem value="90">90 days</SelectItem>
                    <SelectItem value="365">1 year</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="space-y-2">
              <Label>Permission Level</Label>
              <Select value={permissionLevel} onValueChange={setPermissionLevel}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="read">Read Only</SelectItem>
                  <SelectItem value="write">Read & Write</SelectItem>
                  <SelectItem value="admin">Full Admin</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <ClusterAccessSelector
              clusters={clusters}
              selectedClusterIds={selectedClusterIds}
              onToggle={toggleCluster}
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={isSubmitting || !name || !appName || selectedClusterIds.length === 0}
            >
              {isSubmitting ? "Creating..." : "Create API Key"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

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
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
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
        </div>
        <DialogFooter>
          <Button onClick={onClose}>I have saved the credentials</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// Main Integrations Settings Component (for embedding in Settings page)
export function IntegrationsSettings() {
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

  // Revoke mutation
  const revokeMutation = useMutation({
    mutationFn: (keyId: string) => api.integrations.revokeAPIKey(keyId),
    onSuccess: () => {
      // note: a co-owned key leaves its owner sets as it ends, and a primary can move with it, so
      // the Synced Organizations panel rereads as well as the key list.
      void invalidateIntegrationQueries(queryClient).catch(() => undefined);
      toast({ title: "API Key Revoked" });
    },
    onError: (error: Error) => {
      // A key that came to own an organization after this list loaded is refused by the server,
      // and the reread puts its ownership on the card.
      if (keyOwnsOrganizationsDetail(error)) {
        void invalidateIntegrationQueries(queryClient).catch(() => undefined);
      }
      toast({ title: "Failed to revoke", description: error.message, variant: "destructive" });
    },
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (keyId: string) => api.integrations.deleteAPIKey(keyId),
    onSuccess: () => {
      void invalidateIntegrationQueries(queryClient).catch(() => undefined);
      toast({ title: "API Key Deleted" });
    },
    onError: (error: Error) => {
      if (keyOwnsOrganizationsDetail(error)) {
        void invalidateIntegrationQueries(queryClient).catch(() => undefined);
      }
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
            onSubmit={(data) => createMutation.mutate(data)}
            isSubmitting={createMutation.isPending}
          />
        </div>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
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
                onSubmit={(data) => createMutation.mutate(data)}
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
                      {apiKey.is_revoked ? (
                        <Badge variant="destructive">Revoked</Badge>
                      ) : apiKey.is_active ? (
                        <Badge className="bg-green-600">Active</Badge>
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
                    </div>
                  </div>
                  <IntegrationClusterAccessList
                    clusters={clusterAccess}
                    keyName={String(apiKey.name || "N/A")}
                  />
                  <KeyOwnershipSummary apiKey={apiKey} />

                  <div className="flex items-center justify-end gap-2 border-t pt-3">
                    {isRotatable(apiKey) && (
                      <RotateSecretButton
                        keyId={apiKey.id}
                        keyName={String(apiKey.name || apiKey.id)}
                        onRotated={(response) => setRotatedKeys((queue) => [...queue, response])}
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
        description="The previous secret no longer works, and the key's callback URL was cleared until the connection syncs again. Update the OrcaHub connection with this secret now. It will not be shown again."
      />
    </div>
  );
}
