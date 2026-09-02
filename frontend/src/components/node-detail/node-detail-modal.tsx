"use client";

import * as React from "react";
import { usePermissions } from "@/hooks/use-permissions";
import { ServerOff } from "lucide-react";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { LiveTimestamp } from "@/components/ui/live-timestamp";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useNodeResources } from "@/hooks/use-lxd";
import { STALE_AFTER_MS } from "@/lib/query-config";
import { ClusterStatus, type NodeStatus } from "@/types/api";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { toMillis } from "@/lib/utils";
import { NodeSystemPanel } from "./node-system-panel";
import {
  NodeCpuPanel,
  NodeGpuPanel,
  NodeMemoryPanel,
  NodeStoragePanel,
} from "./node-resource-panels";

type NodeDetailTab = "system" | "cpu" | "gpu" | "memory" | "storage";

export interface NodeDetailModalProps {
  node: NodeStatus | null;
  isOpen: boolean;
  onClose: () => void;
  /** When the list this row came from was collected. Used on the System tab. */
  listCollectedAt?: string | null;
  /** The row has left the list. What is shown is the last state we saw. */
  isOrphaned?: boolean;
}

/**
 * Hardware detail for one cluster.
 *
 * important: this used to be a pure function of a prop. The parent copied the clicked row into
 * useState and passed that object down, so the numbers were a photograph taken at click time
 * and stayed that way for as long as the dialog was open. Now the parent passes the row it
 * re-derives from the live list, and the four resource tabs additionally read the uncached
 * per-cluster endpoint while they are on screen.
 *
 * note: gated on the tab, not just on the dialog. Four of five tabs need resources and System
 * needs none, so an operator reading kernel versions holds no live poll open against a cluster.
 */
export function NodeDetailModal({
  node,
  isOpen,
  onClose,
  listCollectedAt,
  isOrphaned = false,
}: NodeDetailModalProps) {
  // important: this dialog opens from the node card on the Overview, which every role reaches.
  // The card no longer shows the control-plane endpoint to a tenant, so showing it one click
  // deeper would make that gate cosmetic. Placed here, not next to its use, because of the
  // early return below.
  const { isPartner } = usePermissions();
  const [tab, setTab] = React.useState<NodeDetailTab>("system");

  React.useEffect(() => {
    if (isOpen) setTab("system");
  }, [isOpen]);

  const wantsResources = isOpen && node !== null && tab !== "system";
  const { data: liveResources, isError: liveFailed } = useNodeResources(node?.name ?? "", {
    enabled: wantsResources,
  });

  // important: every hook above the early return. The file had none, so `if (!node) return null`
  // sat at the top legally; adding one below it fails react-hooks/rules-of-hooks.
  if (!node) return null;

  const meta = clusterStatusMeta(node);
  const label = node.display_name || node.name;
  const isPendingTrust = !node.connected && !node.error;
  const unreachable = meta.status !== ClusterStatus.ONLINE;

  // note: never blank a panel while the live read is in flight. The list row already carries a
  // complete NodeResources of the same type, so every tab paints at once and the live body
  // replaces it in place rather than flashing an empty state.
  const showingLive = wantsResources && liveResources !== undefined;
  const resources = showingLive ? liveResources : node.resources;

  const collectedAt = showingLive
    ? toMillis(liveResources?.collected_at)
    : tab === "system"
      ? toMillis(listCollectedAt)
      : toMillis(node.resources?.collected_at ?? listCollectedAt);

  const staleAfterMs = showingLive ? STALE_AFTER_MS.active : STALE_AFTER_MS.fleet;
  const staleReason = isOrphaned
    ? "this cluster is no longer listed"
    : unreachable
      ? `${label} did not answer the last reachability check`
      : liveFailed
        ? `${label} is not answering right now`
        : undefined;

  return (
    <Dialog onOpenChange={onClose} open={isOpen}>
      <DialogContent className="flex max-h-[90vh] max-w-5xl flex-col overflow-hidden">
        <DialogHeader className="pb-4">
          <div className="flex-1">
            <DialogTitle className="flex items-center gap-2 text-2xl">
              {label}
              {/* certificate-handshake state, and the page that resolves it is partner-gated */}
              {isPartner && isPendingTrust && (
                <Badge className="text-xs" variant="secondary">
                  Pending Trust
                </Badge>
              )}
            </DialogTitle>
            {/* a tenant gets the cluster id, which is already the Node column on every table,
                rather than the hypervisor endpoint they cannot address */}
            <DialogDescription className="font-mono">
              {isPartner ? node.endpoint : node.name}
            </DialogDescription>
            <LiveTimestamp
              className="mt-1"
              forceStale={isOrphaned || unreachable || liveFailed}
              reason={staleReason}
              staleAfterMs={staleAfterMs}
              updatedAt={collectedAt}
            />
          </div>
        </DialogHeader>

        {isOrphaned ? (
          <EmptyState
            description={`${label} is no longer in the cluster list. The readings below were the last ones collected before it disappeared.`}
            icon={ServerOff}
            title="Cluster no longer listed"
          />
        ) : null}

        {/* note: DialogBody rather than a self-scrolling DialogContent, so the cluster name and
            its age line stay pinned while a long storage list scrolls under them. */}
        <DialogBody>
          <Tabs
            className="w-full"
            onValueChange={(next) => setTab(next as NodeDetailTab)}
            value={tab}
          >
            <TabsList className="grid w-full grid-cols-5">
              <TabsTrigger value="system">System</TabsTrigger>
              <TabsTrigger value="cpu">CPU</TabsTrigger>
              <TabsTrigger value="gpu">GPU</TabsTrigger>
              <TabsTrigger value="memory">Memory</TabsTrigger>
              <TabsTrigger value="storage">Storage</TabsTrigger>
            </TabsList>

            <TabsContent className="space-y-4" value="system">
              <NodeSystemPanel node={node} />
            </TabsContent>

            <TabsContent className="space-y-4" value="cpu">
              <NodeCpuPanel
                architecture={String(node.environment?.kernel_architecture || "N/A")}
                resources={resources}
              />
            </TabsContent>

            <TabsContent className="space-y-4" value="gpu">
              <NodeGpuPanel nodeLabel={label} resources={resources} />
            </TabsContent>

            <TabsContent className="space-y-4" value="memory">
              <NodeMemoryPanel resources={resources} />
            </TabsContent>

            <TabsContent className="space-y-4" value="storage">
              <NodeStoragePanel resources={resources} />
            </TabsContent>
          </Tabs>
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}
