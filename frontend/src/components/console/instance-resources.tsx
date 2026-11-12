"use client";

import { Activity, Cpu, HardDrive, Loader2, Network } from "lucide-react";
import {
  cpuReading,
  formatVcpuAmount,
  formatVcpuBusy,
  memoryReading,
} from "@/lib/instance-metrics-view";
import { usageLevel, type UsageLevel } from "@/lib/usage-tone";
import {
  cn,
  formatVcpuCount,
  EMPTY_VALUE,
} from "@/lib/utils";
import { useByteFormat } from "@/hooks/use-byte-format";
import type { InstanceMetrics } from "@/types";

// The console is fixed-dark chrome, so the ramp is the --status-* set read through the
// terminal-status rebinding (their dark values in either page theme). The band comes from
// usageLevel, the product's one ramp, so the bar and the dialog turn amber and red together.
// Neutral while Normal, as every caption is (components/ui/usage-caption.tsx).
function toneFor(percent: number): string {
  const level = usageLevel(percent);
  if (level === "Critical") return "terminal-status text-status-offline";
  if (level === "High") return "terminal-status text-status-pending";
  return "text-gray-300";
}

function barFor(percent: number): string {
  const level = usageLevel(percent);
  if (level === "Critical") return "terminal-status bg-status-offline";
  if (level === "High") return "terminal-status bg-status-pending";
  return "terminal-status bg-status-online";
}

/** " · High" beside a flagged reading, so the band is never told by colour alone. */
function LevelWord({ percent, srOnly = false }: { percent: number; srOnly?: boolean }) {
  const level: UsageLevel = usageLevel(percent);
  if (level === "Normal") return null;
  return <span className={srOnly ? "sr-only" : "whitespace-nowrap font-sans"}>{` · ${level}`}</span>;
}

function quota({ value, unit }: { value: string; unit: string }): string {
  return unit ? `${value} ${unit}` : value;
}

function cpuLabel(metrics: InstanceMetrics): { text: string; tone: string; percent: number | null } {
  const reading = cpuReading(metrics);
  if (reading.state === "calculating") return { text: "calculating", tone: "text-gray-500", percent: null };
  if (reading.state === "unavailable") return { text: EMPTY_VALUE, tone: "text-gray-500", percent: null };
  return { text: `${reading.percent.toFixed(1)}%`, tone: toneFor(reading.percent), percent: reading.percent };
}

