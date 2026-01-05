"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { LoadingLottieMedium } from "@/components/loading-lottie";
import { 
  Server, 
  CheckCircle, 
  XCircle, 
  Clock, 
  AlertTriangle,
  RefreshCw,
  Trash2,
  Plus,
  ExternalLink,
  Users,
  KeyRound,
  Copy,
  Terminal
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Label } from "@/components/ui/label";
import { toast } from "@/hooks/use-toast";
import { ClusterStatus, type ClusterInfo } from "@/types/api";
import { useClusters, useCheckClusterAuth, useDeleteCluster, useRegenerateCert } from "@/hooks/use-lxd";

const statusConfig = {
  [ClusterStatus.ONLINE]: {
    label: "Online",
    variant: "default" as const,
    icon: CheckCircle,
    color: "text-green-500",
  },
  [ClusterStatus.NOT_AUTHORIZED]: {
    label: "Pending Trust",
    variant: "secondary" as const,
    icon: Clock,
    color: "text-amber-500",
  },
  [ClusterStatus.OFFLINE]: {
    label: "Offline",
    variant: "destructive" as const,
    icon: XCircle,
    color: "text-red-500",
  },
  [ClusterStatus.ERROR]: {
    label: "Error",
    variant: "destructive" as const,
    icon: AlertTriangle,
    color: "text-red-500",
  },
  [ClusterStatus.PENDING]: {
    label: "Pending",
    variant: "secondary" as const,
    icon: Clock,
    color: "text-gray-500",
  },
};

