"use client";

import { ArrowDownToLine, ArrowUpFromLine, Clock, Cpu, Layers, MemoryStick, Wifi, WifiOff } from "lucide-react";
import type { ContainerInfo, NetworkInterfaceInfo } from "@/types";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EMPTY_VALUE } from "@/components/ui/table-text";
import { formatBytes, formatMemory, formatNetworkRate } from "@/lib/utils";
import { Tile, UsageProgress, formatUptime } from "@/components/system-resources/primitives";
import { ratePerSecond } from "@/components/system-resources/rates";

interface ContainerSectionProps {
  container: ContainerInfo | null;
  network: NetworkInterfaceInfo[];
  /** "container" when the interfaces are the backend's own namespace. */
  networkScope: "container" | "host";
  previous: { network: NetworkInterfaceInfo[]; timestamp: string } | null;
  timestamp: string;
}

/**
 * The backend's own cgroup: the limits the deployment set in compose (BACKEND_MEM_LIMIT,
 * BACKEND_CPUS), what counts against them, and the worker processes. These are the figures
 * `docker stats` prints, and they are what an OOM kill or CPU throttling is measured against.
 */
export function ContainerSection({ container, network, networkScope, previous, timestamp }: ContainerSectionProps) {
  const containerised = Boolean(container?.is_containerized);
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-lg font-semibold">Backend Container</h3>
        {container && (
          <Badge variant="muted">{container.is_containerized ? container.runtime : "no container"}</Badge>
        )}
        {container?.cgroup_version && <Badge variant="muted">cgroup {container.cgroup_version}</Badge>}
        {container?.container_id && (
          <span className="font-mono text-xs text-muted-foreground">ID {container.container_id}</span>
        )}
        <p className="basis-full text-sm text-muted-foreground">
          The backend&apos;s own cgroup. Limits come from BACKEND_MEM_LIMIT and BACKEND_CPUS in
          the deployment&apos;s .env, and the figures match docker stats.
        </p>
      </div>

      {containerised ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <ContainerMemoryCard container={container!} />
          <ContainerCpuCard container={container!} />
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          The backend is not running in a container, so there is no cgroup limit to report.
        </p>
      )}

      <WorkersCard container={container} />
      <NetworkCard network={network} scope={networkScope} previous={previous} timestamp={timestamp} />
    </section>
  );
}

function ContainerMemoryCard({ container }: { container: ContainerInfo }) {
  const m = container.memory;
  const limit = m?.limit ?? container.memory_limit;
  const headline =
    m?.used !== null && m?.used !== undefined
      ? limit
        ? `${formatMemory(m.used)} of ${formatMemory(limit)}`
        : `${formatMemory(m.used)}, no limit set`
      : EMPTY_VALUE;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Container Memory</CardTitle>
        <CardDescription>{headline}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <div className="flex justify-between text-sm">
            <span>Of the limit</span>
            <span className="font-medium">
              {m?.percent !== null && m?.percent !== undefined ? `${m.percent.toFixed(1)}%` : EMPTY_VALUE}
            </span>
          </div>
          <UsageProgress value={m?.percent ?? 0} />
          <p className="text-xs text-muted-foreground">
            Used excludes reclaimable page cache, the same rule docker stats applies.
          </p>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 pt-2">
          <Tile label="Limit" value={limit ? formatMemory(limit) : "No limit set"} />
          <Tile label="Anonymous" value={m?.anon !== null && m?.anon !== undefined ? formatMemory(m.anon) : EMPTY_VALUE} />
          <Tile label="Page Cache" value={m?.file !== null && m?.file !== undefined ? formatMemory(m.file) : EMPTY_VALUE} />
          <Tile label="Used" value={m?.used !== null && m?.used !== undefined ? formatMemory(m.used) : EMPTY_VALUE} />
        </div>
      </CardContent>
    </Card>
  );
}

function ContainerCpuCard({ container }: { container: ContainerInfo }) {
  const c = container.cpu;
  const limit = c?.limit ?? container.cpu_limit;
  const percent = c?.percent ?? null;
  const headline =
    percent !== null
      ? limit
        ? `${percent.toFixed(1)}% of a ${limit}-vCPU quota`
        : `${percent.toFixed(1)}% of the host`
      : EMPTY_VALUE;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Container CPU</CardTitle>
        <CardDescription>{headline}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <div className="flex justify-between text-sm">
            <span>Of the quota</span>
            <span className="font-medium">{percent !== null ? `${percent.toFixed(1)}%` : EMPTY_VALUE}</span>
          </div>
          <UsageProgress value={percent ?? 0} />
          <p className="text-xs text-muted-foreground">
            {c?.window_seconds && c.window_seconds >= 0.5
              ? `Averaged over the last ${c.window_seconds.toFixed(1)} s by the worker that answered this poll.`
              : "Averaged between two polls by the worker that answers them."}
          </p>
        </div>
        <div className="grid grid-cols-3 gap-4 pt-2">
          <Tile label="vCPU Quota" value={limit ? `${limit} vCPU` : "No quota set"} />
          <Tile label="Throttled Periods" value={c?.nr_throttled ?? EMPTY_VALUE} />
          <Tile
            label="Throttled Time"
            value={c?.throttled_usec !== null && c?.throttled_usec !== undefined ? `${(c.throttled_usec / 1e6).toFixed(1)} s` : EMPTY_VALUE}
          />
        </div>
      </CardContent>
    </Card>
  );
}

