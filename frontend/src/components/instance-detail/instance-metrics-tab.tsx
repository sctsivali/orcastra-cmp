"use client";
import { Activity, AlertTriangle, LockKeyhole } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { RefreshButton } from "@/components/ui/refresh-button";
import { LoadingLottieMedium } from "@/components/loading-lottie";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { Badge } from "@/components/ui/badge";
import type { InstanceMetricSection, InstanceMetrics, VMInfo } from "@/types/api";
import { usePermissions } from "@/hooks/use-permissions";
import { GuestAgentNotice } from "./metrics/guest-agent-notice";
import { CpuCard, DiskCard, MemoryCard, NetworkCard } from "./metrics/metric-cards";
import {
  METRIC_SECTION_LABELS,
  describeMetricsError,
  formatSectionList,
  formatUptime,
  notRunningCopy,
} from "./metrics/metrics-copy";
import { MetricsErrorAlert, MetricsToolbar } from "./metrics/metrics-toolbar";

// Sections a missing guest agent already explains in its own notice.
const AGENT_SECTIONS: readonly InstanceMetricSection[] = ["cpu", "memory", "processes"];

// The live-metrics half of the instance dialog. The query keeps the last successful sample while
// a refresh is failing, so this panel must present freshness and failure separately rather than
// replacing useful readings with a generic error screen.
//
// note: `instance.status` is already the live runtime state. The dialog prefers a metrics reading
// taken since it opened over the row it was opened from, so this panel never has to reconcile
// two statuses itself.
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
  // note: before any early return (rules of hooks). The host PID is Partner and Admin detail; a
  // Tenant's metrics arrive without it.
  const { isPartner } = usePermissions();
  const errorState = isError ? describeMetricsError(error) : null;

  if (instance.status !== "Running") {
    const copy = notRunningCopy(instance.status);
    return (
      <EmptyState
        icon={Activity}
        title={copy.title}
        description={copy.description}
        action={
          <RefreshButton hideTimestamp label="Check instance metrics again" onRefresh={onRefresh} />
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

  const unavailableSections = new Set(metrics.unavailable_sections);
  const allSectionsUnavailable = (
    Object.keys(METRIC_SECTION_LABELS) as InstanceMetricSection[]
  ).every((section) => unavailableSections.has(section));
  const agentAway = metrics.guest_agent === "unreachable" || metrics.guest_agent === "disabled";
  const unexplainedSections = metrics.unavailable_sections.filter(
    (section) => !(agentAway && AGENT_SECTIONS.includes(section)),
  );
  const isPartialRead = unexplainedSections.length > 0 && !allSectionsUnavailable;
  const processesUnavailable = unavailableSections.has("processes");

  const toolbar = (
    <>
      <MetricsToolbar
        errorState={errorState}
        isFetching={isFetching}
        onRefresh={onRefresh}
        updatedAt={updatedAt}
      />
      {errorState ? <MetricsErrorAlert errorState={errorState} /> : null}
    </>
  );

  if (allSectionsUnavailable) {
    return (
      <div className="space-y-4">
        {toolbar}
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
      {toolbar}

      <GuestAgentNotice status={metrics.guest_agent} />

      {isPartialRead ? (
        <PartialReadNotice>
          The cluster did not report {formatSectionList(unexplainedSections)} in this sample.
        </PartialReadNotice>
      ) : null}

      <DetailSection title="Runtime">
        <DetailGrid cols={4}>
          <DetailField
            label="Processes"
            value={processesUnavailable ? undefined : metrics.processes}
          />
          <DetailField label="Uptime" value={formatUptime(metrics.uptime_seconds)} />
          {isPartner ? <DetailField label="PID" value={metrics.pid} /> : null}
          <DetailField label="Base Image" value={metrics.base_image} mono span="full" />
        </DetailGrid>
      </DetailSection>

      <CpuCard metrics={metrics} />
      <MemoryCard metrics={metrics} />
      <DiskCard metrics={metrics} />
      <NetworkCard metrics={metrics} />

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
