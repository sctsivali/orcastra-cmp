"use client";

import * as React from "react";
import { AlarmTile } from "@/components/dashboard/alarm-tile";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle } from "@/components/ui/card";
import { TableLink } from "@/components/ui/table-link";
import { useWarnings, useSystemWarnings } from "@/hooks/use-lxd";
import { usePermissions } from "@/hooks/use-permissions";
import { alarmCounts, platformWarnings } from "@/lib/alarm-counts";
import { inventoryLinks } from "@/lib/inventory-links";
import type { SystemWarningsResponse } from "@/types/api";

export function AlarmStatistics({
  prefetchedWarnings,
  prefetchedSystemWarnings,
}: {
  /** A GET /warnings body, which the client reads untyped. */
  prefetchedWarnings?: unknown;
  prefetchedSystemWarnings?: SystemWarningsResponse | null;
} = {}) {
  // Prefetched data when the dashboard passes it; the card's own reads otherwise.
  const { data: warningsDataFetched } = useWarnings();
  const { data: systemWarningsFetched } = useSystemWarnings();
  const { isPartner } = usePermissions();

  const warningsData = prefetchedWarnings ?? warningsDataFetched;
  const systemWarnings = prefetchedSystemWarnings ?? systemWarningsFetched;

  // The same counting as the Monitoring page's Alarm Statistics (lib/alarm-counts.ts), so one
  // fleet shows one View All total on both.
  const counts = React.useMemo(
    () => alarmCounts(platformWarnings(warningsData), systemWarnings),
    [warningsData, systemWarnings],
  );

  if (counts.total === 0) {
    return null;
  }

  // Platform health only: every tile links to a page useRequireRole(Role.PARTNER) sends a
  // tenant away from, and certificate expiry and inactive nodes are not a tenant's concern.
  if (!isPartner) {
    return null;
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <div>
          <CardTitle className="text-lg">System Alerts</CardTitle>
          <p className="text-sm text-muted-foreground">Active warnings requiring attention</p>
        </div>
        <TableLink href={inventoryLinks.warnings()} className="text-sm">
          View All ({counts.total})
        </TableLink>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          <AlarmTile kind="critical" count={counts.critical} href={inventoryLinks.warnings({ severity: "critical" })} />
          <AlarmTile kind="high" count={counts.high} href={inventoryLinks.warnings({ severity: "high" })} />
          <AlarmTile kind="warning" count={counts.warning} href={inventoryLinks.warnings({ severity: "warning" })} />
          <AlarmTile kind="info" count={counts.info} href={inventoryLinks.warnings({ severity: "info" })} />
          {counts.certIssues > 0 && (
            <AlarmTile kind="certificates" count={counts.certIssues} href={inventoryLinks.certificates({})} />
          )}
          {counts.offlineClusters > 0 && (
            <AlarmTile kind="offlineClusters" count={counts.offlineClusters} href={inventoryLinks.warnings({ tab: "nodes" })} />
          )}
          {/* One per member and CVE that is affected or not confirmed patched. */}
          {counts.kernels > 0 && (
            <AlarmTile kind="kernel" count={counts.kernels} href={inventoryLinks.warnings({ tab: "kernel" })} />
          )}
        </div>
      </CardContent>
    </Card>
  );
}
