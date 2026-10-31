"use client";

import { HardDrive } from "lucide-react";
import type { HostFilesystem, HostInfo, OSInfo } from "@/types";
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
import { UsageBar } from "@/components/ui/usage-bar";
import { usageLevel, usagePercent } from "@/lib/usage-tone";
import { formatBytes, formatDateTime, formatFrequency, plural } from "@/lib/utils";
import {
  Fact,
  FactList,
  MeterRow,
  StatusPercent,
  Tile,
  formatUptime,
} from "@/components/system-resources/primitives";
import { formatWindow, type WindowedReading } from "@/components/system-resources/rates";

interface HostSectionProps {
  /** Null from a backend older than the split; every card then shows the empty value. */
  host: HostInfo | null;
  os: OSInfo;
  cpu: WindowedReading | null;
  /** Inside a container the hostname is the container id and belongs to the other block. */
  containerised: boolean;
}

// The bind mounts the compose file gives the backend, named for what an operator keeps there.
const MOUNT_PURPOSE: Record<string, string> = {
  "/app/uploads": "Uploads",
  "/app/config": "Configuration",
};

/**
 * The machine the backend runs on, as its kernel reports it. On an LXD virtual machine this is
 * the VM, so the figures match the instance overview an operator sees in LXD; on bare metal it
 * is the physical host. Nothing here is capped by the container's cgroup.
 */
export function HostSection({ host, os, cpu, containerised }: HostSectionProps) {
  const kind =
    host?.is_virtual_machine === true
      ? "Virtual Machine"
      : host?.is_virtual_machine === false
        ? "Physical Machine"
        : null;
  return (
    <section className="space-y-4" aria-labelledby="host-machine-heading">
      <div className="flex flex-wrap items-center gap-2">
        <h3 id="host-machine-heading" className="text-lg font-semibold">Host Machine</h3>
        {kind && <Badge variant="muted">{kind}</Badge>}
        {host?.platform_vendor && (
          <span className="font-mono text-xs text-muted-foreground">{host.platform_vendor}</span>
        )}
        <p className="basis-full text-sm text-muted-foreground">
          The server this dashboard runs on, as its operating system reports it. On an LXD
          virtual machine these figures match the instance overview in LXD.
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <HostCpuCard host={host} reading={cpu} />
        <HostMemoryCard host={host} />
        <HostStorageCard host={host} containerised={containerised} />
        <HostSystemCard host={host} os={os} containerised={containerised} />
      </div>
    </section>
  );
}

function HostCpuCard({ host, reading }: { host: HostInfo | null; reading: WindowedReading | null }) {
  const cpu = host?.cpu ?? null;
  const virtual = host?.is_virtual_machine === true;
  const unit = virtual ? "vCPU" : "hardware thread";
  // a backend older than the counters sends no cpu_times, and its own percent has no stated window
  const canMeasure = Boolean(cpu && host?.cpu_times);
  const load = (value: number | null | undefined) => (value === null || value === undefined ? EMPTY_VALUE : value.toFixed(2));
  return (
    <Card>
      <CardHeader>
        <CardTitle>Host CPU</CardTitle>
        <CardDescription>
          {!cpu
            ? "CPU not reported by this backend"
            : virtual
              ? plural(cpu.count_logical, "vCPU")
              : `${plural(cpu.count_physical, "core")}, ${plural(cpu.count_logical, "hardware thread")}`}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <MeterRow
          label="Usage"
          percent={cpu ? reading?.percent ?? null : null}
          measuring={canMeasure}
          caption={
            !cpu
              ? undefined
              : reading
                ? `Busy share of all ${plural(cpu.count_logical, unit)} over the last ${formatWindow(reading.windowSeconds)}.`
                : canMeasure
                  ? "Measured between two updates. The first reading arrives with the next one, in about 5s."
                  : "This backend does not report CPU counters, so usage cannot be measured."
          }
        />
        <div className="space-y-2 pt-2">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Tile label="Clock Speed" value={formatFrequency(cpu?.frequency_current)} />
            <Tile label="Load 1 Min" value={load(cpu?.load_avg_1m)} />
            <Tile label="Load 5 Min" value={load(cpu?.load_avg_5m)} />
            <Tile label="Load 15 Min" value={load(cpu?.load_avg_15m)} />
          </div>
          {cpu && (
            <p className="text-xs text-muted-foreground">
              Load counts processes running or waiting for a CPU. Up to {cpu.count_logical} (one
              per {unit}) means nothing is waiting.
            </p>
          )}
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
            ? `${formatBytes(memory.used)} of ${formatBytes(memory.total)} in use`
            : "Memory not reported by this backend"}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <MeterRow label="Usage" percent={memory ? memory.percent : null} />
        {memory && memory.swap_total > 0 && (
          <MeterRow
            label="Swap"
            percent={memory.swap_percent}
            caption={`${formatBytes(memory.swap_used)} of ${formatBytes(memory.swap_total)} swapped out to disk.`}
          />
        )}
        {memory && memory.swap_total === 0 && (
          <p className="text-xs text-muted-foreground">No swap configured, so nothing is paged out to disk.</p>
        )}
        <div className="grid grid-cols-3 gap-4 pt-2">
          <Tile label="Total" value={memory ? formatBytes(memory.total) : EMPTY_VALUE} />
          <Tile label="In Use" value={memory ? formatBytes(memory.used) : EMPTY_VALUE} />
          <Tile
            label="Available"
            value={memory ? formatBytes(memory.available) : EMPTY_VALUE}
            sub="free plus reclaimable cache"
          />
        </div>
      </CardContent>
    </Card>
  );
}

