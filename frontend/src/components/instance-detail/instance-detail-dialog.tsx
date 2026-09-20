"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { useInstance, useInstanceMetrics } from "@/hooks/use-lxd";
import { InstanceOverviewTab } from "./instance-overview-tab";
import { InstanceMetricsTab } from "./instance-metrics-tab";
import { InstanceConfigTab } from "./instance-config-tab";
import { InstanceDevicesTab } from "./instance-devices-tab";
import { InstanceSnapshotsTab } from "./instance-snapshots-tab";
import { instanceStatusDisplay } from "@/lib/instance-status";
import type { VMInfo } from "@/types/api";

export type InstanceDetailTab = "overview" | "metrics" | "config" | "devices" | "snapshots";

export interface InstanceDetailDialogProps {
  instance: VMInfo | null;
  isOpen: boolean;
  onClose: () => void;
  project?: string;
  /** Fetch the full instance before showing details when the caller only has a monitoring summary. */
  hydrateDetail?: boolean;
  /** Lets a cell open straight to the panel it is about, ex; the root-disk cell to Devices. */
  defaultTab?: InstanceDetailTab;
}

// Replaces instance-monitoring-modal, which showed live metrics and nothing else while the row
// it was opened from already carried config, devices, expanded_devices, profiles, tags,
// description, architecture and both timestamps.
export function InstanceDetailDialog({
  instance,
  isOpen,
  onClose,
  project = "default",
  hydrateDetail = false,
  defaultTab = "overview",
}: InstanceDetailDialogProps) {
  const [tab, setTab] = React.useState<string>(defaultTab);
  const pageVisible = useDocumentVisible();

  React.useEffect(() => {
    if (isOpen) setTab(defaultTab);
  }, [isOpen, defaultTab]);

  // Only Monitoring Top Consumers passes hydrateDetail. Its chart summary intentionally omits
  // configuration, devices, timestamps and architecture. Normal list rows already carry the
  // complete dialog payload and must not issue a duplicate detail request on every opening.
  const {
    data: fetched,
    isLoading: isLoadingDetail,
    isError: isDetailError,
  } = useInstance(
    instance?.name ?? "",
    instance?.host ?? "",
    isOpen && !!instance && hydrateDetail,
    instance?.project ?? undefined,
  );

  const detail: VMInfo | null = instance
    ? hydrateDetail
      ? fetched ?? instance
      : instance
    : null;
  const effectiveProject = detail?.project || project || "default";

  // important: gated on the tab, not just on the dialog. The modal this replaces started a
  // 10-second poll the instant it opened, and kept it running while the operator read something
  // else entirely.
  const {
    data: metrics,
    dataUpdatedAt: metricsUpdatedAt,
    error: metricsError,
    isError: isMetricsError,
    isFetching: isFetchingMetrics,
    isLoading: isLoadingMetrics,
    refetch: refetchMetrics,
  } = useInstanceMetrics(
    detail?.host ?? "",
    detail?.name ?? "",
    effectiveProject,
    isOpen && !!detail && tab === "metrics" && pageVisible && detail.status === "Running",
  );

  const refreshMetrics = React.useCallback(
    () => refetchMetrics({ throwOnError: true }),
    [refetchMetrics],
  );

  if (!detail) return null;

  const serverTimestamp = metrics?.timestamp ? Date.parse(metrics.timestamp) : Number.NaN;
  const updatedAt = Number.isFinite(serverTimestamp) ? serverTimestamp : metricsUpdatedAt || null;
  const statusDisplay = instanceStatusDisplay(detail.status);

  return (
    <RecordDetailDialog
      open={isOpen}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title={detail.name}
      recordId={`${detail.host}/${detail.project || "default"}/${detail.name}`}
      recordIdLabel="Instance"
      subtitle={detail.host_endpoint ?? detail.host}
      error={
        hydrateDetail && isDetailError
          ? "Some instance details could not be loaded. The summary and live metrics remain available."
          : null
      }
      status={
        <Badge variant={statusDisplay.tone}>
          {statusDisplay.label}
        </Badge>
      }
      tab={tab}
      onTabChange={setTab}
      defaultTab={defaultTab}
      isLoading={hydrateDetail && isLoadingDetail && !fetched}
      size="2xl"
      tabs={[
        {
          value: "overview",
          label: "Overview",
          content: <InstanceOverviewTab instance={detail} />,
        },
        {
          value: "metrics",
          label: "Metrics",
          content: (
            <InstanceMetricsTab
              instance={detail}
              metrics={metrics}
              error={metricsError}
              isError={isMetricsError}
              isFetching={isFetchingMetrics}
              isLoading={isLoadingMetrics}
              onRefresh={refreshMetrics}
              updatedAt={updatedAt}
            />
          ),
        },
        { value: "config", label: "Configuration", content: <InstanceConfigTab instance={detail} /> },
        { value: "devices", label: "Devices", content: <InstanceDevicesTab instance={detail} /> },
        {
          value: "snapshots",
          label: "Snapshots",
          // Gated on the tab for the same reason metrics is: an idle panel must not keep
          // asking the cluster about an instance the operator is not looking at.
          content: (
            <InstanceSnapshotsTab
              instance={detail}
              enabled={isOpen && tab === "snapshots"}
            />
          ),
        },
      ]}
    />
  );
}

function useDocumentVisible(): boolean {
  const [visible, setVisible] = React.useState(
    () => typeof document === "undefined" || document.visibilityState === "visible",
  );

  React.useEffect(() => {
    const handleVisibilityChange = () => setVisible(document.visibilityState === "visible");
    handleVisibilityChange();
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, []);

  return visible;
}
