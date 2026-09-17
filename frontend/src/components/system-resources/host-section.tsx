"use client";

import { Activity, Gauge, HardDrive } from "lucide-react";
import type { HostInfo, OSInfo, PythonInfo } from "@/types";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EMPTY_VALUE } from "@/components/ui/table-text";
import { OverflowText } from "@/components/ui/overflow-text";
import { formatBytes, formatFrequency, formatMemory } from "@/lib/utils";
import { Tile, UsageProgress, formatUptime } from "@/components/system-resources/primitives";

interface HostSectionProps {
  /** Null from a backend older than the split; every card then shows the empty value. */
  host: HostInfo | null;
  os: OSInfo;
  python: PythonInfo;
  /** Inside a container the hostname is the container id and belongs to the other block. */
  containerised: boolean;
}

/**
 * The machine the backend runs on, as its kernel reports it. On an LXD virtual machine this is
 * the VM, so the figures match the instance overview an operator sees in LXD; on bare metal it
 * is the physical host. Nothing here is capped by the container's cgroup.
 */
export function HostSection({ host, os, python, containerised }: HostSectionProps) {
  const kind =
    host?.is_virtual_machine === true
      ? "Virtual Machine"
      : host?.is_virtual_machine === false
        ? "Physical Machine"
        : null;
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-lg font-semibold">Host</h3>
        {kind && <Badge variant="muted">{kind}</Badge>}
        {host?.platform_vendor && (
          <span className="font-mono text-xs text-muted-foreground">{host.platform_vendor}</span>
        )}
        <p className="basis-full text-sm text-muted-foreground">
          The machine this backend runs on, as its kernel reports it. On an LXD virtual machine
          these match the instance overview.
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <HostCpuCard host={host} />
        <HostMemoryCard host={host} />
        <HostStorageCard host={host} />
        <HostSystemCard host={host} os={os} python={python} containerised={containerised} />
      </div>
    </section>
  );
}

function HostCpuCard({ host }: { host: HostInfo | null }) {
  const cpu = host?.cpu ?? null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Host CPU</CardTitle>
        <CardDescription>
          {cpu
            ? `${cpu.count_physical} physical cores, ${cpu.count_logical} logical processors`
            : "Core count not reported by this backend"}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <div className="flex justify-between text-sm">
            <span>Overall Usage</span>
            <span className="font-medium">{cpu ? `${cpu.percent.toFixed(1)}%` : EMPTY_VALUE}</span>
          </div>
          <UsageProgress value={cpu?.percent ?? 0} />
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 pt-2">
          <Tile icon={Gauge} label="Frequency" value={formatFrequency(cpu?.frequency_current)} />
          <Tile icon={Activity} label="Load 1m" value={cpu?.load_avg_1m?.toFixed(2) ?? EMPTY_VALUE} />
          <Tile icon={Activity} label="Load 5m" value={cpu?.load_avg_5m?.toFixed(2) ?? EMPTY_VALUE} />
          <Tile icon={Activity} label="Load 15m" value={cpu?.load_avg_15m?.toFixed(2) ?? EMPTY_VALUE} />
        </div>
      </CardContent>
    </Card>
  );
}

function HostMemoryCard({ host }: { host: HostInfo | null }) {
  const memory = host?.memory ?? null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Host Memory</CardTitle>
        <CardDescription>
          {memory
            ? `${formatMemory(memory.used)} of ${formatMemory(memory.total)} used`
            : "Memory not reported by this backend"}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <div className="flex justify-between text-sm">
            <span>RAM Usage</span>
            <span className="font-medium">{memory ? `${memory.percent.toFixed(1)}%` : EMPTY_VALUE}</span>
          </div>
          <UsageProgress value={memory?.percent ?? 0} />
        </div>
        {memory && memory.swap_total > 0 && (
          <div className="space-y-2">
            <div className="flex justify-between text-sm">
              <span>Swap Usage</span>
              <span className="font-medium">{memory.swap_percent.toFixed(1)}%</span>
            </div>
            <UsageProgress value={memory.swap_percent} />
            <p className="text-xs text-muted-foreground">
              {formatMemory(memory.swap_used)} of {formatMemory(memory.swap_total)}
            </p>
          </div>
        )}
        <div className="grid grid-cols-3 gap-4 pt-2">
          <Tile label="Total" value={memory ? formatMemory(memory.total) : EMPTY_VALUE} />
          <Tile label="Used" value={memory ? formatMemory(memory.used) : EMPTY_VALUE} />
          <Tile label="Available" value={memory ? formatMemory(memory.available) : EMPTY_VALUE} />
        </div>
      </CardContent>
    </Card>
  );
}

