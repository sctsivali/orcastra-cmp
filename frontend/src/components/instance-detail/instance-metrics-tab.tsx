"use client";

import * as React from "react";
import { Activity, Cpu, HardDrive, Network } from "lucide-react";
import { Card } from "@/components/ui/card";
import { UsageMeter } from "@/components/ui/usage-meter";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingLottieMedium } from "@/components/loading-lottie";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { formatBytes, formatMemory} from "@/lib/utils";
import type { InstanceMetrics, VMInfo } from "@/types/api";

// The live-metrics half of the instance dialog, carried over from instance-monitoring-modal
// with four corrections rather than a rewrite:
//   - formatBytes comes from lib/utils; the modal had its own fifth copy of it.
//   - the meters come from the house primitives, not a hand-rolled div with bg-green-500. A raw
//     palette step is banned in this codebase and the semantic tokens exist for exactly this.
//   - absent values render the shared placeholder instead of the string "N/A".
//   - `architecture` never goes through formatLabel, which turns x86_64 into "X86 64".
export function InstanceMetricsTab({
  instance,
  metrics,
  isLoading,
}: {
  instance: VMInfo;
  metrics?: InstanceMetrics;
  isLoading: boolean;
}) {
  if (instance.status !== "Running") {
    return (
      <EmptyState
        icon={Activity}
        title="Instance is not running"
        description="Start the instance to see live CPU, memory, disk and network figures."
      />
    );
  }
  if (isLoading) {
    return (
      <div className="flex justify-center py-8">
        <LoadingLottieMedium />
      </div>
    );
  }
  if (!metrics) {
    return <EmptyState icon={Activity} title="No metrics available" />;
  }

  const memoryPercent = Math.min(metrics.memory_usage_percent ?? 0, 100);
  const interfaces = Object.keys(metrics.network_rx_bytes ?? {});
  const disks = Object.entries(metrics.disk_usage_bytes ?? {});

  return (
    <div className="space-y-4">
      <DetailSection title="Runtime">
        <DetailGrid cols={4}>
          <DetailField label="Processes" value={metrics.processes} />
          <DetailField label="Uptime" value={formatUptime(metrics.uptime_seconds)} />
          <DetailField label="PID" value={metrics.pid} />
          <DetailField label="Base Image" value={metrics.base_image} mono span="full" />
        </DetailGrid>
      </DetailSection>

      <Card className="space-y-2 p-4">
        <div className="flex items-center gap-2">
          <Cpu className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          <h3 className="font-semibold">CPU</h3>
        </div>
        <div className="flex items-baseline gap-3">
          <span className="text-3xl font-bold">
            {(metrics.cpu_usage_percent ?? 0).toFixed(1)}%
          </span>
          <span className="text-xs text-muted-foreground">
            {metrics.cpu_usage_percent === 0 ? "calculating" : "across all cores"}
          </span>
        </div>
      </Card>

      <Card className="space-y-2 p-4">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          <h3 className="font-semibold">Memory</h3>
        </div>
        {/* important: UsageMeter, not Progress. The Overview tab draws the same field with the
            usageTone severity ramp, and a bare Progress here painted the product accent instead,
            so one dialog showed the same 92 percent in two colours that mean different things.
            Progress also ships no accessible name, so this bar announced only "progress bar, 92
            percent" without saying what was at 92 percent. */}
        <UsageMeter
          label="Memory"
          total={metrics.memory_total_bytes}
          used={metrics.memory_used_bytes}
          value={`${formatMemory(metrics.memory_used_bytes)} / ${formatMemory(
            metrics.memory_total_bytes,
          )} (${memoryPercent.toFixed(1)}%)`}
        />
      </Card>

      {disks.length > 0 && (
        <Card className="space-y-2 p-4">
          <div className="flex items-center gap-2">
            <HardDrive className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
            <h3 className="font-semibold">Disk</h3>
          </div>
          <dl className="space-y-1">
            {disks.map(([device, bytes]) => (
              <div key={device} className="flex justify-between text-sm">
                <dt className="text-muted-foreground">{device}</dt>
                <dd className="font-medium">{formatBytes(bytes)}</dd>
              </div>
            ))}
          </dl>
        </Card>
      )}

      {interfaces.length > 0 && (
        <Card className="space-y-3 p-4">
          <div className="flex items-center gap-2">
            <Network className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
            <h3 className="font-semibold">Network</h3>
          </div>
          {interfaces.map((iface) => (
            <div key={iface} className="space-y-1">
              <div className="border-b pb-1 text-sm font-medium">{iface}</div>
              <DetailGrid cols={2}>
                <DetailField label="Received" value={formatBytes(metrics.network_rx_bytes[iface])} />
                <DetailField
                  label="Transmitted"
                  value={formatBytes(metrics.network_tx_bytes?.[iface] ?? 0)}
                />
              </DetailGrid>
            </div>
          ))}
        </Card>
      )}

      {(metrics.ipv4_addresses?.length || metrics.ipv6_addresses?.length) ? (
        <DetailSection title="Addresses">
          <div className="space-y-2">
            {(["ipv4_addresses", "ipv6_addresses"] as const).map((key) =>
              metrics[key]?.length ? (
                <div key={key}>
                  <p className="mb-1 text-xs text-muted-foreground">
                    {key === "ipv4_addresses" ? "IPv4" : "IPv6"}
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {metrics[key].map((ip) => (
                      <Badge key={ip} variant="outline" className="font-mono">
                        {ip}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : null,
            )}
          </div>
        </DetailSection>
      ) : null}
    </div>
  );
}

function formatUptime(seconds?: number): string | undefined {
  if (!seconds) return undefined;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}
