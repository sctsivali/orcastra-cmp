"use client";

import * as React from "react";
import { SlidersHorizontal } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { InstanceFileExplorer } from "@/components/instance-files/instance-file-explorer";
import { useInstance, useInstanceMetrics } from "@/hooks/use-lxd";
import { useFilesUnavailableReason } from "@/hooks/use-instance-files";
import { InstanceOverviewTab } from "./instance-overview-tab";
import { InstanceMetricsTab } from "./instance-metrics-tab";
import { InstanceConfigTab } from "./instance-config-tab";
import { InstanceDevicesTab } from "./instance-devices-tab";
import { InstanceSnapshotsTab } from "./instance-snapshots-tab";
import { instanceStatusDisplay } from "@/lib/instance-status";
import type { VMInfo } from "@/types/api";

export type InstanceDetailTab = "overview" | "metrics" | "config" | "devices" | "snapshots" | "files";

export interface InstanceDetailDialogProps {
  instance: VMInfo | null;
  isOpen: boolean;
  onClose: () => void;
  project?: string;
  /** Fetch the full instance before showing details when the caller only has a monitoring summary. */
  hydrateDetail?: boolean;
  /** Lets a cell open straight to the panel it is about, ex; the root-disk cell to Devices. */
  defaultTab?: InstanceDetailTab;
  /** Partner+ action. Authorization is still enforced by the backend. */
  canEditSpecification?: boolean;
  editSpecificationDisabledReason?: string;
  onEditSpecification?: () => void;
}

// Everything about one instance: overview, live metrics, config, devices, snapshots and files.
export function InstanceDetailDialog({
  instance,
  isOpen,
  onClose,
  project = "default",
  hydrateDetail = false,
  defaultTab = "overview",
  canEditSpecification = false,
  editSpecificationDisabledReason,
  onEditSpecification,
}: InstanceDetailDialogProps) {
  const [tab, setTab] = React.useState<string>(defaultTab);
  // Set during render rather than in an effect, so the first frame of a new opening already
  // knows that an older cached reading must not decide the runtime state.
  const [opening, setOpening] = React.useState(() => ({ open: isOpen, at: Date.now() }));
  if (opening.open !== isOpen) setOpening({ open: isOpen, at: Date.now() });
  const openedAt = opening.at;
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

  const record: VMInfo | null = instance
    ? hydrateDetail
      ? fetched ?? instance
      : instance
    : null;
  const effectiveProject = record?.project || project || "default";
  const filesUnavailableReason = useFilesUnavailableReason(record?.host ?? "", effectiveProject, record?.name ?? "");

  // important: gated on the tab and the page, never on the row's status. The row is the one
  // captured at click time, so a status gate would freeze the panel on "not running" for an
  // instance started while the dialog is open. The metrics response carries the runtime state
  // itself, and the polling policy keeps a slow check going while the instance is not running.
  const {
    data: metrics,
    dataUpdatedAt: metricsUpdatedAt,
    error: metricsError,
    isError: isMetricsError,
    isFetching: isFetchingMetrics,
    isLoading: isLoadingMetrics,
    refetch: refetchMetrics,
  } = useInstanceMetrics(
    record?.host ?? "",
    record?.name ?? "",
    effectiveProject,
    isOpen && !!record && tab === "metrics" && pageVisible,
  );

  const refreshMetrics = React.useCallback(
    () => refetchMetrics({ throwOnError: true }),
    [refetchMetrics],
  );

  // A start or stop the list saw re-checks at once rather than at the next slow poll.
  const rowStatus = record?.status;
  const previousRowStatus = React.useRef(rowStatus);
  React.useEffect(() => {
    if (previousRowStatus.current === rowStatus) return;
    previousRowStatus.current = rowStatus;
    if (isOpen && tab === "metrics") void refetchMetrics({ cancelRefetch: false });
  }, [rowStatus, isOpen, tab, refetchMetrics]);

  // A metrics reading taken since this dialog opened is newer than the row it was opened from,
  // so its runtime state wins; a cached reading from an earlier opening never does.
  const liveStatus =
    metrics && metricsUpdatedAt >= openedAt && metrics.status !== record?.status
      ? metrics.status
      : null;
  const detail: VMInfo | null = record && liveStatus ? { ...record, status: liveStatus } : record;

  if (!detail) return null;

  // The browser's own receive time, so the age label never compares a server clock with a
  // browser clock that disagrees with it.
  const updatedAt = metricsUpdatedAt || null;
  const statusDisplay = instanceStatusDisplay(detail.status);
  const canSeeFiles = !filesUnavailableReason;

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
      footer={
        canEditSpecification && onEditSpecification ? (
          <div className="flex w-full flex-wrap items-center justify-end gap-2">
            {editSpecificationDisabledReason && (
              <span className="text-xs text-muted-foreground sm:mr-auto">
                {editSpecificationDisabledReason}
              </span>
            )}
            <Button
              size="sm"
              onClick={onEditSpecification}
              disabled={!!editSpecificationDisabledReason}
            >
              <SlidersHorizontal className="mr-2 h-4 w-4" aria-hidden="true" />
              Edit specifications
            </Button>
          </div>
        ) : undefined
      }
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
        ...(canSeeFiles
          ? [
              {
                value: "files",
                label: "Files",
                content: (
                  <InstanceFileExplorer
                    host={detail.host}
                    project={detail.project || "default"}
                    instanceName={detail.name}
                    active={isOpen && tab === "files"}
                    embedded
                  />
                ),
              },
            ]
          : []),
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
