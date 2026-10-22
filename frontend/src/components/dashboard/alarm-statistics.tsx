"use client";

import * as React from "react";
// note: these alert cards all point at /inventory/warnings with distinct query strings,
// which are distinct router cache keys. Prefetching them on sight cost seven RSC requests
// on every dashboard paint, so they warm on intent like the sidebar does.
import { IntentLink as Link } from "@/components/navigation/route-intent";
import {
  AlertTriangle,
  Info,
  Flame,
  ShieldAlert,
  ShieldX,
  Shield,
  WifiOff,
  ChevronRight } from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle } from "@/components/ui/card";
import { useWarnings, useSystemWarnings } from "@/hooks/use-lxd";
import { usePermissions } from "@/hooks/use-permissions";
import { inventoryLinks } from "@/lib/inventory-links";
import { severityBucket } from "@/lib/warning-facets";

export function AlarmStatistics({ prefetchedWarnings, prefetchedSystemWarnings }: { prefetchedWarnings?: any; prefetchedSystemWarnings?: any } = {}) {
  // use prefetched data if available, otherwise fall back to own hooks (standalone usage)
  const { data: warningsDataFetched } = useWarnings();
  const { data: systemWarningsFetched } = useSystemWarnings();
  const { isPartner } = usePermissions();
  
  const warningsData = prefetchedWarnings ?? warningsDataFetched;
  const systemWarnings = prefetchedSystemWarnings ?? systemWarningsFetched;
  
  const actualWarnings = (warningsData as any)?.warnings || [];
  const certWarnings = systemWarnings?.certificate_warnings || [];
  const nodeWarnings = systemWarnings?.node_warnings || [];
  const kernelWarnings = systemWarnings?.kernel_advisories || [];
  
  // Count warnings by severity
  const counts = React.useMemo(() => {
    const result = { critical: 0, high: 0, warning: 0, info: 0, total: 0, certIssues: 0, inactiveNodes: 0, kernels: 0 };
    
    actualWarnings.forEach((w: any) => {
      result.total++;
      // The same buckets the tile links open, so a tile's number is the count the list shows.
      const bucket = severityBucket(w.severity);
      if (bucket) result[bucket]++;
    });
    
    certWarnings.forEach((w: any) => {
      result.total++;
      result.certIssues++;
    });
    
    nodeWarnings.forEach(() => {
      result.total++;
      result.inactiveNodes++;
    });

    // note: its own tile, not folded into Critical or Warning. Those tiles open the platform
    // tab, which has no kernel rows, and a tile's number must be the count its list shows.
    kernelWarnings.forEach(() => {
      result.total++;
      result.kernels++;
    });

    return result;
  }, [actualWarnings, certWarnings, nodeWarnings, kernelWarnings]);

  // Don't render if no warnings
  if (counts.total === 0) {
    return null;
  }

  // important: this card is platform health, and every tile in it links to a page
  // useRequireRole(Role.PARTNER) bounces a tenant away from. Certificate expiry and inactive
  // nodes are not a tenant's concern, and offering them as navigation is a dead end.
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
        <Link 
          href="/inventory/warnings" 
          className="text-sm text-primary hover:underline flex items-center gap-1"
        >
          View All ({counts.total})
          <ChevronRight className="h-4 w-4" />
        </Link>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          {/* Critical */}
          <Link 
            href={inventoryLinks.warnings({ severity: "critical" })}
            className="flex items-center gap-3 p-3 rounded-lg border bg-red-500/5 border-red-500/20 hover:bg-red-500/10 hover:border-red-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-red-500/10 group-hover:bg-red-500/20 transition-colors">
              <Flame className="h-4 w-4 text-red-500" />
            </div>
            <div>
              <p className="text-xl font-bold text-red-600">{counts.critical}</p>
              <p className="text-xs text-muted-foreground">Critical</p>
            </div>
          </Link>

          {/* Major/High */}
          <Link 
            href={inventoryLinks.warnings({ severity: "high" })}
            className="flex items-center gap-3 p-3 rounded-lg border bg-orange-500/5 border-orange-500/20 hover:bg-orange-500/10 hover:border-orange-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-orange-500/10 group-hover:bg-orange-500/20 transition-colors">
              <ShieldAlert className="h-4 w-4 text-orange-500" />
            </div>
            <div>
              <p className="text-xl font-bold text-orange-600">{counts.high}</p>
              <p className="text-xs text-muted-foreground">Major</p>
            </div>
          </Link>

          {/* Warning */}
          <Link 
            href={inventoryLinks.warnings({ severity: "warning" })}
            className="flex items-center gap-3 p-3 rounded-lg border bg-yellow-500/5 border-yellow-500/20 hover:bg-yellow-500/10 hover:border-yellow-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-yellow-500/10 group-hover:bg-yellow-500/20 transition-colors">
              <AlertTriangle className="h-4 w-4 text-yellow-500" />
            </div>
            <div>
              <p className="text-xl font-bold text-yellow-600">{counts.warning}</p>
              <p className="text-xs text-muted-foreground">Warning</p>
            </div>
          </Link>

          {/* Info */}
          <Link 
            href={inventoryLinks.warnings({ severity: "info" })}
            className="flex items-center gap-3 p-3 rounded-lg border bg-blue-500/5 border-blue-500/20 hover:bg-blue-500/10 hover:border-blue-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-500/10 group-hover:bg-blue-500/20 transition-colors">
              <Info className="h-4 w-4 text-blue-500" />
            </div>
            <div>
              <p className="text-xl font-bold text-blue-600">{counts.info}</p>
              <p className="text-xs text-muted-foreground">Info</p>
            </div>
          </Link>

          {/* Cert Issues */}
          {counts.certIssues > 0 && (
            <Link 
              href="/inventory/clusters/certificates"
              className="flex items-center gap-3 p-3 rounded-lg border bg-purple-500/5 border-purple-500/20 hover:bg-purple-500/10 hover:border-purple-500/40 transition-all cursor-pointer group"
            >
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-purple-500/10 group-hover:bg-purple-500/20 transition-colors">
                <Shield className="h-4 w-4 text-purple-500" />
              </div>
              <div>
                <p className="text-xl font-bold text-purple-600">{counts.certIssues}</p>
                <p className="text-xs text-muted-foreground">Cert Issues</p>
              </div>
            </Link>
          )}

          {/* Inactive Nodes */}
          {counts.inactiveNodes > 0 && (
            <Link 
              href={inventoryLinks.warnings({ tab: "nodes" })}
              className="flex items-center gap-3 p-3 rounded-lg border bg-rose-500/5 border-rose-500/20 hover:bg-rose-500/10 hover:border-rose-500/40 transition-all cursor-pointer group"
            >
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-rose-500/10 group-hover:bg-rose-500/20 transition-colors">
                <WifiOff className="h-4 w-4 text-rose-500" />
              </div>
              <div>
                <p className="text-xl font-bold text-rose-600">{counts.inactiveNodes}</p>
                <p className="text-xs text-muted-foreground">Inactive</p>
              </div>
            </Link>
          )}

          {/* Kernel CVE findings: one per member and CVE that is affected or not confirmed patched */}
          {counts.kernels > 0 && (
            <Link
              href={inventoryLinks.warnings({ tab: "kernel" })}
              className="flex items-center gap-3 p-3 rounded-lg border border-status-offline/40 hover:bg-muted/50 transition-colors cursor-pointer"
            >
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-muted">
                <ShieldX className="h-4 w-4 text-status-offline" aria-hidden="true" />
              </div>
              <div>
                <p className="text-xl font-bold text-status-offline">{counts.kernels}</p>
                <p className="text-xs text-muted-foreground">Kernel CVEs</p>
              </div>
            </Link>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