function HostStorageCard({ host, containerised }: { host: HostInfo | null; containerised: boolean }) {
  const filesystems = host?.filesystems ?? [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Host Storage</CardTitle>
        <CardDescription>
          {containerised
            ? "The host disk behind the dashboard's uploads and configuration, sized as the host sees it."
            : "Disks mounted on the host machine."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {filesystems.length === 0 && (
          <p className="text-sm text-muted-foreground">
            {host ? "No host filesystem is visible from this container." : "Not reported by this backend."}
          </p>
        )}
        {filesystems.map((fs) => (
          <FilesystemRow key={`${fs.device}-${fs.total}`} fs={fs} />
        ))}
      </CardContent>
    </Card>
  );
}

function FilesystemRow({ fs }: { fs: HostFilesystem }) {
  // ext4 keeps blocks back for root by default; they count as neither used nor free, which is
  // why df's percent is used over (used + free) and the three figures do not sum to the total.
  const reserved = fs.total - fs.used - fs.free;
  const showReserved = fs.total > 0 && reserved / fs.total > 0.005;
  return (
    <div className="space-y-2 p-3 bg-muted/30 rounded-lg">
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
        <StatusPercent percent={fs.percent} />
      </div>
      <UsageBar
        percent={usagePercent(fs.percent, 100)}
        label={`Disk ${fs.device}`}
        valueText={`${fs.percent.toFixed(1)}% full, ${usageLevel(fs.percent).toLowerCase()}`}
      />
      <div className="flex flex-wrap justify-between gap-x-4 text-xs text-muted-foreground">
        <span>{formatBytes(fs.used)} used</span>
        <span>{formatBytes(fs.free)} free</span>
        <span>
          {formatBytes(fs.total)} total{showReserved && `, ${formatBytes(reserved)} reserved`}
        </span>
      </div>
      {fs.mountpoints.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Holds{" "}
          {fs.mountpoints.map((path, i) => (
            <span key={path}>
              {i > 0 && ", "}
              {MOUNT_PURPOSE[path] ? (
                <>
                  {MOUNT_PURPOSE[path]} (<span className="font-mono">{path}</span>)
                </>
              ) : (
                <span className="font-mono">{path}</span>
              )}
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

function HostSystemCard({ host, os, containerised }: { host: HostInfo | null; os: OSInfo; containerised: boolean }) {
  const uptime = host ? host.uptime_seconds : os.uptime_seconds;
  const bootedAt = host?.boot_time ?? null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Host System</CardTitle>
        <CardDescription>Operating system and kernel of the host machine.</CardDescription>
      </CardHeader>
      <CardContent>
        <FactList>
          <Fact label="Operating System">{os.system}</Fact>
          <Fact label="Kernel">
            <span className="font-mono">{os.release}</span>
          </Fact>
          <Fact label="Architecture">
            <span className="font-mono">{os.machine}</span>
          </Fact>
          <Fact label="Uptime">
            {formatUptime(uptime)}
            {bootedAt && (
              <span className="block text-xs font-normal text-muted-foreground">since {formatDateTime(bootedAt)}</span>
            )}
          </Fact>
          {!containerised && (
            <Fact label="Hostname" className="sm:col-span-2">
              <span className="font-mono">{os.hostname}</span>
            </Fact>
          )}
        </FactList>
      </CardContent>
    </Card>
  );
}
