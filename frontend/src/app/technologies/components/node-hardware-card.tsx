"use client";

import * as React from "react";
import { ChevronDown, Cpu, Server } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { EmptyState } from "@/components/ui/empty-state";
import { GpuPanel } from "@/components/gpu";
import { cn, formatBytes, formatMemory} from "@/lib/utils";
import type { NodeStatus } from "@/types/api";
import { CpuSocketList, NetworkCardList, StorageDeviceList } from "./hardware-lists";

/**
 * One node's detected hardware, behind a disclosure.
 *
 * No fetch. The profile rides NodeResources on the /nodes response this page already reads,
 * which is why there is no loading state, no per-node error state and no request fan-out
 * across a sixteen-node fleet.
 *
 * The trigger is a bare CollapsibleTrigger so Radix renders a real button. regions-page.tsx
 * wraps CardHeader in `asChild` instead, which merges the trigger props onto a div with no
 * tabIndex and no role, and those cards cannot be reached by keyboard at all.
 */
export function NodeHardwareCard({ node }: { node: NodeStatus }) {
  const [open, setOpen] = React.useState(false);

  const resources = node.resources;
  const hardware = resources?.hardware;
  const label = node.display_name || node.name;
  const reachable = node.connected && Boolean(hardware?.detected);

  const summary = [
    resources?.cpu_total ? `${resources.cpu_total} logical CPUs` : null,
    resources?.memory_total ? formatMemory(resources.memory_total) : null,
    resources?.storage_total ? formatBytes(resources.storage_total) : null,
  ].filter(Boolean) as string[];

  const machine = hardware?.system
    ? [hardware.system.vendor, hardware.system.product].filter(Boolean).join(" ")
    : "";

  return (
    <Card>
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger
          disabled={!reachable}
          aria-label={
            reachable
              ? `Show detected hardware for ${label}`
              : `${label} is not reporting hardware`
          }
          className={cn(
            "flex w-full items-center justify-between gap-3 p-4 text-left rounded-lg",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
            reachable ? "hover:bg-muted/50" : "cursor-not-allowed",
          )}
        >
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <Server className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              <span className="font-medium">{label}</span>
              {hardware?.scope === "cluster" ? (
                <Badge variant="outline" className="font-normal">
                  {hardware.member_count} members
                </Badge>
              ) : null}
              {node.connected ? null : <Badge variant="muted">Not answering</Badge>}
            </div>
            {/* note: spans, not paragraphs. A button's content model is phrasing content, and
                the accessible name is computed from this subtree. */}
            {machine ? (
              <span className="block text-sm text-muted-foreground break-words">{machine}</span>
            ) : null}
            {summary.length > 0 ? (
              <span className="block text-xs text-muted-foreground">{summary.join(" · ")}</span>
            ) : null}
            {!reachable ? (
              <span className="block text-xs text-muted-foreground break-words">
                {node.error ||
                  "Hardware detail appears here once this node answers and reports its resources."}
              </span>
            ) : null}
          </div>
          {reachable ? (
            <ChevronDown
              className={cn(
                "h-4 w-4 shrink-0 text-muted-foreground motion-safe:transition-transform",
                open && "rotate-180",
              )}
              aria-hidden="true"
            />
          ) : null}
        </CollapsibleTrigger>

        <CollapsibleContent>
          <CardContent className="space-y-6 border-t pt-4">
            {hardware ? <NodeHardwareDetail hardware={hardware} node={node} /> : null}
          </CardContent>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}

function NodeHardwareDetail({
  hardware,
  node,
}: {
  hardware: NonNullable<NonNullable<NodeStatus["resources"]>["hardware"]>;
  node: NodeStatus;
}) {
  const { system, cpu, memory } = hardware;
  const cluster = hardware.scope === "cluster";

  return (
    <>
      {system ? (
        <DetailSection
          title="System and Firmware"
          description={
            cluster ? "Reported identically by every member of this cluster." : undefined
          }
        >
          <DetailGrid cols={4}>
            <DetailField label="Vendor" value={system.vendor || undefined} />
            <DetailField label="Product" value={system.product || undefined} />
            <DetailField label="Family" value={system.family || undefined} />
            <DetailField label="Type" value={system.type || undefined} />
            <DetailField label="Firmware Vendor" value={system.firmware?.vendor || undefined} />
            <DetailField label="Firmware Version" value={system.firmware?.version || undefined} mono />
            <DetailField label="Firmware Date" value={system.firmware?.date || undefined} />
            <DetailField label="Chassis" value={system.chassis?.type || undefined} />
            <DetailField label="Motherboard" value={system.motherboard?.product || undefined} />
          </DetailGrid>
        </DetailSection>
      ) : null}

      <DetailSection title="CPU Topology">
        <DetailGrid cols={4}>
          {/* Architectures is a list because a cluster can genuinely run two, and because an
              unreported architecture must read as nothing rather than as x86_64. */}
          <DetailField
            label={cpu.architectures.length > 1 ? "Architectures" : "Architecture"}
            value={cpu.architectures.length > 0 ? cpu.architectures.join(", ") : undefined}
            mono
          />
          <DetailField label="Logical CPUs" value={cpu.total || undefined} />
          <DetailField label="Physical Cores" value={cpu.cores || undefined} />
          <DetailField label="Threads" value={cpu.threads || undefined} />
          <DetailField label="NUMA Nodes" value={cpu.numa_nodes || undefined} />
          <DetailField label="Sockets" value={cpu.sockets.length || undefined} />
        </DetailGrid>
        <CpuSocketList sockets={cpu.sockets} />
      </DetailSection>

      <DetailSection title="Memory on This Node">
        <DetailGrid cols={4}>
          <DetailField label="Total" value={memory.total ? formatMemory(memory.total) : undefined} />
          <DetailField label="Used" value={memory.used ? formatMemory(memory.used) : undefined} />
          <DetailField label="Free" value={memory.free ? formatMemory(memory.free) : undefined} />
          <DetailField
            label="Hugepages"
            value={memory.hugepages_total ? formatMemory(memory.hugepages_total) : undefined}
          />
        </DetailGrid>
      </DetailSection>

      {hardware.storage_devices.length > 0 ? (
        <DetailSection
          title="Storage Devices"
          description={
            hardware.storage_types.length > 0 ? hardware.storage_types.join(", ") : undefined
          }
        >
          <StorageDeviceList devices={hardware.storage_devices} />
        </DetailSection>
      ) : null}

      {hardware.networks.length > 0 ? (
        <DetailSection title="Network Interfaces">
          <NetworkCardList cards={hardware.networks} />
        </DetailSection>
      ) : null}

      {node.resources ? (
        <DetailSection title="Graphics and Compute Cards">
          {/* The canonical GPU surface, fed from the same node.resources the counts come from.
              A second GPU renderer is how the counts came to disagree in the first place. */}
          <GpuPanel src={node.resources} dense />
        </DetailSection>
      ) : null}

      {!system && hardware.storage_devices.length === 0 && hardware.networks.length === 0 ? (
        <EmptyState
          icon={Cpu}
          title="This node reports only CPU and memory"
          description="Nested and virtualised hosts read no firmware, disk or network inventory. Nothing is hidden here, the node simply does not report it."
        />
      ) : null}
    </>
  );
}
