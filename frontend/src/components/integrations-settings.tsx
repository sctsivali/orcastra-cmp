"use client";

import * as React from "react";
import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Key,
  Plus,
  RefreshCw,
  Copy,
  CheckCircle2,
  XCircle,
  Ban,
  Users,
  Server,
  Eye,
  EyeOff,
} from "lucide-react";
import { Button } from "@/components/ui/button";
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
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  APIKeyInfo,
  APIKeyCreateRequest,
  APIKeyCreateResponse,
  ClusterPermission,
  AvailableCluster,
} from "@/types";

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

  const safeClusterList = Array.isArray(clusters) ? clusters : [];

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

            <div className="space-y-2">
              <Label>Cluster Access</Label>
              <div className="h-48 border rounded-md p-3 overflow-y-auto">
                {safeClusterList.length === 0 ? (
                  <p className="text-sm text-muted-foreground text-center py-4">
                    No clusters available.
                  </p>
                ) : (
                  <div className="space-y-2">
                    {safeClusterList.map((cluster) => {
                      if (!cluster || typeof cluster.id !== 'string') return null;
                      const clusterId = String(cluster.id);
                      const clusterName = String(cluster.name || '');
                      const clusterEndpoint = String(cluster.endpoint || '');
                      const clusterType = String(cluster.server_type || 'unknown');
                      const isSelected = selectedClusterIds.includes(clusterId);
                      
                      return (
                        <label
                          key={clusterId}
                          htmlFor={`cluster-${clusterId}`}
                          className={cn(
                            "flex items-center justify-between p-2 rounded-md cursor-pointer transition-colors",
                            isSelected
                              ? "bg-primary/10 border border-primary"
                              : "bg-muted/50 hover:bg-muted"
                          )}
                        >
                          <div className="flex items-center gap-3">
                            <input
                              type="checkbox"
                              id={`cluster-${clusterId}`}
                              checked={isSelected}
                              onChange={() => toggleCluster(clusterId)}
                              className="h-4 w-4 rounded border-primary text-primary focus:ring-primary"
                            />
                            <div>
                              <p className="font-medium text-sm">{clusterName}</p>
                              <p className="text-xs text-muted-foreground font-mono">
                                {clusterEndpoint}
                              </p>
                            </div>
                          </div>
                          <Badge variant="outline" className="text-xs">
                            {clusterType}
                          </Badge>
                        </label>
                      );
                    })}
                  </div>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                Selected: {selectedClusterIds.length} cluster(s)
              </p>
            </div>
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
}: {
  open: boolean;
  onClose: () => void;
  response: APIKeyCreateResponse | null;
}) {
  const [showSecret, setShowSecret] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  const copyToClipboard = async (text: string, type: string) => {
    await navigator.clipboard.writeText(text);
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
          <DialogTitle className="flex items-center gap-2">
            <CheckCircle2 className="h-5 w-5 text-green-500" />
            API Key Created Successfully
          </DialogTitle>
          <DialogDescription>
            Save these credentials now. The secret will not be shown again.
          </DialogDescription>
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
          <Button onClick={onClose}>I have Saved the Credentials</Button>
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
    refetch: refetchKeys,
    isRefetching,
  } = useQuery({
    queryKey: ["integrations", "api-keys"],
    queryFn: () => api.integrations.listAPIKeys(),
    enabled: mounted,
    staleTime: 30000,
  });

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
      toast({
        title: "Failed to create API key",
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

  return (
    <div className="space-y-6">
      {/* Header with actions */}
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Manage API keys for external applications
        </p>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetchKeys()}
            disabled={isRefetching}
          >
            <RefreshCw className={cn("h-4 w-4 mr-2", isRefetching && "animate-spin")} />
            Refresh
          </Button>
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
                <p className="text-2xl font-bold text-green-600">{activeKeys}</p>
              </div>
              <CheckCircle2 className="h-8 w-8 text-green-600" />
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-muted-foreground">Revoked Keys</p>
                <p className="text-2xl font-bold text-red-600">{revokedKeys}</p>
              </div>
              <XCircle className="h-8 w-8 text-red-600" />
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
                <h3 className="text-lg font-medium">No API Keys</h3>
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
        <div className="grid gap-4 md:grid-cols-2">
          {apiKeys.map((apiKey) => {
            if (!apiKey || typeof apiKey.id !== 'string') return null;
            const clusterCount = Array.isArray(apiKey.cluster_permissions) ? apiKey.cluster_permissions.length : 0;
            const userCount = typeof apiKey.user_mappings_count === 'number' ? apiKey.user_mappings_count : 0;
            
            return (
              <Card key={apiKey.id} className={cn(apiKey.is_revoked && "opacity-60")}>
                <CardHeader className="pb-3">
                  <div className="flex items-start justify-between">
                    <div className="space-y-1">
                      <CardTitle className="text-lg flex items-center gap-2">
                        <Key className="h-5 w-5" />
                        {String(apiKey.name || 'Unnamed')}
                      </CardTitle>
                      <CardDescription className="flex items-center gap-2">
                        <span className="font-mono text-xs bg-muted px-2 py-0.5 rounded">
                          {String(apiKey.id)}
                        </span>
                        <span>•</span>
                        <span>{String(apiKey.app_name || '')}</span>
                      </CardDescription>
                    </div>
                    <div>
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
                  <div className="grid grid-cols-2 gap-4 text-sm">
                    <div className="flex items-center gap-2">
                      <Server className="h-4 w-4 text-muted-foreground" />
                      <span className="text-muted-foreground">Clusters:</span>
                      <span className="font-medium">{clusterCount}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <Users className="h-4 w-4 text-muted-foreground" />
                      <span className="text-muted-foreground">Users:</span>
                      <span className="font-medium">{userCount}</span>
                    </div>
                  </div>
                  
                  <div className="flex items-center justify-end gap-2 border-t pt-3">
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
