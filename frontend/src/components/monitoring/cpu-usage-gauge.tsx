"use client";

import { GaugeChart } from "@/components/monitoring/gauge-chart";
import { formatVcpuAmount } from "@/lib/instance-metrics-view";
import type { InstanceRateStatus } from "@/types/api";
import { plural } from "@/lib/utils";

export interface CpuUsageReading {
  status: InstanceRateStatus;
  busyPercent: number | null;
  busyThreads: number | null;
  /** CPU threads behind busyPercent: the Clusters that reported. */
  threads: number;
  reportingInstances: number;
  runningInstances: number;
}

/**
 * CPU utilisation as busy CPU threads, with its state spelled out.
 *
 * Utilisation only. The allocation line beside it (vCPUs allocated per CPU thread) stays with
 * the caller, so one label never mixes allocation and usage.
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
          From {reading.reportingInstances} of {plural(reading.runningInstances, "running instance")}
        </p>
      ) : null}
    </>
  );
}

function caption(reading: CpuUsageReading, idle: boolean, ready: boolean): string {
  if (idle) return "No running instances";
  if (ready && reading.busyThreads !== null && reading.threads > 0) {
    return `${formatVcpuAmount(reading.busyThreads)} of ${plural(reading.threads, "CPU thread")} busy`;
  }
  if (reading.status === "calculating") return "Waiting for a second sample";
  return "Live usage not reported by the cluster";
}
