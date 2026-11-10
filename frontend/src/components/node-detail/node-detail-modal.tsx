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
import { toMillis, EMPTY_VALUE } from "@/lib/utils";
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
  /** The row has left the list. The dialog shows the last state seen. */
  isOrphaned?: boolean;
}

/**
 * Hardware detail for one cluster.
 *
 * The parent passes the row it re-derives from the live list, and the four resource tabs also
 * read the uncached per-cluster endpoint while they are on screen. The live read is gated on the
 * tab: System needs no resources, so reading kernel versions holds no poll open against a cluster.
 */
export function NodeDetailModal({
  node,
  isOpen,
  onClose,
  listCollectedAt,
  isOrphaned = false,
}: NodeDetailModalProps) {
  // important: every role reaches this dialog from the Overview node card. The card hides the
  // control-plane endpoint from a tenant, so this dialog must too. The hook sits here because
  // of the early return below.
  const { isPartner } = usePermissions();
  const [tab, setTab] = React.useState<NodeDetailTab>("system");

  React.useEffect(() => {
    if (isOpen) setTab("system");
  }, [isOpen]);

  const wantsResources = isOpen && node !== null && tab !== "system";
  const { data: liveResources, isError: liveFailed } = useNodeResources(node?.name ?? "", {
    enabled: wantsResources,
  });

  // important: every hook stays above this early return; a hook below it fails
  // react-hooks/rules-of-hooks.
  if (!node) return null;

  const meta = clusterStatusMeta(node);
  const label = node.display_name || node.name;
  const isPendingTrust = !node.connected && !node.error;
  const unreachable = meta.status !== ClusterStatus.ONLINE;

  // Never blank a panel while the live read is in flight. The list row already carries a
  // complete NodeResources, so every tab paints at once and the live body replaces it in place.
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
              {/* Certificate-handshake state; the page that resolves it is partner-gated. */}
              {isPartner && isPendingTrust && (
                <Badge className="text-xs" variant="secondary">
                  Pending Trust
                </Badge>
              )}
            </DialogTitle>
            {/* A tenant gets the cluster id, already the Node column on every table, instead
                of the hypervisor endpoint it cannot address. */}
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

        {/* DialogBody rather than a self-scrolling DialogContent, so the cluster name and its
            age line stay pinned while a long storage list scrolls under them. */}
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
                architecture={String(node.environment?.kernel_architecture || EMPTY_VALUE)}
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
              <NodeStoragePanel nodeName={node.name} resources={resources} />
            </TabsContent>
          </Tabs>
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}
