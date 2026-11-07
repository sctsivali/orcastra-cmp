"use client";

import { useState } from "react";
import { cpuAllocation, cpuAllocationFigure, cpuAllocationText } from "@/lib/cpu-allocation";
import {
  GPU_EMPTY_TITLE,
  GPU_NO_COMPUTE_TITLE,
  gpuDenseRatio,
  gpuNoComputeCaption,
  summarizeGpus,
} from "@/lib/gpu";
import { CpuAllocationFigure, CpuAllocationInfo, CpuAllocationText } from "@/components/cpu/cpu-allocation-info";
import { GpuCapacityInfo } from "@/components/gpu";
import { HypervisorReleaseStatus } from "@/components/hypervisor-release";

import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { Server, Cloud, Cpu, MemoryStick, Sparkles, MoreVertical, Edit, Users } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { KernelAdvisoryBadge } from "@/components/kernel-advisory/kernel-advisory-badge";
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
import { useUpdateNodeTags, useUpdateNodeDisplayName } from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { usePermissions } from "@/hooks/use-permissions";
import { cn, formatBytes } from "@/lib/utils";
import { capacityReading } from "@/lib/capacity-reading";
import { OverflowText } from "@/components/ui/overflow-text";
import { Progress } from "@/components/ui/progress";
import { UsageBar } from "@/components/ui/usage-bar";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { TagManager } from "@/components/tag-manager";
import { toast } from "@/hooks/use-toast";
import { inventoryLinks } from "@/lib/inventory-links";

interface NodeStatusCardProps {
  node: NodeStatus;
  onViewDetails: (node: NodeStatus) => void;
  onTagsUpdate?: () => void;
}

