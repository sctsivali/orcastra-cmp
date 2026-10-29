"use client";

import { AlertTriangle, ChevronRight, Flame, Info, Shield, ShieldAlert, WifiOff } from "lucide-react";

// note: the tiles point at /inventory/warnings with distinct query strings, which are distinct
// router cache keys. They warm on intent rather than on sight.
import { IntentLink as Link } from "@/components/navigation/route-intent";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { inventoryLinks } from "@/lib/inventory-links";
import { warningClusterValue } from "@/lib/warning-facets";
import type { CertificateWarning } from "@/types/api";

export interface AlarmCounts {
  critical: number;
  high: number;
  warning: number;
  info: number;
  total: number;
  certIssues: number;
  inactiveNodes: number;
}

interface AlarmStatisticsCardProps {
  counts: AlarmCounts;
  /** A cluster id, or "all". */
  selectedNode: string;
  /** The offline-cluster row for the selected cluster, whose own value the nodes tab facets on. */
  selectedNodeWarning?: CertificateWarning;
}

export function AlarmStatisticsCard({ counts, selectedNode, selectedNodeWarning }: AlarmStatisticsCardProps) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>
            Alarm Statistics
          </CardTitle>
          <CardDescription>System alerts and warnings overview</CardDescription>
        </div>
        {counts.total > 0 && (
          <Link 
            href={inventoryLinks.warnings({ node: selectedNode === "all" ? undefined : selectedNode })}
            className="text-sm text-primary hover:underline flex items-center gap-1"
          >
            View All ({counts.total})
            <ChevronRight className="h-4 w-4" />
          </Link>
        )}
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Link 
            href={inventoryLinks.warnings({ severity: "critical", node: selectedNode === "all" ? undefined : selectedNode })}
            className="flex items-center gap-3 p-4 rounded-lg border bg-red-500/5 border-red-500/20 hover:bg-red-500/10 hover:border-red-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-red-500/10 group-hover:bg-red-500/20 transition-colors">
              <Flame className="h-5 w-5 text-red-500" />
            </div>
            <div>
              <p className="text-2xl font-bold text-red-600">{counts.critical}</p>
              <p className="text-xs text-muted-foreground">Critical</p>
            </div>
          </Link>
          <Link 
            href={inventoryLinks.warnings({ severity: "high", node: selectedNode === "all" ? undefined : selectedNode })}
            className="flex items-center gap-3 p-4 rounded-lg border bg-orange-500/5 border-orange-500/20 hover:bg-orange-500/10 hover:border-orange-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-orange-500/10 group-hover:bg-orange-500/20 transition-colors">
              <ShieldAlert className="h-5 w-5 text-orange-500" />
            </div>
            <div>
              <p className="text-2xl font-bold text-orange-600">{counts.high}</p>
              <p className="text-xs text-muted-foreground">Major</p>
            </div>
          </Link>
          <Link 
            href={inventoryLinks.warnings({ severity: "warning", node: selectedNode === "all" ? undefined : selectedNode })}
            className="flex items-center gap-3 p-4 rounded-lg border bg-yellow-500/5 border-yellow-500/20 hover:bg-yellow-500/10 hover:border-yellow-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-yellow-500/10 group-hover:bg-yellow-500/20 transition-colors">
              <AlertTriangle className="h-5 w-5 text-yellow-500" />
            </div>
            <div>
              <p className="text-2xl font-bold text-yellow-600">{counts.warning}</p>
              <p className="text-xs text-muted-foreground">Warning</p>
            </div>
          </Link>
          <Link 
            href={inventoryLinks.warnings({ severity: "info", node: selectedNode === "all" ? undefined : selectedNode })}
            className="flex items-center gap-3 p-4 rounded-lg border bg-blue-500/5 border-blue-500/20 hover:bg-blue-500/10 hover:border-blue-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-blue-500/10 group-hover:bg-blue-500/20 transition-colors">
              <Info className="h-5 w-5 text-blue-500" />
            </div>
            <div>
              <p className="text-2xl font-bold text-blue-600">{counts.info}</p>
              <p className="text-xs text-muted-foreground">Info</p>
            </div>
          </Link>
        </div>
      
        {/* Certificate Issues & Inactive Nodes */}
        {(counts.certIssues > 0 || counts.inactiveNodes > 0) && (
          <div className="grid grid-cols-2 gap-4 mt-4">
            {counts.certIssues > 0 && (
              <Link 
                href="/inventory/clusters/certificates"
                className="flex items-center gap-3 p-4 rounded-lg border border-primary/20 bg-primary/5 transition-all hover:border-primary/40 hover:bg-primary/10 cursor-pointer group"
              >
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 transition-colors group-hover:bg-primary/20">
                  <Shield className="h-5 w-5 text-primary" />
                </div>
                <div>
                  <p className="text-2xl font-bold text-foreground">{counts.certIssues}</p>
                  <p className="text-xs text-muted-foreground">Cert Issues</p>
                </div>
              </Link>
            )}
            {counts.inactiveNodes > 0 && (
              <Link 
                href={inventoryLinks.warnings({ tab: "nodes", cluster: selectedNodeWarning ? warningClusterValue(selectedNodeWarning) : undefined })}
                className="flex items-center gap-3 p-4 rounded-lg border bg-rose-500/5 border-rose-500/20 hover:bg-rose-500/10 hover:border-rose-500/40 transition-all cursor-pointer group"
              >
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-rose-500/10 group-hover:bg-rose-500/20 transition-colors">
                  <WifiOff className="h-5 w-5 text-rose-500" />
                </div>
                <div>
                  <p className="text-2xl font-bold text-rose-600">{counts.inactiveNodes}</p>
                  <p className="text-xs text-muted-foreground">Inactive Clusters</p>
                </div>
              </Link>
            )}
          </div>
        )}
      
        {counts.total === 0 && (
          <div className="mt-4 text-center py-4 bg-green-500/5 rounded-lg border border-green-500/20">
            <p className="text-green-600 font-medium">✓ All systems operational - No active alarms</p>
          </div>
        )}
        {counts.total > 0 && (
          <div className="mt-4 text-xs text-muted-foreground">
            <span>Click any category to view details</span>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