function WorkersCard({ container }: { container: ContainerInfo | null }) {
  const w = container?.workers ?? null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Workers</CardTitle>
        <CardDescription>
          Uvicorn worker processes inside the backend. Requests are spread across them, so the
          answering PID changes between polls.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Tile
            icon={Layers}
            label="Workers"
            value={w ? w.count : EMPTY_VALUE}
            sub={w ? (w.expected !== null ? `of ${w.expected} expected` : "WEB_CONCURRENCY not set") : undefined}
          />
          <Tile icon={MemoryStick} label="Total Memory" value={w ? formatMemory(w.total_rss) : EMPTY_VALUE} sub="resident, all workers" />
          <Tile icon={Cpu} label="Answered By" value={w ? `PID ${w.answering_pid}` : EMPTY_VALUE} sub="this poll" />
          <Tile
            icon={Clock}
            label="Master Uptime"
            value={w?.master_uptime_seconds !== null && w?.master_uptime_seconds !== undefined ? formatUptime(w.master_uptime_seconds) : EMPTY_VALUE}
            sub={w?.master_pid !== null && w?.master_pid !== undefined ? `PID ${w.master_pid}` : "no master process"}
          />
        </div>
        {w && w.workers.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {w.workers.map((p) => (
              <Badge key={p.pid} variant="outline" className="font-mono text-xs">
                PID {p.pid} {formatMemory(p.memory_rss)} up {formatUptime(p.uptime_seconds)}
              </Badge>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function NetworkCard({
  network,
  scope,
  previous,
  timestamp,
}: {
  network: NetworkInterfaceInfo[];
  scope: "container" | "host";
  previous: { network: NetworkInterfaceInfo[]; timestamp: string } | null;
  timestamp: string;
}) {
  const shown = network.filter((iface) => iface.bytes_sent > 0 || iface.bytes_recv > 0 || iface.is_up);
  return (
    <Card>
      <CardHeader>
        <CardTitle>{scope === "container" ? "Container Network" : "Host Network"}</CardTitle>
        <CardDescription>
          {scope === "container"
            ? "Interfaces inside the backend's network namespace. The host's own interfaces are not visible from here."
            : "Interfaces of the machine running this backend."}{" "}
          {network.filter((n) => n.is_up).length} of {network.length} active.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {shown.map((iface) => {
            const prev = previous?.network.find((p) => p.name === iface.name);
            const rx = ratePerSecond(prev?.bytes_recv, iface.bytes_recv, previous?.timestamp, timestamp);
            const tx = ratePerSecond(prev?.bytes_sent, iface.bytes_sent, previous?.timestamp, timestamp);
            return (
              <div key={iface.name} className="p-3 bg-muted/30 rounded-lg space-y-2">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    {iface.is_up ? (
                      <Wifi className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                    ) : (
                      <WifiOff className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                    )}
                    <span className="font-medium font-mono">{iface.name}</span>
                  </div>
                  <Badge variant={iface.is_up ? "default" : "secondary"}>{iface.is_up ? "UP" : "DOWN"}</Badge>
                </div>
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div className="flex items-center gap-2">
                    <ArrowDownToLine className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                    <div>
                      <p className="text-muted-foreground">Received</p>
                      <p className="font-medium">{formatBytes(iface.bytes_recv)}</p>
                      {rx !== null && <p className="text-xs text-muted-foreground">{formatNetworkRate(rx)}</p>}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <ArrowUpFromLine className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                    <div>
                      <p className="text-muted-foreground">Sent</p>
                      <p className="font-medium">{formatBytes(iface.bytes_sent)}</p>
                      {tx !== null && <p className="text-xs text-muted-foreground">{formatNetworkRate(tx)}</p>}
                    </div>
                  </div>
                </div>
                {iface.addresses.length > 0 && (
                  <div className="flex flex-wrap gap-1 pt-1">
                    {iface.addresses.map((addr) => (
                      <Badge key={addr} variant="outline" className="font-mono text-xs">
                        {addr}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