export default function ClustersPage() {
  const router = useRouter();
  const { data, isLoading, refetch, isRefetching } = useClusters();
  const clusters = (data as any)?.clusters || [];
  const [checkingAuth, setCheckingAuth] = React.useState<string | null>(null);
  const [regenerateModal, setRegenerateModal] = React.useState<{open: boolean, cluster: any | null}>({open: false, cluster: null});
  const [regeneratedCert, setRegeneratedCert] = React.useState<{identityName: string, certificate: string} | null>(null);
  
  const checkClusterAuth = useCheckClusterAuth();
  const deleteCluster = useDeleteCluster();
  const regenerateCert = useRegenerateCert();

  const handleCheckAuth = async (clusterId: string) => {
    setCheckingAuth(clusterId);
    try {
      const response = await checkClusterAuth.mutateAsync(clusterId);
      
      if (response.authenticated) {
        toast({
          title: "Authentication Successful",
          description: `Cluster ${clusterId} is online and authenticated`,
        });
        // Reload list to update status
        await refetch();
      } else {
        toast({
          title: "Not Authenticated",
          description: response.message || "Certificate not yet trusted",
          variant: "destructive",
        });
      }
    } catch (error: any) {
      toast({
        title: "Check Failed",
        description: error.message || "Failed to check authentication",
        variant: "destructive",
      });
    } finally {
      setCheckingAuth(null);
    }
  };

  const generateIdentityName = (clusterId: string): string => {
    const now = new Date();
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    const hours = String(now.getHours()).padStart(2, '0');
    const minutes = String(now.getMinutes()).padStart(2, '0');
    const seconds = String(now.getSeconds()).padStart(2, '0');
    const millis = String(now.getMilliseconds()).padStart(3, '0');
    const randomSuffix = Math.random().toString(36).substring(2, 6);
    const datetime = `${year}${month}${day}-${hours}${minutes}${seconds}${millis}-${randomSuffix}`;
    return `orca-${clusterId}-${datetime}`;
  };

  const handleRegenerateCert = async (cluster: any) => {
    try {
      const response = await regenerateCert.mutateAsync(cluster.cluster_id);
      
      if (response.success && response.certificate) {
        const identityName = generateIdentityName(cluster.cluster_id);
        setRegeneratedCert({
          identityName,
          certificate: response.certificate
        });
        setRegenerateModal({open: true, cluster});
        toast({
          title: "Certificate Regenerated",
          description: "Copy and run the command on your LXD server to add trust",
        });
      }
    } catch (error: any) {
      toast({
        title: "Regenerate Failed",
        description: error.message || "Failed to regenerate certificate",
        variant: "destructive",
      });
    }
  };

  const copyToClipboard = async (text: string, label: string) => {
    try {
      // Log what we're trying to copy for debugging
      console.log('Copying to clipboard:', text.substring(0, 100) + '...');
      
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        console.log('Successfully copied using Clipboard API');
        toast({
          title: "Copied!",
          description: `${label} copied to clipboard`,
        });
      } else {
        // Fallback for HTTP or older browsers
        const textArea = document.createElement('textarea');
        textArea.value = text;
        // Make sure textarea is visible but offscreen
        textArea.style.position = 'fixed';
        textArea.style.top = '0';
        textArea.style.left = '0';
        textArea.style.width = '2em';
        textArea.style.height = '2em';
        textArea.style.padding = '0';
        textArea.style.border = 'none';
        textArea.style.outline = 'none';
        textArea.style.boxShadow = 'none';
        textArea.style.background = 'transparent';
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        
        try {
          const successful = document.execCommand('copy');
          textArea.remove();
          
          if (successful) {
            console.log('Successfully copied using execCommand');
            toast({
              title: "Copied!",
              description: `${label} copied to clipboard`,
            });
          } else {
            throw new Error('execCommand returned false');
          }
        } catch (err) {
          console.error('Copy failed:', err);
          textArea.remove();
          toast({
            title: "Copy Failed",
            description: "Please select and copy the text manually (Ctrl+C or Cmd+C)",
            variant: "destructive",
          });
        }
      }
    } catch (error) {
      console.error('Copy error:', error);
      toast({
        title: "Copy Failed",
        description: "Please select and copy the text manually (Ctrl+C or Cmd+C)",
        variant: "destructive",
      });
    }
  };

  const handleDelete = async (clusterId: string) => {
    if (!confirm(`Are you sure you want to remove cluster ${clusterId}?`)) {
      return;
    }

    try {
      await deleteCluster.mutateAsync(clusterId);
      toast({
        title: "Cluster Removed",
        description: `Cluster ${clusterId} has been removed`,
      });
      await new Promise(resolve => setTimeout(resolve, 100));
      await refetch();
    } catch (error: any) {
      toast({
        title: "Delete Failed",
        description: error.message || "Failed to remove cluster",
        variant: "destructive",
      });
    }
  };

  return (
    <div className="container py-8">
      {/* Regenerate Certificate Modal */}
      <Dialog open={regenerateModal.open} onOpenChange={(open) => {
        if (!open) {
          setRegenerateModal({open: false, cluster: null});
          setRegeneratedCert(null);
        }
      }}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <KeyRound className="h-5 w-5 text-amber-500" />
              Certificate Regenerated
            </DialogTitle>
            <DialogDescription>
              Add trust to complete the authentication for {regenerateModal.cluster?.display_name}
            </DialogDescription>
          </DialogHeader>
          
          {regeneratedCert && (
            <div className="space-y-4">
              <Alert>
                <Terminal className="h-4 w-4" />
                <AlertDescription className="space-y-3">
                  <p className="text-sm font-medium">
                    Run this command on your hypervisor server:
                  </p>
                  <div className="space-y-2">
                    <div className="flex items-center justify-between">
                      <p className="text-xs text-muted-foreground">
                        Add trust with unique identity name <code className="bg-muted px-1 py-0.5 rounded">{regeneratedCert.identityName}</code>
                      </p>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          const cmd = `lxc config trust add --name ${regeneratedCert.identityName} - <<EOF\n${regeneratedCert.certificate}\nEOF`;
                          copyToClipboard(cmd, "Command");
                        }}
                      >
                        <Copy className="h-3 w-3 mr-1" />
                        Copy
                      </Button>
                    </div>
                    <pre className="bg-black text-green-400 p-3 rounded text-xs overflow-x-auto select-all cursor-text" onClick={(e) => {
                      // Allow text selection by clicking anywhere on the pre
                      const selection = window.getSelection();
                      const range = document.createRange();
                      range.selectNodeContents(e.currentTarget);
                      selection?.removeAllRanges();
                      selection?.addRange(range);
                    }}>
{`lxc config trust add --name ${regeneratedCert.identityName} - <<EOF
${regeneratedCert.certificate}
EOF`}
                    </pre>
                  </div>
                </AlertDescription>
              </Alert>
              
              <div className="flex gap-3">
                <Button
                  onClick={async () => {
                    if (regenerateModal.cluster) {
                      setCheckingAuth(regenerateModal.cluster.cluster_id);
                      try {
                        const response = await checkClusterAuth.mutateAsync(regenerateModal.cluster.cluster_id);
                        
                        if (response.authenticated) {
                          toast({
                            title: "Authentication Successful",
                            description: `Cluster ${regenerateModal.cluster.cluster_id} is now online`,
                          });
                          // Only close modal if authentication successful
                          setRegenerateModal({open: false, cluster: null});
                          setRegeneratedCert(null);
                          await refetch();
                        } else {
                          toast({
                            title: "Not Authenticated Yet",
                            description: response.message || "Certificate not yet trusted. Please run the command on your LXD server.",
                            variant: "destructive",
                          });
                          // Keep modal open for retry
                        }
                      } catch (error: any) {
                        toast({
                          title: "Check Failed",
                          description: error.message || "Failed to check authentication",
                          variant: "destructive",
                        });
                        // Keep modal open for retry
                      } finally {
                        setCheckingAuth(null);
                      }
                    }
                  }}
                  disabled={checkingAuth === regenerateModal.cluster?.cluster_id}
                  className="flex-1 bg-gradient-to-r from-blue-500 to-blue-600 hover:from-blue-600 hover:to-blue-700"
                >
                  {checkingAuth === regenerateModal.cluster?.cluster_id ? (
                    <>
                      <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                      Checking...
                    </>
                  ) : (
                    <>
                      <RefreshCw className="h-4 w-4 mr-2" />
                      Verify Connection
                    </>
                  )}
                </Button>
                <Button
                  variant="outline"
                  onClick={() => {
                    setRegenerateModal({open: false, cluster: null});
                    setRegeneratedCert(null);
                  }}
                >
                  Close
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold">Registered Clusters</h1>
          <p className="text-muted-foreground mt-1">
            Manage hypervisor clusters
          </p>
        </div>
        <Button
          onClick={() => router.push("/register-node")}
          className="gap-2 bg-gradient-to-r from-violet-500 to-purple-600 hover:from-violet-600 hover:to-purple-700 shrink-0"
          size="sm"
        >
          <Plus className="h-4 w-4" />
          <span className="hidden sm:inline">Register Cluster</span>
          <span className="sm:hidden">Register</span>
        </Button>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Cluster Overview</CardTitle>
              <CardDescription>
                {clusters.length} {clusters.length === 1 ? "cluster" : "clusters"} registered
              </CardDescription>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => refetch()}
              disabled={isRefetching}
            >
              <RefreshCw className={`h-4 w-4 mr-2 ${isRefetching ? "animate-spin" : ""}`} />
              Refresh
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <LoadingLottieMedium />
            </div>
          ) : clusters.length === 0 ? (
            <div className="text-center py-12">
              <Server className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
              <h3 className="text-lg font-semibold mb-2">No Clusters Registered</h3>
              <p className="text-muted-foreground mb-4">
                Get started by registering your first hypervisor cluster
              </p>
              <Button
                onClick={() => router.push("/register-node")}
                className="gap-2"
              >
                <Plus className="h-4 w-4" />
                Register First Cluster
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Cluster</TableHead>
                  <TableHead>Endpoint</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Zone</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {clusters.map((cluster: any) => {
                  const StatusIcon = statusConfig[cluster.status as ClusterStatus]?.icon || AlertTriangle;
                  return (
                    <TableRow key={cluster.cluster_id}>
                      <TableCell className="font-medium">
                        <div className="flex items-center gap-2">
                          <Server className="h-4 w-4 text-muted-foreground" />
                          <div>
                            <div className="font-semibold">{cluster.display_name}</div>
                            <div className="text-xs text-muted-foreground">
                              {cluster.cluster_id}
                            </div>
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>
                        <a
                          href={cluster.endpoint}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-sm hover:underline flex items-center gap-1"
                        >
                          {cluster.endpoint}
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">{cluster.cluster_type}</Badge>
                      </TableCell>
                      <TableCell>
                        {cluster.zone ? (
                          <span className="text-sm">{cluster.zone}</span>
                        ) : (
                          <span className="text-sm text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <Badge variant={statusConfig[cluster.status as ClusterStatus]?.variant || "secondary"}>
                          <StatusIcon className="h-3 w-3 mr-1" />
                          {statusConfig[cluster.status as ClusterStatus]?.label || "Unknown"}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-2">
                          {cluster.cluster_type === "MicroCloud" && cluster.status === "online" && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => router.push(`/inventory/clustering/members?node=${cluster.cluster_id}`)}
                              title="View Cluster Members"
                            >
                              <Users className="h-4 w-4" />
                            </Button>
                          )}
                          {cluster.status === ClusterStatus.NOT_AUTHORIZED && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => handleRegenerateCert(cluster)}
                              title="Regenerate Certificate"
                              className="text-amber-600 hover:text-amber-700"
                            >
                              <KeyRound className="h-4 w-4" />
                            </Button>
                          )}
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => handleCheckAuth(cluster.cluster_id)}
                            disabled={checkingAuth === cluster.cluster_id}
                            title="Check Authentication"
                          >
                            {checkingAuth === cluster.cluster_id ? (
                              <RefreshCw className="h-4 w-4 animate-spin" />
                            ) : (
                              <RefreshCw className="h-4 w-4" />
                            )}
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => handleDelete(cluster.cluster_id)}
                            title="Delete Cluster"
                          >
                            <Trash2 className="h-4 w-4 text-destructive" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
