"use client";

import { Activity, Cpu, HardDrive, Loader2, Network } from "lucide-react";
import {
  cpuReading,
  formatVcpuAmount,
  formatVcpuCount,
  memoryReading,
} from "@/lib/instance-metrics-view";
import { USAGE_CRITICAL_PERCENT, USAGE_ELEVATED_PERCENT } from "@/lib/usage-tone";
import {
  cn,
  formatByteQuota,
  formatBytes,
  formatMemory,
  formatMemoryQuota,
  formatNetworkRate,
} from "@/lib/utils";
import type { InstanceMetrics } from "@/types";

// note: the console is fixed-dark chrome and keeps its own palette; only the thresholds are
// shared with the rest of the product, so the bar and the dialog turn amber and red together.
function toneFor(percent: number): string {
  if (percent >= USAGE_CRITICAL_PERCENT) return "text-red-400";
  if (percent >= USAGE_ELEVATED_PERCENT) return "text-yellow-400";
  return "text-gray-300";
}

function barFor(percent: number, calm: string): string {
  if (percent >= USAGE_CRITICAL_PERCENT) return "bg-red-500";
  if (percent >= USAGE_ELEVATED_PERCENT) return "bg-yellow-500";
  return calm;
}

function quota({ value, unit }: { value: string; unit: string }): string {
  return unit ? `${value} ${unit}` : value;
}

function cpuLabel(metrics: InstanceMetrics): { text: string; tone: string; percent: number | null } {
  const reading = cpuReading(metrics);
  if (reading.state === "calculating") return { text: "calculating", tone: "text-gray-500", percent: null };
  if (reading.state === "unavailable") return { text: "n/a", tone: "text-gray-500", percent: null };
  return { text: `${reading.percent.toFixed(1)}%`, tone: toneFor(reading.percent), percent: reading.percent };
}

/** CPU and memory in the compact bar. */
export function InstanceResourceChips({ metrics }: { metrics: InstanceMetrics }) {
  const cpu = cpuLabel(metrics);
  const memory = memoryReading(metrics);
  return (
    <>
      <div className="flex items-center gap-1 text-xs">
        <Cpu className="h-3 w-3 text-blue-400" aria-hidden="true" />
        <span className="sr-only">CPU</span>
        <span className={cn("font-mono", cpu.tone)}>{cpu.text}</span>
      </div>

      <div className="h-3 w-px bg-terminal-border" />

      <div className="flex items-center gap-1 text-xs">
        <Activity className="h-3 w-3 text-green-400" aria-hidden="true" />
        <span className="sr-only">Memory</span>
        <span className={cn("font-mono", memory ? toneFor(memory.percent) : "text-gray-500")}>
          {memory && memory.total > 0 ? quota(formatMemoryQuota(memory.used, memory.total, 1)) : "n/a"}
        </span>
      </div>

      <div className="h-3 w-px bg-terminal-border" />
    </>
  );
}

/** The expanded Instance Resources section. */
export function InstanceResourcesPanel({
  metrics,
  loading,
}: {
  metrics: InstanceMetrics | undefined;
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-4 text-gray-500">
        <Loader2 className="h-4 w-4 animate-spin mr-2" aria-hidden="true" />
        Loading metrics...
      </div>
    );
  }
  if (!metrics) {
    return <div className="text-gray-500 py-2 text-center">Metrics unavailable</div>;
  }

  const cpu = cpuLabel(metrics);
  const cpuState = cpuReading(metrics);
  const memory = memoryReading(metrics);
  const totals = metrics.disk_total_bytes ?? {};
  const ratesReported = metrics.network_rx_bytes_per_second !== undefined;
  const rx = metrics.network_rx_bytes_per_second ?? {};
  const tx = metrics.network_tx_bytes_per_second ?? {};

  return (
    <>
      <div>
        <div className="flex justify-between mb-1">
          <span className="text-gray-400 flex items-center gap-1">
            <Cpu className="h-3 w-3 text-blue-400" aria-hidden="true" /> CPU
          </span>
          <span className={cn("font-mono", cpu.tone)}>
            {cpu.text}
            {cpuState.state === "ready" && cpuState.busy !== null && cpuState.vcpus
              ? ` (${formatVcpuAmount(cpuState.busy)} of ${formatVcpuCount(cpuState.vcpus)})`
              : ""}
          </span>
        </div>
        {cpu.percent !== null ? (
          <ProgressBar value={cpu.percent} color={barFor(cpu.percent, "bg-blue-500")} />
        ) : null}
      </div>

      <div>
        <div className="flex justify-between mb-1">
          <span className="text-gray-400 flex items-center gap-1">
            <Activity className="h-3 w-3 text-green-400" aria-hidden="true" /> Memory
          </span>
          <span className="text-gray-300 font-mono">
            {memory && memory.total > 0
              ? `${quota(formatMemoryQuota(memory.used, memory.total, 2))} (${memory.percent.toFixed(0)}%)`
              : "n/a"}
          </span>
        </div>
        {memory && memory.total > 0 ? (
          <ProgressBar value={memory.percent} color={barFor(memory.percent, "bg-green-500")} />
        ) : null}
        {memory && memory.available !== null ? (
          <div className="mt-1 text-gray-500">
            {formatMemory(memory.available)} available
            {memory.cache !== null ? `, ${formatMemory(memory.cache)} page cache` : ""}
          </div>
        ) : null}
      </div>

      {Object.entries(metrics.disk_usage_bytes).map(([device, bytes]) => (
        <div key={device} className="flex justify-between">
          <span className="text-gray-400 flex items-center gap-1">
            <HardDrive className="h-3 w-3 text-orange-400" aria-hidden="true" /> {device}
          </span>
          <span className="text-gray-300 font-mono">
            {totals[device] ? quota(formatByteQuota(bytes, totals[device], 1)) : formatBytes(bytes)}
          </span>
        </div>
      ))}

      {Object.entries(metrics.network_rx_bytes || {}).map(([iface, received]) => (
        <div key={iface} className="flex justify-between">
          <span className="text-gray-400 flex items-center gap-1">
            <Network className="h-3 w-3 text-purple-400" aria-hidden="true" /> {iface}
          </span>
          <span className="text-gray-300 font-mono">
            {ratesReported ? (
              <>
                <span className="text-green-400">
                  ↓{rx[iface] !== undefined ? formatNetworkRate(rx[iface], 1) : "…"}
                </span>{" "}
                <span className="text-blue-400">
                  ↑{tx[iface] !== undefined ? formatNetworkRate(tx[iface], 1) : "…"}
                </span>
                <span className="text-gray-500">
                  {" "}({formatBytes(received + (metrics.network_tx_bytes?.[iface] ?? 0), 1)} total)
                </span>
              </>
            ) : (
              <>
                <span className="text-green-400">↓{formatBytes(received, 1)}</span>{" "}
                <span className="text-blue-400">↑{formatBytes(metrics.network_tx_bytes?.[iface] ?? 0, 1)}</span>
              </>
            )}
          </span>
        </div>
      ))}
    </>
  );
}

/** Thin progress bar widget */
function ProgressBar({ value, color = "bg-blue-500" }: { value: number; color?: string }) {
  const clamped = Math.min(Math.max(value, 0), 100);
  return (
    <div className="w-full bg-terminal-border rounded-full h-1.5 overflow-hidden">
      <div
        className={cn("h-full rounded-full transition-all duration-300", color)}
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}
