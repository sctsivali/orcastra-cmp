"use client";

import { useState, useMemo } from "react";
import { useRouter } from "next/navigation";
import { CheckCircle2, XCircle, Server, RefreshCw, Cloud, Cpu, MemoryStick, Sparkles, MoreVertical, Edit, Users } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { LXDVersionChecker } from "./lxd-version-checker";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NodeStatus } from "@/types";
import { useNodesStatus, useUpdateNodeTags, useUpdateNodeDisplayName } from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { cn } from "@/lib/utils";
import { NodeDetailModal } from "./node-detail-modal";
import { TagManager, TagFilter } from "@/components/tag-manager";
import { toast } from "@/hooks/use-toast";

interface NodeStatusCardProps {
  node: NodeStatus;
  index: number;
  onViewDetails: (node: NodeStatus) => void;
  onTagsUpdate?: () => void;
}

function NodeStatusCard({ node, index, onViewDetails, onTagsUpdate }: NodeStatusCardProps) {
  const router = useRouter();
  const updateNodeTags = useUpdateNodeTags();
  const updateNodeDisplayName = useUpdateNodeDisplayName();
  const { isReadOnly } = useReadOnlyMode();
  const [editNameDialogOpen, setEditNameDialogOpen] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [newDisplayName, setNewDisplayName] = useState(node.display_name || node.name);
  const [isSavingName, setIsSavingName] = useState(false);

  const handleSaveDisplayName = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Node name updates are disabled in read-only mode",
        variant: "destructive",
      });
      return;
    }
    setIsSavingName(true);
    try {
      await updateNodeDisplayName.mutateAsync({
        nodeName: node.name,
        displayName: newDisplayName,
      });
      // Close dialog first before showing toast and refetch
      setEditNameDialogOpen(false);
      // Small delay to ensure dialog is fully closed
      setTimeout(() => {
        toast({
          title: "Name Updated",
          description: `Display name for node '${node.name}' has been updated to '${newDisplayName}'.`,
          variant: "success",
        });
        onTagsUpdate?.();
      }, 100);
    } catch (error) {
      toast({
        title: "Failed to update name",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setIsSavingName(false);
    }
  };

  const handleTagsChange = async (newTags: string[]) => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Tag updates are disabled in read-only mode",
        variant: "destructive",
      });
      return;
    }
    try {
      await updateNodeTags.mutateAsync({
        nodeName: node.name,
        tags: newTags,
      });
      toast({
        title: "Tags Updated",
        description: `Tags for node '${node.name}' have been updated.`,
        variant: "success",
      });
      onTagsUpdate?.();
    } catch (error) {
      toast({
        title: "Failed to update tags",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    }
  };
  const formatBytes = (bytes: number) => {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
  };

  const getUsagePercentage = (used: number, total: number) => {
    if (total === 0) return 0;
    return ((used / total) * 100).toFixed(0);
  };

  // helper to get display name for node type
  const getNodeTypeDisplay = (nodeType: string | null | undefined): string => {
    if (!nodeType) return "LXD";
    const type = nodeType.toLowerCase();
    if (type === "incus") return "Incus";
    if (type === "microcloud") return "MicroCloud";
    return "LXD";
  };

  const NodeIcon = node.node_type?.toLowerCase() === "microcloud" ? Cloud : Server;

  return (
    <Card
      className={cn(
        "animate-fade-in overflow-hidden transition-all duration-300 hover:shadow-md",
        node.connected
          ? "border-emerald-200 dark:border-emerald-900"
          : "border-red-200 dark:border-red-900"
      )}
      style={{ animationDelay: `${index * 100}ms` }}
    >
      <div
        className={cn(
          "h-1 w-full",
          node.connected
            ? "bg-gradient-to-r from-emerald-400 to-emerald-600"
            : "bg-gradient-to-r from-red-400 to-red-600"
        )}
      />
      <CardContent className="p-3 md:p-4">
        <div className="flex flex-col sm:flex-row items-start justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0 flex-1">
            <div
              className={cn(
                "flex h-10 w-10 items-center justify-center rounded-lg shrink-0",
                node.connected
                  ? "bg-emerald-100 text-emerald-600 dark:bg-emerald-950 dark:text-emerald-400"
                  : "bg-red-100 text-red-600 dark:bg-red-950 dark:text-red-400"
              )}
            >
              <NodeIcon className="h-5 w-5" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <h3 className="font-semibold truncate">{node.display_name || node.name}</h3>
                {node.node_type && (
                  <Badge variant="outline" className="text-[10px] px-1.5 py-0 h-4">
                    {getNodeTypeDisplay(node.node_type)}
                  </Badge>
                )}
              </div>
              {node.display_name && (
                <p className="text-xs text-muted-foreground truncate">
                  {node.name}
                </p>
              )}
              <p className="text-xs text-muted-foreground font-mono truncate">
                {node.endpoint}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0 self-start sm:self-auto">
            {node.connected ? (
              <Badge variant="success" className="gap-1">
                <CheckCircle2 className="h-3 w-3" />
                Connected
              </Badge>
            ) : (
              <Badge variant="error" className="gap-1">
                <XCircle className="h-3 w-3" />
                Offline
              </Badge>
            )}
            <div onClick={(e) => e.stopPropagation()}>
              <DropdownMenu open={dropdownOpen} onOpenChange={setDropdownOpen}>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="icon" className="h-7 w-7">
                    <MoreVertical className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem 
                    className="cursor-pointer"
                    onSelect={(e) => {
                      e.preventDefault();
                      setNewDisplayName(node.display_name || node.name);
                      setEditNameDialogOpen(true);
                      setDropdownOpen(false); // Close dropdown when opening dialog
                    }}
                  >
                    <Edit className="mr-2 h-4 w-4" />
                    Edit Name
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </div>
        </div>
        {node.connected && node.server_version && (
          <div className="mt-3 flex flex-wrap gap-2 md:gap-4 text-xs text-muted-foreground items-center">
            <span>
              <span className="font-medium">Version:</span>{" "}
              {node.server_version}
            </span>
            {node.api_version && (
              <span>
                <span className="font-medium">Hypervisor API:</span> {getNodeTypeDisplay(node.node_type)} {node.api_version}
              </span>
            )}
            {/* LXD/Incus Version checker - works for LXD, MicroCloud, and Incus */}
            <LXDVersionChecker currentVersion={node.server_version} nodeType={node.node_type ?? undefined} />
          </div>
        )}
        {node.connected && node.resources && (
          <div className="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
            {/* CPU Info */}
            <div className="flex flex-col gap-1">
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Cpu className="h-3.5 w-3.5" />
                <span className="font-medium">CPU</span>
              </div>
              <div className="text-xs">
                <div className="font-semibold text-foreground">
                  {node.resources.cpu_used} / {node.resources.cpu_total} cores
                </div>
                <div className="text-muted-foreground">
                  {getUsagePercentage(node.resources.cpu_used, node.resources.cpu_total)}% used
                </div>
              </div>
            </div>

            {/* RAM Info */}
            <div className="flex flex-col gap-1">
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <MemoryStick className="h-3.5 w-3.5" />
                <span className="font-medium">RAM</span>
              </div>
              <div className="text-xs">
                <div className="font-semibold text-foreground">
                  {formatBytes(node.resources.memory_used)} / {formatBytes(node.resources.memory_total)}
                </div>
                <div className="text-muted-foreground">
                  {getUsagePercentage(node.resources.memory_used, node.resources.memory_total)}% used
                </div>
              </div>
            </div>

            {/* GPU Info */}
            <div className="flex flex-col gap-1">
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Sparkles className="h-3.5 w-3.5" />
                <span className="font-medium">GPU</span>
              </div>
              <div className="text-xs">
                <div className="font-semibold text-foreground">
                  {node.resources.gpu_used} / {node.resources.gpu_total} cards
                </div>
                <div className="text-muted-foreground">
                  {node.resources.gpu_total > 0 
                    ? `${getUsagePercentage(node.resources.gpu_used, node.resources.gpu_total)}% used`
                    : "No GPU"
                  }
                </div>
              </div>
            </div>
          </div>
        )}
        {!node.connected && node.error && (
          <p className="mt-3 text-xs text-red-600 dark:text-red-400">
            {node.error}
          </p>
        )}

        {/* Tags */}
        <div 
          className="mt-3 pt-3 border-t"
          onClick={(e) => e.stopPropagation()}
        >
          <TagManager
            tags={node.tags || []}
            onTagsChange={handleTagsChange}
            readonly={isReadOnly}
            maxTags={10}
            placeholder="Add node tag..."
          />
        </div>

        {/* View Members Button for MicroCloud */}
        {node.node_type?.toLowerCase() === "microcloud" && node.connected && (
          <div className="mt-3 pt-3 border-t">
            <Button
              variant="outline"
              size="sm"
              className="w-full gap-2"
              onClick={(e) => {
                e.stopPropagation();
                router.push(`/inventory/clustering/members?node=${node.name}`);
              }}
            >
              <Users className="h-4 w-4" />
              View Cluster Members
            </Button>
          </div>
        )}

        {/* View Details Button */}
        <div className="mt-3">
          <Button
            variant="ghost"
            size="sm"
            className="w-full"
            onClick={(e) => {
              e.stopPropagation();
              onViewDetails(node);
            }}
          >
            View Details
          </Button>
        </div>
      </CardContent>

      {/* Edit Name Dialog */}
      <Dialog open={editNameDialogOpen} onOpenChange={setEditNameDialogOpen}>
        <DialogContent className="sm:max-w-[425px]" onClick={(e) => e.stopPropagation()}>
          <DialogHeader>
            <DialogTitle>Edit Node Display Name</DialogTitle>
            <DialogDescription>
              Change the display name for node {node.name}. The original node name will remain unchanged.
            </DialogDescription>
          </DialogHeader>
          
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="node-name">Display Name</Label>
              <Input
                id="node-name"
                value={newDisplayName}
                onChange={(e) => setNewDisplayName(e.target.value)}
                placeholder="Enter display name"
              />
              <p className="text-xs text-muted-foreground">
                Original name: <span className="font-mono">{node.name}</span>
              </p>
            </div>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setEditNameDialogOpen(false)}
              disabled={isSavingName}
            >
              Cancel
            </Button>
            <Button onClick={handleSaveDisplayName} disabled={isSavingName || !newDisplayName.trim()}>
              {isSavingName ? "Saving..." : "Save Changes"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function LoadingSkeleton() {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {[...Array(2)].map((_, i) => (
        <Card key={i} className="overflow-hidden">
          <Skeleton className="h-1 w-full" />
          <CardContent className="p-4">
            <div className="flex items-start justify-between">
              <div className="flex items-center gap-3">
                <Skeleton className="h-10 w-10 rounded-lg" />
                <div className="space-y-2">
                  <Skeleton className="h-4 w-20" />
                  <Skeleton className="h-3 w-32" />
                </div>
              </div>
              <Skeleton className="h-5 w-20 rounded-full" />
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export function NodeStatusCards() {
  const { data, isLoading, refetch, isRefetching } = useNodesStatus();
  const [selectedNode, setSelectedNode] = useState<NodeStatus | null>(null);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);

  // Collect all unique tags from nodes
  const allTags = useMemo(() => {
    const tags = new Set<string>();
    data?.nodes.forEach((node) => {
      node.tags?.forEach((tag) => tags.add(tag));
    });
    return Array.from(tags).sort();
  }, [data?.nodes]);

  // Filter nodes by tags and status (exclude pending/not_authorized nodes)
  const filteredNodes = useMemo(() => {
    if (!data?.nodes) return [];
    
    // Filter out nodes with pending/not_authorized status
    // Only show truly authenticated nodes (connected AND has server_version)
    const onlineNodes = data.nodes.filter(node => node.connected && node.server_version);
    
    if (selectedTags.length === 0) return onlineNodes;
    
    return onlineNodes.filter((node) => {
      const nodeTags = node.tags || [];
      return selectedTags.some(tag => nodeTags.includes(tag));
    });
  }, [data?.nodes, selectedTags]);

  const handleViewDetails = (node: NodeStatus) => {
    setSelectedNode(node);
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    setIsModalOpen(false);
    setSelectedNode(null);
  };

  if (isLoading) {
    return <LoadingSkeleton />;
  }

  return (
    <>
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold">Clusters</h2>
            {data?.nodes && data.nodes.length > 0 ? (
              data.all_connected ? (
                <Badge variant="success" className="text-xs">
                  All Connected
                </Badge>
              ) : (
                <Badge variant="warning" className="text-xs">
                  Some nodes not connected
                </Badge>
              )
            ) : (
              <Badge variant="secondary" className="text-xs">
                No Clusters
              </Badge>
            )}
            {selectedTags.length > 0 && (
              <Badge variant="outline" className="text-xs">
                {filteredNodes.length} of {data?.nodes.length || 0}
              </Badge>
            )}
          </div>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => refetch()}
              disabled={isRefetching}
              className="gap-2"
            >
              <RefreshCw
                className={cn("h-4 w-4", isRefetching && "animate-spin")}
              />
              Refresh
            </Button>
          </div>
        </div>

        {/* Tag Filter */}
        {allTags.length > 0 && (
          <TagFilter
            availableTags={allTags}
            selectedTags={selectedTags}
            onTagsChange={setSelectedTags}
          />
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {filteredNodes.map((node, index) => (
            <NodeStatusCard
              key={node.name}
              node={node}
              index={index}
              onViewDetails={handleViewDetails}
              onTagsUpdate={refetch}
            />
          ))}
        </div>
      </div>

      <NodeDetailModal
        node={selectedNode}
        isOpen={isModalOpen}
        onClose={handleCloseModal}
      />
    </>
  );
}
