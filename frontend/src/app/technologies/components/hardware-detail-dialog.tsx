"use client";

import * as React from "react";
import { AlertTriangle, Cpu, Loader2 } from "lucide-react";

import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { GpuPanel } from "@/components/gpu";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { formatBytes, formatLabel } from "@/lib/utils";
import type { NodeStatus } from "@/types/api";
import type { HardwareCategory } from "./hardware-aggregate";
import { CpuSocketList, StorageDeviceList } from "./hardware-lists";

interface HardwareDetailDialogProps {
  category: HardwareCategory | null;
  nodes: NodeStatus[];
  open: boolean;
  isRefreshing: boolean;
  refreshError: boolean;
  onOpenChange: (open: boolean) => void;
}

function NodePanel({
  node,
  children,
}: {
  node: NodeStatus;
  children: React.ReactNode;
}) {
  const label = node.display_name || node.name;
  const memberCount = node.resources?.hardware?.member_count;

  return (
    <section className="space-y-4 rounded-lg border p-4" aria-label={`${label} hardware`}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="min-w-0 break-words text-sm font-semibold">{label}</h3>
        <Badge variant={node.connected ? "positive" : "negative"}>
          {node.connected ? "Reporting" : "Not answering"}
        </Badge>
        {memberCount && memberCount > 1 ? (
          <Badge variant="outline" className="font-normal">
            {memberCount} members
          </Badge>
        ) : null}
      </div>
      {children}
    </section>
  );
}

function MissingReading({ node, noun }: { node: NodeStatus; noun: string }) {
  return (
    <div className="flex items-start gap-3 rounded-md border border-dashed p-3 text-sm">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <p className="break-words text-muted-foreground">
        {node.error || `This cluster is not reporting ${noun} right now.`}
      </p>
    </div>
  );
}

function CpuDetail({ node }: { node: NodeStatus }) {
  const resources = node.resources;
  const hardware = resources?.hardware;
  if (!resources) return <MissingReading node={node} noun="CPU detail" />;

  const architectures = hardware?.cpu.architectures.length
    ? hardware.cpu.architectures.join(", ")
    : node.environment?.kernel_architecture;
  const system = hardware?.system;

  return (
    <div className="space-y-5">
      <DetailSection title="Capacity">
        <DetailGrid cols={4}>
          <DetailField label="Total Hardware Threads" value={resources.cpu_total} />
          <DetailField label="Allocated vCPUs" value={resources.cpu_used} />
          <DetailField label="Unallocated Hardware Threads" value={resources.cpu_available} />
          <DetailField label="Sockets" value={resources.cpu_sockets} />
          <DetailField label="Architecture" value={architectures || undefined} mono />
          <DetailField label="Physical Cores" value={hardware?.cpu.cores} />
          <DetailField label="Topology Hardware Threads" value={hardware?.cpu.threads} />
          <DetailField label="NUMA Nodes" value={hardware?.cpu.numa_nodes} />
        </DetailGrid>
      </DetailSection>

      {system ? (
        <DetailSection title="System and Firmware">
          <DetailGrid cols={4}>
            <DetailField label="Vendor" value={system.vendor || undefined} />
            <DetailField label="Product" value={system.product || undefined} />
            <DetailField label="Family" value={system.family || undefined} />
            <DetailField label="Type" value={system.type || undefined} />
            <DetailField label="Firmware Vendor" value={system.firmware?.vendor || undefined} />
            <DetailField
              label="Firmware Version"
              value={system.firmware?.version || undefined}
              mono
            />
            <DetailField label="Firmware Date" value={system.firmware?.date || undefined} />
            <DetailField label="Motherboard" value={system.motherboard?.product || undefined} />
          </DetailGrid>
        </DetailSection>
      ) : null}

      {hardware?.cpu.sockets.length ? (
        <DetailSection title="Processors">
          <CpuSocketList sockets={hardware.cpu.sockets} />
        </DetailSection>
      ) : null}
    </div>
  );
}

function MemoryDetail({ node }: { node: NodeStatus }) {
  const resources = node.resources;
  const topology = resources?.hardware?.memory;
  if (!resources) return <MissingReading node={node} noun="memory detail" />;

  return (
    <DetailGrid cols={4}>
      <DetailField label="Total" value={formatBytes(resources.memory_total)} />
      {/* note: host memory in use (LXD's resources API), not what instances were allocated. */}
      <DetailField label="In Use" value={formatBytes(resources.memory_used)} />
      <DetailField label="Available" value={formatBytes(resources.memory_available)} />
      <DetailField
        label="Hugepages"
        value={topology ? formatBytes(topology.hugepages_total) : undefined}
      />
      <DetailField
        label="Hardware-reported Used"
        value={topology ? formatBytes(topology.used) : undefined}
      />
      <DetailField
        label="Hardware-reported Free"
        value={topology ? formatBytes(topology.free) : undefined}
      />
    </DetailGrid>
  );
}

