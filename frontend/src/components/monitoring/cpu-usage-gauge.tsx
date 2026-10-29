"use client";

import { GaugeChart } from "@/components/monitoring/gauge-chart";
import { formatVcpuAmount } from "@/lib/instance-metrics-view";
import type { InstanceRateStatus } from "@/types/api";

export interface CpuUsageReading {
  status: InstanceRateStatus;
  busyPercent: number | null;
  busyThreads: number | null;
  /** Hardware threads behind busyPercent: the Clusters that reported. */
  threads: number;
  reportingInstances: number;
  runningInstances: number;
}

/**
 * CPU utilisation as busy hardware threads, with its state spelled out.
 *
 * note: utilisation only. The allocation line beside it (vCPUs allocated per hardware thread)
 * stays with the caller; this gauge used to show allocation for one Cluster and average usage
 * for several, under the same label.
 */
export function CpuUsageGauge({
  reading,
  label,
  statusMessage,
}: {
  reading: CpuUsageReading;
  label: string;
  statusMessage?: string;
}) {
  const idle = reading.runningInstances === 0;
  const ready = reading.status === "ready" && reading.busyPercent !== null;
  return (
    <>
      <GaugeChart
        value={ready ? reading.busyPercent : null}
        label={label}
        statusMessage={statusMessage}
        emptyLabel={reading.status === "calculating" ? "Calculating…" : "Unavailable"}
      />
      <p className="text-sm text-muted-foreground mt-2 tabular-nums">{caption(reading, idle, ready)}</p>
      {ready && !idle && reading.reportingInstances < reading.runningInstances ? (
        <p className="text-xs text-muted-foreground tabular-nums">
          From {reading.reportingInstances} of {reading.runningInstances} running instances
        </p>
      ) : null}
    </>
  );
}

function caption(reading: CpuUsageReading, idle: boolean, ready: boolean): string {
  if (idle) return "No running instances";
  if (ready && reading.busyThreads !== null && reading.threads > 0) {
    return `${formatVcpuAmount(reading.busyThreads)} of ${reading.threads} hardware threads busy`;
  }
  if (reading.status === "calculating") return "Waiting for a second sample";
  return "Live usage not reported by the cluster";
}