export function NodeStatusCard({ node, onViewDetails, onTagsUpdate }: NodeStatusCardProps) {
  const { navigate } = useNavigationProgress();
  const updateNodeTags = useUpdateNodeTags();
  const updateNodeDisplayName = useUpdateNodeDisplayName();
  const { isReadOnly } = useReadOnlyMode();
  // important: this card is mounted ungated on the Overview, which every role reaches. The
  // node name and its status are the tenant's business, because their instance runs there.
  // The control-plane endpoint, the raw connection error, the rename action and the upgrade
  // runbook are the operator's, and a tenant is shown none of them anywhere else.
  const { isPartner } = usePermissions();
  const [editNameDialogOpen, setEditNameDialogOpen] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [newDisplayName, setNewDisplayName] = useState(node.display_name || node.name);
  const [isSavingName, setIsSavingName] = useState(false);

  const handleSaveDisplayName = async () => {
    if (!isPartner) return;
    if (isReadOnly) {
      toast({
        title: "Read-only mode active",
        description: "Cluster name updates are disabled in read-only mode.",
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
          title: "Cluster name updated",
          description: `Display name for cluster '${node.name}' has been updated to '${newDisplayName}'.`,
          variant: "success",
        });
        onTagsUpdate?.();
      }, 100);
    } catch (error) {
      toast({
        title: "Could not update cluster name",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setIsSavingName(false);
    }
  };

  const handleTagsChange = async (newTags: string[]) => {
    if (!isPartner) return;
    if (isReadOnly) {
      toast({
        title: "Read-only mode active",
        description: "Tag updates are disabled in read-only mode.",
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
        title: "Tags updated",
        description: `Tags for cluster '${node.name}' have been updated.`,
        variant: "success",
      });
      onTagsUpdate?.();
    } catch (error) {
      toast({
        title: "Could not update tags",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    }
  };

  const cpu = cpuAllocation(node.resources ?? { cpu_total: 0, cpu_used: 0 });
  const cpuFigure = cpuAllocationFigure(cpu);
  const memoryFigure = node.resources
    ? `${formatBytes(node.resources.memory_used)} / ${formatBytes(node.resources.memory_total)}`
    : "";
  // capacityReading's percent is usagePercent: clamped for the bar, and null rather than 0 when
  // the counters are missing, so an unread host draws no bar instead of an empty one.
  const memory = node.resources
    ? capacityReading({ used: node.resources.memory_used, total: node.resources.memory_total })
    : null;
  // Clustering, not the distribution name, is what the Cloud glyph is saying here.
  const NodeIcon = node.clustered ? Cloud : Server;
  const statusMeta = clusterStatusMeta(node);
  const StatusIcon = statusMeta.icon;

  return (
    // note: no entrance stagger and no hover lift. A status surface does not animate for
    // decoration, and the card itself is not a control: its buttons are.
    <Card data-slot="cluster-card" className="overflow-hidden">
      <CardContent className="p-3 md:p-4">
        {/* important: flex-wrap with a basis-48 title block, not flex-col sm:flex-row. The grid
            is three-up at xl, a 309px card at 1280 with the sidebar open (the same measurement
            projects-section.tsx records), and the badge group (status, Clustered, kebab) is
            about 190px, so a fixed row left the title around 35px. Wrapping drops the badges
            under the title on a narrow card instead of crushing the name. */}
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0 flex-1 basis-48">
            <div
              className={cn(
                "flex h-10 w-10 items-center justify-center rounded-lg shrink-0 bg-muted",
                statusMeta.iconClassName
              )}
            >
              <NodeIcon className="h-5 w-5" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <h3 className="font-semibold truncate">{node.display_name || node.name}</h3>
              </div>
              {node.display_name && (
                <p className="text-xs text-muted-foreground truncate">
                  {node.name}
                </p>
              )}
              {isPartner && (
                <p className="text-xs text-muted-foreground">
                  <OverflowText
                    value={node.endpoint}
                    label={`endpoint for ${node.display_name || node.name}`}
                    mono
                  />
                </p>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {/* "Connected" and "Online" were two spellings of one idea across two pages.
                One vocabulary now, from lib/cluster-status.ts. */}
            <Badge variant={statusMeta.tone} className="gap-1">
              <StatusIcon className="h-3 w-3" />
              {statusMeta.label}
            </Badge>
            {node.clustered && <Badge variant="muted">Clustered</Badge>}
            {/* the endpoint behind this is require_partner() (backend nodes.py), so offering it
                to a tenant is a button that can only ever answer 403 */}
            {isPartner && (
              <div onClick={(e) => e.stopPropagation()}>
                <DropdownMenu open={dropdownOpen} onOpenChange={setDropdownOpen}>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" size="icon" className="h-7 w-7" aria-label={`Actions for cluster ${node.name}`}>
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
            )}
          </div>
        </div>
        {node.connected && node.server_version && (
          <div className="mt-3 flex flex-wrap gap-2 md:gap-4 text-xs text-muted-foreground items-center">
            <span>
              <span className="font-medium">Hypervisor Version:</span>{" "}
              {node.server_version}
            </span>
            {node.api_version && (
              <span>
                <span className="font-medium">API Version:</span> {node.api_version}
              </span>
            )}
            {/* The backend's verdict against the Cluster's own release track. Partner+ only: it
                carries the upgrade command, a maintenance instruction a tenant can neither run
                nor act on. */}
            {isPartner && (
              <HypervisorReleaseStatus
                release={node.hypervisor_release}
                currentVersion={node.server_version}
                clustered={node.clustered}
              />
            )}
            {/* Partner+ for the same reason: an open kernel CVE is a maintenance finding. The chip
                renders nothing while every member reads as patched. */}
            {isPartner && <KernelAdvisoryBadge clusterId={node.name} />}
          </div>
        )}
        {node.connected && node.resources && (
          <div className="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
            {/* CPU Info */}
            <div className="flex min-w-0 flex-col gap-1">
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Cpu className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <span className="font-medium">vCPU Allocation</span>
                <CpuAllocationInfo cpu={cpu} />
              </div>
              <div className="text-xs">
                <CpuAllocationFigure cpu={cpu} className="block font-semibold text-foreground" />
                {/* The monitoring card's meter: the clamped share of threads in the allocation
                    tone, so an over-commit reads as a ratio rather than as a red bar.
                    important: role="meter", not Radix's progressbar. This is a level, not a task
                    in progress, and "no progressbar on the page" is how the house loader is
                    told apart from a settled Overview. */}
                {cpu.threads > 0 && (
                  <Progress
                    role="meter"
                    value={cpu.percentClamped}
                    aria-label="vCPU allocation"
                    getValueLabel={() => `${cpuFigure}, ${cpuAllocationText(cpu)}`}
                    className={cn("my-1 h-2", cpu.meterClassName)}
                  />
                )}
                <CpuAllocationText cpu={cpu} className={cn("block", cpu.textClassName)} />
              </div>
            </div>

            {/* Memory */}
            <div className="flex min-w-0 flex-col gap-1">
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <MemoryStick className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <span className="font-medium">Memory</span>
              </div>
              <div className="text-xs">
                <div className="font-semibold text-foreground">{memoryFigure}</div>
                {memory && (
                  <UsageBar
                    percent={memory.percent}
                    label="Memory"
                    valueText={`${memoryFigure} (${memory.percent.toFixed(1)}%)`}
                    className="my-1 h-2"
                  />
                )}
                <div className="text-muted-foreground">
                  {memory ? `${memory.percent.toFixed(1)}% used` : "Usage unavailable"}
                </div>
              </div>
            </div>

            {/* GPU Info */}
            <div className="flex min-w-0 flex-col gap-1">
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Sparkles className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <span className="font-medium">GPU</span>
                <GpuCapacityInfo />
              </div>
              {(() => {
                const gpu = summarizeGpus(node.resources);
                return (
                  <div className="text-xs">
                    <div className="font-semibold text-foreground">
                      {gpu.hasCompute
                        ? gpuDenseRatio(gpu)
                        : gpu.hasCards
                          ? GPU_NO_COMPUTE_TITLE
                          : GPU_EMPTY_TITLE}
                    </div>
                    {(gpu.hasCompute || gpu.hasCards) && (
                      <div className="text-muted-foreground">
                        {gpu.hasCompute
                          ? `${(gpu.allocationPercent ?? 0).toFixed(1)}% allocated${
                              gpu.extraCards > 0 ? ` · ${gpu.cardsTotal} cards` : ""
                            }`
                          : gpuNoComputeCaption(gpu)}
                      </div>
                    )}
                  </div>
                );
              })()}
            </div>
          </div>
        )}
        {/* node.error is the backend exception verbatim (str(e) at services/lxd/lxd_service.py),
            so it carries the endpoint, port and TLS detail. The status badge above already tells
            a tenant the node is not answering, which is the part that concerns them. */}
        {isPartner && !node.connected && node.error && (
          <p className="mt-3 text-xs text-destructive">{node.error}</p>
        )}

        {/* Tags */}
        <div 
          className="mt-3 pt-3 border-t"
          onClick={(e) => e.stopPropagation()}
        >
          <TagManager
            tags={node.tags || []}
            onTagsChange={handleTagsChange}
                  readonly={isReadOnly || !isPartner}
            maxTags={10}
            placeholder="Add cluster tag…"
          />
        </div>

        {/* Cluster members exist for any clustered host, not only a MicroCloud one */}
        {node.clustered && node.connected && (
          <div className="mt-3 pt-3 border-t">
            <Button
              variant="outline"
              size="sm"
              className="w-full gap-2"
              onClick={(e) => {
                e.stopPropagation();
                navigate(inventoryLinks.members(node.name));
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
            <DialogTitle>Edit Cluster Display Name</DialogTitle>
            <DialogDescription>
              Change the display name for cluster {node.name}. The original cluster name will remain unchanged.
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
              {isSavingName ? "Saving…" : "Save Changes"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