function StorageDetail({ node }: { node: NodeStatus }) {
  const resources = node.resources;
  if (!resources) return <MissingReading node={node} noun="storage detail" />;

  return (
    <div className="space-y-5">
      {resources.degraded ? (
        <PartialReadNotice>
          This reading is incomplete. At least one storage source did not answer.
        </PartialReadNotice>
      ) : null}
      <DetailSection title="Capacity">
        <DetailGrid cols={3}>
          <DetailField label="Usable Capacity" value={formatBytes(resources.storage_total)} />
          <DetailField label="Used" value={formatBytes(resources.storage_used)} />
          <DetailField label="Available" value={formatBytes(resources.storage_available)} />
        </DetailGrid>
      </DetailSection>

      {resources.storage_pools.length > 0 ? (
        <DetailSection title="Storage Pools">
          <ul className="space-y-2">
            {resources.storage_pools.map((pool, index) => (
              <li
                key={`${pool.name}-${pool.scope ?? "node"}-${index}`}
                className="rounded-md border p-3"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="break-words text-sm font-medium">{pool.name}</span>
                  <div className="flex flex-wrap gap-1.5">
                    <Badge variant="outline" className="font-normal">
                      {formatLabel(pool.driver)}
                    </Badge>
                    {pool.scope ? (
                      <Badge variant="secondary" className="font-normal">
                        {pool.scope === "shared" ? "Shared" : "Local"}
                      </Badge>
                    ) : null}
                  </div>
                </div>
                <DetailGrid cols={3} className="mt-3">
                  <DetailField label="Capacity" value={formatBytes(pool.space_total)} />
                  <DetailField label="Used" value={formatBytes(pool.space_used)} />
                  <DetailField label="Available" value={formatBytes(pool.space_available)} />
                </DetailGrid>
              </li>
            ))}
          </ul>
        </DetailSection>
      ) : null}

      {resources.hardware?.storage_devices.length ? (
        <DetailSection title="Detected Devices">
          <StorageDeviceList devices={resources.hardware.storage_devices} />
        </DetailSection>
      ) : null}
    </div>
  );
}

function CategoryDetail({
  category,
  node,
}: {
  category: HardwareCategory["id"];
  node: NodeStatus;
}) {
  if (category === "cpu") return <CpuDetail node={node} />;
  if (category === "memory") return <MemoryDetail node={node} />;
  if (category === "storage") return <StorageDetail node={node} />;
  if (!node.resources) return <MissingReading node={node} noun="GPU detail" />;
  return <GpuPanel src={node.resources} dense />;
}

export function HardwareDetailDialog({
  category,
  nodes,
  open,
  isRefreshing,
  refreshError,
  onOpenChange,
}: HardwareDetailDialogProps) {
  const [nodePage, setNodePage] = React.useState(1);
  const orderedNodes = React.useMemo(
    () =>
      [...nodes].sort((a, b) =>
        (a.display_name || a.name).localeCompare(b.display_name || b.name),
      ),
    [nodes],
  );
  const totalPages = Math.max(1, orderedNodes.length);
  const currentPage = Math.min(nodePage, totalPages);
  const selectedNode = orderedNodes[currentPage - 1];
  const Icon = category?.icon ?? Cpu;

  React.useEffect(() => {
    if (open) setNodePage(1);
  }, [open, category?.id]);

  React.useEffect(() => {
    setNodePage((page) => Math.min(page, totalPages));
  }, [totalPages]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] max-w-5xl overflow-hidden flex flex-col">
        <DialogHeader className="pr-8 pb-4">
          <DialogTitle className="flex items-center gap-2">
            <Icon className="h-5 w-5 shrink-0" aria-hidden="true" />
            {category?.title ?? "Hardware details"}
          </DialogTitle>
          <DialogDescription>
            {category
              ? `${category.description}. Values remain separated by cluster so partial readings stay visible.`
              : "Hardware detail by cluster."}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4 pr-2">
          {isRefreshing ? (
            <div
              className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground"
              role="status"
            >
              <Loader2
                className="h-4 w-4 animate-spin motion-reduce:animate-none"
                aria-hidden="true"
              />
              Refreshing hardware readings…
            </div>
          ) : null}
          {refreshError ? (
            <PartialReadNotice>
              The latest refresh failed. These are the last successful hardware readings.
            </PartialReadNotice>
          ) : null}
          {category && selectedNode ? (
            <NodePanel key={selectedNode.name} node={selectedNode}>
              <CategoryDetail category={category.id} node={selectedNode} />
            </NodePanel>
          ) : (
            <EmptyState
              icon={Cpu}
              title="No hardware detail available"
              description="Details appear here once a cluster is registered and reporting resources."
            />
          )}
        </DialogBody>

        {orderedNodes.length > 1 ? (
          <DataTablePagination
            variant="compact"
            label={`${category?.title ?? "hardware"} cluster details`}
            currentPage={currentPage}
            totalPages={totalPages}
            onPageChange={setNodePage}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
