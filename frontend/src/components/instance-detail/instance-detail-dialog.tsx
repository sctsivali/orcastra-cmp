"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { useInstance, useInstanceMetrics } from "@/hooks/use-lxd";
import { InstanceOverviewTab } from "./instance-overview-tab";
import { InstanceMetricsTab } from "./instance-metrics-tab";
import { InstanceConfigTab } from "./instance-config-tab";
import { InstanceDevicesTab } from "./instance-devices-tab";
import type { VMInfo } from "@/types/api";

export type InstanceDetailTab = "overview" | "metrics" | "config" | "devices";

export interface InstanceDetailDialogProps {
  instance: VMInfo | null;
  isOpen: boolean;
  onClose: () => void;
  project?: string;
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
  defaultTab = "overview",
}: InstanceDetailDialogProps) {
  const [tab, setTab] = React.useState<string>(defaultTab);

  React.useEffect(() => {
    if (isOpen) setTab(defaultTab);
  }, [isOpen, defaultTab]);

  // important: the LIST payload sets resources to null, so this is the only way to show them.
  // Everything else below is already on the row, which is why the dialog paints immediately and
  // only this one section can be pending.
  const { data: fetched } = useInstance(
    instance?.name ?? "",
    instance?.host ?? "",
    isOpen && !!instance,
  );

  // important: gated on the tab, not just on the dialog. The modal this replaces started a
  // 10-second poll the instant it opened, and kept it running while the operator read something
  // else entirely.
  const { data: metrics, isLoading: isLoadingMetrics } = useInstanceMetrics(
    instance?.host ?? "",
    instance?.name ?? "",
    project,
    isOpen && !!instance && tab === "metrics",
  );

  if (!instance) return null;

  // note: never blank the dialog while the detail fetch is in flight. The row is a complete
  // VMInfo minus `resources`, so the header and every tab but that one section paint at once.
  const detail: VMInfo = fetched ?? instance;

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
      status={
        <Badge variant={detail.status === "Running" ? "default" : "secondary"}>
          {detail.status}
        </Badge>
      }
      tab={tab}
      onTabChange={setTab}
      defaultTab={defaultTab}
      size="2xl"
      tabs={[
        { value: "overview", label: "Overview", content: <InstanceOverviewTab instance={detail} /> },
        {
          value: "metrics",
          label: "Metrics",
          content: (
            <InstanceMetricsTab
              instance={detail}
              metrics={metrics}
              isLoading={isLoadingMetrics}
            />
          ),
        },
        { value: "config", label: "Configuration", content: <InstanceConfigTab instance={detail} /> },
        { value: "devices", label: "Devices", content: <InstanceDevicesTab instance={detail} /> },
      ]}
      // note: no whole-dialog skeleton. The row is a complete VMInfo minus `resources`, so
      // everything paints at once and only the resources block in Overview can be pending.
      // `isLoadingDetail` is deliberately unused here rather than passed as a condition that
      // can never be true.
    />
  );
}