/** CPU and memory in the compact bar. */
export function InstanceResourceChips({ metrics }: { metrics: InstanceMetrics }) {
  const { formatBytes, formatByteQuota } = useByteFormat();
  const cpu = cpuLabel(metrics);
  const memory = memoryReading(metrics);
  return (
    <>
      <div className="flex items-center gap-1 text-xs">
        <Cpu className="h-3 w-3 text-gray-400" aria-hidden="true" />
        <span className="sr-only">vCPU</span>
        <span className={cn("font-mono", cpu.tone)}>
          {cpu.text}
          {cpu.percent !== null ? <LevelWord percent={cpu.percent} srOnly /> : null}
        </span>
      </div>

      <div className="h-3 w-px bg-terminal-border" />

      <div className="flex items-center gap-1 text-xs">
        <Activity className="h-3 w-3 text-gray-400" aria-hidden="true" />
        <span className="sr-only">Memory</span>
        <span className={cn("font-mono", memory && !memory.unlimited ? toneFor(memory.percent) : "text-gray-500")}>
          {memory && memory.unlimited
            ? `${formatBytes(memory.used)}, no limit`
            : memory && memory.total > 0
              ? quota(formatByteQuota(memory.used, memory.total, 1))
              : EMPTY_VALUE}
          {memory && !memory.unlimited && memory.total > 0 ? <LevelWord percent={memory.percent} srOnly /> : null}
        </span>
      </div>

      {/* Leads into the network rates, which the resource bar shows from sm up. */}
      <div className="hidden h-3 w-px bg-terminal-border sm:block" />
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
  const { formatBytes, formatByteQuota, formatNetworkRate } = useByteFormat();
  if (loading) {
    return (
      <div className="flex items-center justify-center py-4 text-gray-500">
        <Loader2 className="h-4 w-4 animate-spin mr-2" aria-hidden="true" />
        Loading metrics…
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
            <Cpu className="h-3 w-3 text-gray-400" aria-hidden="true" /> vCPU
          </span>
          <span className={cn("font-mono", cpu.tone)}>
            {cpu.text}
            {cpuState.state === "ready" && cpuState.busy !== null && cpuState.unlimited
              ? ` (${formatVcpuBusy(cpuState.busy)}, no limit)`
              : cpuState.state === "ready" && cpuState.busy !== null && cpuState.vcpus
                ? ` (${formatVcpuAmount(cpuState.busy)} of ${formatVcpuCount(cpuState.vcpus)})`
                : ""}
            {cpu.percent !== null ? <LevelWord percent={cpu.percent} /> : null}
          </span>
        </div>
        {cpu.percent !== null ? (
          <ProgressBar value={cpu.percent} color={barFor(cpu.percent)} label={`vCPU usage: ${cpu.text}`} />
        ) : null}
      </div>

      <div>
        <div className="flex justify-between mb-1">
          <span className="text-gray-400 flex items-center gap-1">
            <Activity className="h-3 w-3 text-gray-400" aria-hidden="true" /> Memory
          </span>
          {/* Toned like the vCPU line and the compact chip above it. */}
          <span
            className={cn(
              "font-mono",
              memory && !memory.unlimited && memory.total > 0 ? toneFor(memory.percent) : "text-gray-300",
            )}
          >
            {memory && memory.unlimited
              ? `${formatBytes(memory.used)} used, no limit`
              : memory && memory.total > 0
                ? `${quota(formatByteQuota(memory.used, memory.total, 2))} (${memory.percent.toFixed(0)}%)`
                : EMPTY_VALUE}
            {memory && !memory.unlimited && memory.total > 0 ? <LevelWord percent={memory.percent} /> : null}
          </span>
        </div>
        {memory && memory.total > 0 && !memory.unlimited ? (
          <ProgressBar
            value={memory.percent}
            color={barFor(memory.percent)}
            label={`Memory: ${quota(formatByteQuota(memory.used, memory.total, 2))} (${memory.percent.toFixed(0)}%)`}
          />
        ) : null}
        {/* Without a memory limit "available" is the host's free memory, not this
            instance's headroom, so it is left out. */}
        {memory && memory.available !== null && !memory.unlimited ? (
          <div className="mt-1 text-gray-500">
            {formatBytes(memory.available)} available
            {memory.cache !== null ? `, ${formatBytes(memory.cache)} page cache` : ""}
          </div>
        ) : null}
      </div>

      {Object.entries(metrics.disk_usage_bytes).map(([device, bytes]) => (
        <div key={device} className="flex justify-between">
          <span className="text-gray-400 flex items-center gap-1">
            <HardDrive className="h-3 w-3 text-gray-400" aria-hidden="true" /> {device}
          </span>
          <span className="text-gray-300 font-mono">
            {totals[device] ? quota(formatByteQuota(bytes, totals[device], 1)) : formatBytes(bytes)}
          </span>
        </div>
      ))}

      {Object.entries(metrics.network_rx_bytes || {}).map(([iface, received]) => (
        <div key={iface} className="flex justify-between">
          <span className="text-gray-400 flex items-center gap-1">
            <Network className="h-3 w-3 text-gray-400" aria-hidden="true" /> {iface}
          </span>
          <span className="text-gray-300 font-mono">
            {ratesReported ? (
              <>
                <span>
                  ↓{rx[iface] !== undefined ? formatNetworkRate(rx[iface], 1) : "…"}
                </span>{" "}
                <span>
                  ↑{tx[iface] !== undefined ? formatNetworkRate(tx[iface], 1) : "…"}
                </span>
                <span className="text-gray-500">
                  {" "}({formatBytes(received + (metrics.network_tx_bytes?.[iface] ?? 0), 1)} total)
                </span>
              </>
            ) : (
              <>
                <span>↓{formatBytes(received, 1)}</span>{" "}
                <span>↑{formatBytes(metrics.network_tx_bytes?.[iface] ?? 0, 1)}</span>
              </>
            )}
          </span>
        </div>
      ))}
    </>
  );
}

// A live reading, so the fill eases between samples; reduced motion jumps instead.
function ProgressBar({ value, color, label }: { value: number; color: string; label: string }) {
  const clamped = Number.isFinite(value) ? Math.min(Math.max(value, 0), 100) : 0;
  const level = usageLevel(clamped);
  return (
    <div
      aria-label={level === "Normal" ? label : `${label}, ${level.toLowerCase()}`}
      aria-valuemax={100}
      aria-valuemin={0}
      aria-valuenow={Math.round(clamped)}
      className="w-full bg-terminal-border rounded-full h-1.5 overflow-hidden"
      data-level={level.toLowerCase()}
      role="meter"
    >
      <div
        className={cn("h-full rounded-full [transition-property:width] duration-300 motion-reduce:transition-none", color)}
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}
