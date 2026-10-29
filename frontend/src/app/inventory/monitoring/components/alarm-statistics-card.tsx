"use client";

import { useMemo } from "react";

import { AlarmTile, NoActiveAlarms } from "@/components/dashboard/alarm-tile";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { TableLink } from "@/components/ui/table-link";
import { alarmCounts, offlineClusterWarning } from "@/lib/alarm-counts";
import { inventoryLinks } from "@/lib/inventory-links";
import { warningClusterValue } from "@/lib/warning-facets";
import type { SystemWarningsResponse, WarningInfo } from "@/types/api";

interface AlarmStatisticsCardProps {
  warnings: readonly WarningInfo[];
  system: SystemWarningsResponse | null | undefined;
  /** A cluster id, or "all". */
  selectedNode: string;
  /** False until both warning reads have answered: an unread fleet is not "No active alarms". */
  loaded: boolean;
}

export function AlarmStatisticsCard({ warnings, system, selectedNode, loaded }: AlarmStatisticsCardProps) {
  const node = selectedNode === "all" ? undefined : selectedNode;
  const counts = useMemo(() => alarmCounts(warnings, system, node), [warnings, system, node]);

  // The offline-clusters and kernel tabs facet on the row's own value (display name, else id),
  // so a narrowed tile carries that row's value rather than the picker's id.
  const offlineRow = node ? offlineClusterWarning(system, node) : undefined;
  const kernelRow = node ? system?.kernel_advisories?.find((row) => row.cluster_id === node) : undefined;

  return (
    <Card data-slot="alarm-statistics">
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>
            Alarm Statistics
          </CardTitle>
          <CardDescription>System alerts and warnings overview</CardDescription>
        </div>
        {counts.total > 0 && (
          <TableLink href={inventoryLinks.warnings({ node })} className="text-sm">
            View All ({counts.total})
          </TableLink>
        )}
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <AlarmTile kind="critical" count={counts.critical} href={inventoryLinks.warnings({ severity: "critical", node })} />
          <AlarmTile kind="high" count={counts.high} href={inventoryLinks.warnings({ severity: "high", node })} />
          <AlarmTile kind="warning" count={counts.warning} href={inventoryLinks.warnings({ severity: "warning", node })} />
          <AlarmTile kind="info" count={counts.info} href={inventoryLinks.warnings({ severity: "info", node })} />
          {counts.certIssues > 0 && (
            <AlarmTile kind="certificates" count={counts.certIssues} href={inventoryLinks.certificates({ cluster: node })} />
          )}
          {counts.offlineClusters > 0 && (
            <AlarmTile
              kind="offlineClusters"
              count={counts.offlineClusters}
              href={inventoryLinks.warnings({ tab: "nodes", cluster: offlineRow ? warningClusterValue(offlineRow) : undefined })}
            />
          )}
          {counts.kernels > 0 && (
            <AlarmTile
              kind="kernel"
              count={counts.kernels}
              href={inventoryLinks.warnings({ tab: "kernel", cluster: kernelRow ? warningClusterValue(kernelRow) : undefined })}
            />
          )}
        </div>
        {loaded && counts.total === 0 ? (
          <div className="mt-4">
            <NoActiveAlarms />
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