function HostStorageCard({ host }: { host: HostInfo | null }) {
  const filesystems = host?.filesystems ?? [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Host Storage</CardTitle>
        <CardDescription>
          The host filesystem behind the backend&apos;s bind mounts, sized as the host sees it.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {filesystems.length === 0 && (
          <p className="text-sm text-muted-foreground">
            {host ? "No host filesystem is visible from this container." : "Not reported by this backend."}
          </p>
        )}
        {filesystems.map((fs) => (
          <div key={`${fs.device}-${fs.total}`} className="space-y-2 p-3 bg-muted/30 rounded-lg">
            <div className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2">
                <HardDrive className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="max-w-[200px] text-sm font-medium">
                  <OverflowText value={fs.device} label="filesystem device" mono />
                </span>
                <Badge variant="outline" className="font-mono text-xs">
                  {fs.fstype}
                </Badge>
              </div>
              <span className="text-sm font-medium">{fs.percent.toFixed(1)}%</span>
            </div>
            <UsageProgress value={fs.percent} />
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>{formatBytes(fs.used)} used</span>
              <span>{formatBytes(fs.free)} free</span>
              <span>{formatBytes(fs.total)} total</span>
            </div>
            {fs.mountpoints.length > 0 && (
              <p className="text-xs text-muted-foreground">
                holds{" "}
                <span className="font-mono">{fs.mountpoints.join(", ")}</span>
              </p>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function HostSystemCard({
  host,
  os,
  python,
  containerised,
}: {
  host: HostInfo | null;
  os: OSInfo;
  python: PythonInfo;
  containerised: boolean;
}) {
  const uptime = host ? formatUptime(host.uptime_seconds) : formatUptime(os.uptime_seconds);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Host System</CardTitle>
        <CardDescription>Kernel, architecture and the Python runtime the backend uses.</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-2 text-sm">
          <div className="p-2 bg-muted/30 rounded">
            <span className="text-muted-foreground">System:</span>{" "}
            <span className="font-medium">{os.system}</span>
          </div>
          <div className="p-2 bg-muted/30 rounded">
            <span className="text-muted-foreground">Kernel:</span>{" "}
            <span className="font-medium font-mono">{os.release}</span>
          </div>
          <div className="p-2 bg-muted/30 rounded">
            <span className="text-muted-foreground">Architecture:</span>{" "}
            <span className="font-medium font-mono">{os.machine}</span>
          </div>
          <div className="p-2 bg-muted/30 rounded">
            <span className="text-muted-foreground">Uptime:</span>{" "}
            <span className="font-medium">{uptime}</span>
          </div>
          {!containerised && (
            <div className="p-2 bg-muted/30 rounded col-span-2">
              <span className="text-muted-foreground">Hostname:</span>{" "}
              <span className="font-medium font-mono">{os.hostname}</span>
            </div>
          )}
          <div className="p-2 bg-muted/30 rounded">
            <span className="text-muted-foreground">Python:</span>{" "}
            <span className="font-medium font-mono">{python.version}</span>
          </div>
          <div className="p-2 bg-muted/30 rounded">
            <span className="text-muted-foreground">Implementation:</span>{" "}
            <span className="font-medium">{python.implementation}</span>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
