"use client";
import {
  Activity,
  AlertTriangle,
  Cpu,
  HardDrive,
  LockKeyhole,
  Network,
} from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card } from "@/components/ui/card";
import { UsageMeter } from "@/components/ui/usage-meter";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { LiveTimestamp } from "@/components/ui/live-timestamp";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { RefreshButton } from "@/components/ui/refresh-button";
import { LoadingLottieMedium } from "@/components/loading-lottie";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { STALE_AFTER_MS } from "@/lib/query-config";
import { formatBytes, formatMemory } from "@/lib/utils";
import {
  APIError,
  type InstanceMetricSection,
  type InstanceMetrics,
  type VMInfo,
} from "@/types/api";

const METRIC_SECTION_LABELS: Record<InstanceMetricSection, string> = {
  cpu: "CPU",
  memory: "Memory",
  disk: "Disk",
  network: "Network",
  processes: "Processes",
};

// The live-metrics half of the instance dialog. The query keeps the last successful sample while
// a refresh is failing, so this panel must present freshness and failure separately rather than
// replacing useful readings with a generic error screen.
export function InstanceMetricsTab({
  instance,
  metrics,
  error,
  isError,
  isFetching,
  isLoading,
  onRefresh,
  updatedAt,
}: {
  instance: VMInfo;
  metrics?: InstanceMetrics;
  error: unknown;
  isError: boolean;
  isFetching: boolean;
  isLoading: boolean;
  onRefresh: () => Promise<unknown> | unknown;
  updatedAt: number | null;
}) {
  // A disabled React Query keeps its cached data. Once the detail/list record says the instance
  // is no longer Running, an older Running metrics sample must not revive the panel. In the
  // other direction, a fresh Stopped metrics response is authoritative over a stale Running row.
  const runtimeStatus =
    instance.status === "Running" ? metrics?.status ?? instance.status : instance.status;
  const canRecoverStoppedMetrics =
    instance.status === "Running" && !!metrics && metrics.status !== "Running";
  const errorState = isError ? describeMetricsError(error) : null;

  if (runtimeStatus !== "Running") {
    return (
      <EmptyState
        icon={Activity}
        title={
          canRecoverStoppedMetrics
            ? "Latest metrics report instance is not running"
            : "Instance is not running"
        }
        description={
          canRecoverStoppedMetrics
            ? "The instance may have started after this sample. Refresh once to check its current runtime state."
            : "Start the instance to see live CPU, memory, disk and network figures."
        }
        action={
          canRecoverStoppedMetrics ? (
            <RefreshButton
              hideTimestamp
              label="Check instance metrics again"
              onRefresh={onRefresh}
            />
          ) : undefined
        }
      />
    );
  }
  if (isLoading && !metrics) {
    return (
      <div className="flex justify-center py-8">
        <LoadingLottieMedium />
        <span className="sr-only">Loading live metrics</span>
      </div>
    );
  }

  if (errorState && !metrics) {
    return (
      <EmptyState
        icon={errorState.permissionDenied ? LockKeyhole : AlertTriangle}
        title={errorState.title}
        description={errorState.description}
        action={
          errorState.permissionDenied ? undefined : (
            <RefreshButton
              hideTimestamp
              label="Retry instance metrics"
              onRefresh={onRefresh}
            />
          )
        }
      />
    );
  }

  if (!metrics) {
    return (
      <EmptyState
        icon={Activity}
        title="No metrics available"
        description="The cluster returned no live readings for this instance."
        action={
          <RefreshButton hideTimestamp label="Retry instance metrics" onRefresh={onRefresh} />
        }
      />
    );
  }

  const memoryPercent = Math.min(metrics.memory_usage_percent ?? 0, 100);
  const interfaces = Object.keys(metrics.network_rx_bytes ?? {});
  const disks = Object.entries(metrics.disk_usage_bytes ?? {});
  const unavailableSections = new Set(metrics.unavailable_sections);
  const allSectionsUnavailable = (
    Object.keys(METRIC_SECTION_LABELS) as InstanceMetricSection[]
  ).every((section) => unavailableSections.has(section));
  const isPartialRead = unavailableSections.size > 0 && !allSectionsUnavailable;
  const cpuUnavailable =
    metrics.cpu_sample_status === "unavailable" || unavailableSections.has("cpu");
  const cpuCalculating = metrics.cpu_sample_status === "calculating";
  const memoryUnavailable = unavailableSections.has("memory");
  const processesUnavailable = unavailableSections.has("processes");

  if (allSectionsUnavailable) {
    return (
      <div className="space-y-4">
        <MetricsToolbar
          errorState={errorState}
          isFetching={isFetching}
          onRefresh={onRefresh}
          updatedAt={updatedAt}
        />
        {errorState ? <MetricsErrorAlert errorState={errorState} /> : null}
        <EmptyState
          className="py-8"
          description="The instance is running, but the cluster reports no CPU, memory, disk or network figures. A missing guest agent is the usual cause."
          icon={Activity}
          title="No readings reported"
        />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <MetricsToolbar
        errorState={errorState}
        isFetching={isFetching}
        onRefresh={onRefresh}
        updatedAt={updatedAt}
      />

      {errorState ? <MetricsErrorAlert errorState={errorState} /> : null}

      {isPartialRead ? (
        <PartialReadNotice>
          Some live readings are unavailable: {formatSectionList(metrics.unavailable_sections)}.
          Available sections remain current.
        </PartialReadNotice>
      ) : null}

      <DetailSection title="Runtime">
        <DetailGrid cols={4}>
          <DetailField
            label="Processes"
            value={processesUnavailable ? undefined : metrics.processes}
          />
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
          <span className="text-3xl font-bold tabular-nums">
            {cpuCalculating
              ? "Calculating…"
              : cpuUnavailable
                ? "Unavailable"
              : `${(metrics.cpu_usage_percent ?? 0).toFixed(1)}%`}
          </span>
          <span className="text-xs text-muted-foreground">
            {cpuCalculating
              ? "waiting for a second live sample"
              : cpuUnavailable
                ? "not reported by the cluster"
                : "across all vCPUs"}
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
        {memoryUnavailable ? (
          <p className="text-sm text-muted-foreground">
            Memory usage was not reported by the cluster.
          </p>
        ) : metrics.memory_total_bytes > 0 ? (
          <UsageMeter
            label="Memory"
            total={metrics.memory_total_bytes}
            used={metrics.memory_used_bytes}
            value={`${formatMemory(metrics.memory_used_bytes)} / ${formatMemory(
              metrics.memory_total_bytes,
            )} (${memoryPercent.toFixed(1)}%)`}
          />
        ) : (
          <p className="text-sm font-medium tabular-nums">
            {formatMemory(metrics.memory_used_bytes)} / {formatMemory(metrics.memory_total_bytes)}
          </p>
        )}
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

function formatSectionList(sections: InstanceMetricSection[]): string {
  const labels = sections.map((section) => METRIC_SECTION_LABELS[section]);
  if (labels.length <= 1) return labels[0] ?? "Metrics";
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
  return `${labels.slice(0, -1).join(", ")}, and ${labels.at(-1)}`;
}

interface MetricsErrorState {
  title: string;
  description: string;
  permissionDenied: boolean;
  staleReason: string;
}

function describeMetricsError(error: unknown): MetricsErrorState {
  const status = error instanceof APIError ? error.status : null;
  if (status === 401) {
    return {
      title: "Session expired",
      description: "Sign in again before requesting live metrics.",
      permissionDenied: true,
      staleReason: "session expired",
    };
  }
  if (status === 403) {
    return {
      title: "Metrics access denied",
      description: "Your current role cannot view metrics for this instance.",
      permissionDenied: true,
      staleReason: "access denied",
    };
  }
  if (status === 429) {
    return {
      title: "Metrics temporarily limited",
      description: "The request budget was reached. Wait a moment before refreshing.",
      permissionDenied: false,
      staleReason: "request limited",
    };
  }
  if (status === 503 || status === 504 || status === 0) {
    return {
      title: "Cluster did not answer",
      description: "The instance may still be running. Check cluster connectivity and retry.",
      permissionDenied: false,
      staleReason: "cluster did not answer",
    };
  }
  return {
    title: "Metrics could not be refreshed",
    description: "The latest request failed. Retry after checking the cluster status.",
    permissionDenied: false,
    staleReason: "refresh failed",
  };
}

function MetricsErrorAlert({ errorState }: { errorState: MetricsErrorState }) {
  return (
    <Alert variant="destructive">
      <AlertTriangle aria-hidden="true" className="h-4 w-4" />
      <AlertTitle>{errorState.title}</AlertTitle>
      <AlertDescription>
        {errorState.description} Showing the last successful readings.
      </AlertDescription>
    </Alert>
  );
}

function MetricsToolbar({
  errorState,
  isFetching,
  onRefresh,
  updatedAt,
}: {
  errorState: MetricsErrorState | null;
  isFetching: boolean;
  onRefresh: () => Promise<unknown> | unknown;
  updatedAt: number | null;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <LiveTimestamp
        forceStale={!!errorState}
        reason={errorState?.staleReason}
        staleAfterMs={STALE_AFTER_MS.live}
        updatedAt={updatedAt}
      />
      <div className="flex items-center gap-2">
        {isFetching ? (
          <span aria-hidden="true" className="text-xs text-muted-foreground">
            Refreshing…
          </span>
        ) : null}
        <RefreshButton
          hideTimestamp
          label="Refresh instance metrics"
          onRefresh={onRefresh}
        />
      </div>
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
