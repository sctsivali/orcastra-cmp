"use client";

import type { LucideIcon } from "lucide-react";
import { Activity, Cpu, HardDrive, Network } from "lucide-react";
import { Card } from "@/components/ui/card";
import { UsageMeter } from "@/components/ui/usage-meter";
import { DetailField, DetailGrid } from "@/components/detail/detail-field";
import {
  cpuReading,
  formatSampleWindow,
  formatVcpuAmount,
  formatVcpuCount,
  memoryReading,
} from "@/lib/instance-metrics-view";
import {
  formatByteQuota,
  formatBytes,
  formatMemory,
  formatMemoryQuota,
  formatNetworkRate,
} from "@/lib/utils";
import type { InstanceMetrics } from "@/types/api";

function CardHeading({ icon: Icon, title }: { icon: LucideIcon; title: string }) {
  return (
    <div className="flex items-center gap-2">
      <Icon className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
      <h3 className="font-semibold">{title}</h3>
    </div>
  );
}

function Pending({ value, detail }: { value: string; detail: string }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <span className="text-2xl font-semibold tabular-nums">{value}</span>
      <span className="text-xs text-muted-foreground">{detail}</span>
    </div>
  );
}

export function CpuCard({ metrics }: { metrics: InstanceMetrics }) {
  const reading = cpuReading(metrics);
  const window = formatSampleWindow(metrics.sample_window_seconds);
  return (
    <Card className="space-y-3 p-4">
      <CardHeading icon={Cpu} title="CPU" />
      {reading.state === "calculating" ? (
        <Pending value="Calculating…" detail="Waiting for a second live sample" />
      ) : reading.state === "unavailable" ? (
        <Pending
          value="Unavailable"
          detail={
            reading.busy !== null
              ? `${formatVcpuAmount(reading.busy)} vCPUs busy; the vCPU count was not reported`
              : "Not reported by the cluster"
          }
        />
      ) : (
        <div className="space-y-1">
          <UsageMeter
            label="Usage"
            used={reading.percent}
            total={100}
            value={`${reading.percent.toFixed(1)}%`}
          />
          {reading.busy !== null && reading.vcpus ? (
            <p className="text-xs text-muted-foreground tabular-nums">
              {formatVcpuAmount(reading.busy)} of {formatVcpuCount(reading.vcpus)} busy
              {window ? ` · ${window}` : ""}
            </p>
          ) : null}
        </div>
      )}
    </Card>
  );
}

export function MemoryCard({ metrics }: { metrics: InstanceMetrics }) {
  const reading = memoryReading(metrics);
  return (
    <Card className="space-y-3 p-4">
      <CardHeading icon={Activity} title="Memory" />
      {!reading ? (
        <p className="text-sm text-muted-foreground">Memory usage was not reported by the cluster.</p>
      ) : reading.total > 0 ? (
        <>
          {/* important: UsageMeter, not Progress. Progress paints the product accent and ships
              no accessible name, so a bar announced only "progress bar, 92 percent". */}
          <UsageMeter
            label="Used"
            total={reading.total}
            used={reading.used}
            value={`${quota(formatMemoryQuota(reading.used, reading.total, 2))} (${reading.percent.toFixed(1)}%)`}
          />
          {reading.available !== null || reading.cache !== null ? (
            <DetailGrid cols={2}>
              {reading.available !== null ? (
                <DetailField label="Available" value={formatMemory(reading.available)} />
              ) : null}
              {reading.cache !== null ? (
                <DetailField label="Page Cache" value={formatMemory(reading.cache)} />
              ) : null}
            </DetailGrid>
          ) : null}
          {reading.includesCache ? (
            <p className="text-xs text-muted-foreground">
              Includes page cache the guest can reclaim, so it can read higher than the memory
              its programs hold.
            </p>
          ) : null}
        </>
      ) : (
        <p className="text-sm font-medium tabular-nums">
          {formatMemory(reading.used)} used, no limit reported
        </p>
      )}
    </Card>
  );
}

export function DiskCard({ metrics }: { metrics: InstanceMetrics }) {
  const devices = Object.entries(metrics.disk_usage_bytes ?? {});
  const totals = metrics.disk_total_bytes ?? {};
  const ioStatus = metrics.disk_io_status ?? "unavailable";
  if (devices.length === 0 && ioStatus === "unavailable") return null;
  const ioWindow = formatSampleWindow(metrics.disk_io_window_seconds);
  return (
    <Card className="space-y-3 p-4">
      <CardHeading icon={HardDrive} title="Disk" />
      {devices.map(([device, used]) =>
        totals[device] ? (
          <UsageMeter
            key={device}
            label={device}
            used={used}
            total={totals[device]}
            value={quota(formatByteQuota(used, totals[device], 2))}
          />
        ) : (
          <div key={device} className="flex justify-between text-sm">
            <span className="text-muted-foreground">{device}</span>
            <span className="font-semibold tabular-nums">{formatBytes(used)} used</span>
          </div>
        ),
      )}
      {ioStatus === "calculating" ? (
        <Pending value="Calculating…" detail="Disk I/O needs a second sample" />
      ) : ioStatus === "ready" ? (
        <div className="space-y-1">
          <DetailGrid cols={2}>
            <DetailField label="Read" value={formatNetworkRate(metrics.disk_read_bytes_per_second ?? 0)} />
            <DetailField label="Write" value={formatNetworkRate(metrics.disk_write_bytes_per_second ?? 0)} />
          </DetailGrid>
          {ioWindow ? (
            <p className="text-xs text-muted-foreground">Block device I/O, {ioWindow}</p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

export function NetworkCard({ metrics }: { metrics: InstanceMetrics }) {
  const interfaces = Object.keys(metrics.network_rx_bytes ?? {});
  if (interfaces.length === 0) return null;
  const rx = metrics.network_rx_bytes_per_second ?? {};
  const tx = metrics.network_tx_bytes_per_second ?? {};
  const window = formatSampleWindow(metrics.sample_window_seconds);
  return (
    <Card className="space-y-3 p-4">
      <CardHeading icon={Network} title="Network" />
      {interfaces.map((iface) => (
        <div key={iface} className="space-y-1">
          <div className="border-b pb-1 text-sm font-medium">{iface}</div>
          <DetailGrid cols={2}>
            <DetailField
              label="Receive"
              value={<RateValue rate={rx[iface]} total={metrics.network_rx_bytes[iface]} />}
            />
            <DetailField
              label="Transmit"
              value={<RateValue rate={tx[iface]} total={metrics.network_tx_bytes?.[iface] ?? 0} />}
            />
          </DetailGrid>
        </div>
      ))}
      {window ? <p className="text-xs text-muted-foreground">Rates over the {window}</p> : null}
    </Card>
  );
}

function RateValue({ rate, total }: { rate: number | undefined; total: number }) {
  return (
    <span className="flex flex-col">
      <span className="tabular-nums">
        {rate === undefined ? "Calculating…" : formatNetworkRate(rate)}
      </span>
      <span className="text-xs text-muted-foreground tabular-nums">
        {formatBytes(total)} since start
      </span>
    </span>
  );
}

function quota({ value, unit }: { value: string; unit: string }): string {
  return unit ? `${value} ${unit}` : value;
}
