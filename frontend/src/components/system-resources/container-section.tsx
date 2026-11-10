"use client";

import type { ContainerInfo, NetworkInterfaceInfo, PythonInfo } from "@/types";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EMPTY_VALUE } from "@/components/ui/table-text";
import { formatBytes, plural, formatCount } from "@/lib/utils";
import { MeterRow, Tile, formatUptime } from "@/components/system-resources/primitives";
import {
  formatWindow,
  type ThrottleReading,
  type WindowedReading,
} from "@/components/system-resources/rates";
import { WorkersCard } from "@/components/system-resources/workers-card";
import { NetworkCard } from "@/components/system-resources/network-card";

interface ContainerSectionProps {
  container: ContainerInfo | null;
  python: PythonInfo;
  /** Host logical CPUs: what an unlimited container's CPU percent is measured against. */
  hostCpus: number | null;
  cpu: WindowedReading | null;
  throttle: ThrottleReading | null;
  network: NetworkInterfaceInfo[];
  /** "container" when the interfaces are the backend's own namespace. */
  networkScope: "container" | "host";
  previousNetwork: { network: NetworkInterfaceInfo[]; timestamp: string } | null;
  timestamp: string;
}

const RUNTIME_LABEL: Record<string, string> = { docker: "Docker", lxc: "LXC" };

/**
 * The backend's own cgroup: the limits the deployment set in compose (BACKEND_MEM_LIMIT,
 * BACKEND_CPUS), what counts against them, and the worker processes. These are the figures
 * `docker stats` prints, and they are what an OOM kill or CPU throttling is measured against.
 */
export function ContainerSection({
  container,
  python,
  hostCpus,
  cpu,
  throttle,
  network,
  networkScope,
  previousNetwork,
  timestamp,
}: ContainerSectionProps) {
  const containerised = Boolean(container?.is_containerized);
  return (
    <section className="space-y-4" aria-labelledby="backend-container-heading">
      <div className="flex flex-wrap items-center gap-2">
        <h3 id="backend-container-heading" className="text-lg font-semibold">Backend Container</h3>
        {container && (
          <Badge variant="muted">
            {container.is_containerized ? RUNTIME_LABEL[container.runtime] ?? container.runtime : "No Container"}
          </Badge>
        )}
        {container?.cgroup_version && <Badge variant="muted">cgroup {container.cgroup_version}</Badge>}
        {container?.container_id && (
          <span className="font-mono text-xs text-muted-foreground">Container ID {container.container_id}</span>
        )}
        <p className="basis-full text-sm text-muted-foreground">
          {containerised
            ? "The container that runs the dashboard's API. Its limits come from BACKEND_MEM_LIMIT and BACKEND_CPUS in the deployment's .env. When memory reaches the limit the kernel stops a worker, and at the CPU limit requests slow down."
            : "The dashboard's API runs directly on the host machine, not in a container, so there is no container limit to report."}
        </p>
      </div>

      {containerised && container && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <ContainerMemoryCard container={container} />
          <ContainerCpuCard container={container} hostCpus={hostCpus} reading={cpu} throttle={throttle} />
        </div>
      )}

      <WorkersCard workers={container?.workers ?? null} python={python} />
      <NetworkCard network={network} scope={networkScope} previous={previousNetwork} timestamp={timestamp} />
    </section>
  );
}

function bytesOrEmpty(value: number | null | undefined): string {
  return value === null || value === undefined ? EMPTY_VALUE : formatBytes(value);
}

function ContainerMemoryCard({ container }: { container: ContainerInfo }) {
  const m = container.memory;
  const limit = m?.limit ?? container.memory_limit;
  const used = m?.used ?? null;
  const headline =
    used === null
      ? limit
        ? `${formatBytes(limit)} limit`
        : "No limit set"
      : limit
        ? `${formatBytes(used)} of ${formatBytes(limit)} limit, ${formatBytes(Math.max(limit - used, 0))} headroom`
        : `${formatBytes(used)} in use, no limit set`;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Container Memory</CardTitle>
        <CardDescription>{headline}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {limit ? (
          <MeterRow
            label="Usage of the Limit"
            percent={m?.percent ?? null}
            caption="Counts what the backend holds. Cache the kernel can reclaim is left out, the way docker stats counts it."
          />
        ) : (
          <p className="text-sm text-muted-foreground">
            No memory limit is set, so the backend can use all of the host machine&apos;s memory.
          </p>
        )}
        <div className="space-y-2 pt-2">
          <div className="grid grid-cols-2 gap-4">
            <Tile label="In Use" value={bytesOrEmpty(used)} sub="counted against the limit" />
            <Tile label="Limit" value={limit ? formatBytes(limit) : "No limit set"} sub="set in .env" />
            <Tile label="Process Memory" value={bytesOrEmpty(m?.anon)} sub="heap and stacks" />
            <Tile label="File Cache" value={bytesOrEmpty(m?.file)} sub="partly reclaimable" />
          </div>
          <p className="text-xs text-muted-foreground">
            Process memory and file cache are parts of the container&apos;s total. In Use is that
            total minus the reclaimable cache, so the two do not add up to it.
          </p>
        </div>
      </CardContent>
    </Card>
  );
}

function throttledTime(usec: number): string {
  const seconds = usec / 1e6;
  return seconds < 60 ? `${seconds.toFixed(1)}s` : formatUptime(seconds);
}

function ContainerCpuCard({
  container,
  hostCpus,
  reading,
  throttle,
}: {
  container: ContainerInfo;
  hostCpus: number | null;
  reading: WindowedReading | null;
  throttle: ThrottleReading | null;
}) {
  const c = container.cpu;
  const limit = c?.limit ?? container.cpu_limit;
  const headline = limit
    ? reading
      ? `${reading.percent.toFixed(1)}% of the ${limit} vCPU limit`
      : `${limit} vCPU limit`
    : hostCpus
      ? `No limit set, measured against the host's ${plural(hostCpus, "CPU")}`
      : "No limit set";
  const windowText = reading
    ? `Over the last ${formatWindow(reading.windowSeconds)}.`
    : "Measured between two updates. The first reading arrives with the next one, in about 5s.";
  const throttleText =
    !limit || !throttle
      ? ""
      : throttle.count === 0
        ? ` Not throttled in the last ${formatWindow(throttle.windowSeconds)}.`
        : ` Throttled ${plural(throttle.count, "time")} in the last ${formatWindow(throttle.windowSeconds)}: the backend reached its CPU limit.`;
  const since = "since container start";
  // Without a readable cpu.stat there is nothing to measure, so no reading is on its way.
  const counting = c?.usage_usec !== null && c?.usage_usec !== undefined;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Container CPU</CardTitle>
        <CardDescription>{headline}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <MeterRow
          label={limit ? "Usage of the Limit" : "Usage of the Host"}
          percent={reading?.percent ?? null}
          measuring={counting}
          caption={counting ? `${windowText}${throttleText}` : "The container's CPU counter is not readable here."}
        />
        <div className="grid grid-cols-3 gap-4 pt-2">
          <Tile label="vCPU Limit" value={limit ? plural(limit, "vCPU") : "No limit set"} sub="set in .env" />
          <Tile
            label="Times Throttled"
            value={c?.nr_throttled !== null && c?.nr_throttled !== undefined ? formatCount(c.nr_throttled) : EMPTY_VALUE}
            sub={since}
          />
          <Tile
            label="Time Throttled"
            value={c?.throttled_usec !== null && c?.throttled_usec !== undefined ? throttledTime(c.throttled_usec) : EMPTY_VALUE}
            sub={since}
          />
        </div>
      </CardContent>
    </Card>
  );
}
