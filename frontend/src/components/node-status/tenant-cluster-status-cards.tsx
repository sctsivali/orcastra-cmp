"use client";

import { Server, WifiOff } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { OverflowText } from "@/components/ui/overflow-text";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Skeleton } from "@/components/ui/skeleton";
import { useNodeAvailability } from "@/hooks/use-lxd";
import { clusterStatusMeta } from "@/lib/cluster-status";

function AvailabilitySkeleton() {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      {[0, 1].map((key) => (
        <Card key={key}>
          <CardContent className="flex items-center justify-between gap-4 p-4">
            <div className="space-y-2">
              <Skeleton className="h-4 w-28" />
              <Skeleton className="h-3 w-20" />
            </div>
            <Skeleton className="h-5 w-20 rounded" />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

/**
 * Tenant-safe cluster availability for Overview.
 *
 * This surface deliberately never mounts the privileged /nodes inventory query and offers
 * no hardware dialog or node controls. Tenants only need to know where their workloads live
 * and whether that cluster can currently answer.
 */
export function TenantClusterStatusCards() {
  const { data, isLoading, isError, dataUpdatedAt, refreshNow } = useNodeAvailability();

  return (
    <section className="space-y-4" aria-labelledby="tenant-clusters-heading">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 id="tenant-clusters-heading" className="text-lg font-semibold">
            Clusters
          </h2>
          <p className="text-sm text-muted-foreground">
            {data
              ? `${data.online_count} of ${data.total_count} available`
              : "Availability for the clusters that host your instances"}
          </p>
        </div>
        <RefreshButton
          label="Refresh cluster availability"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      {isLoading ? (
        <AvailabilitySkeleton />
      ) : isError ? (
        <EmptyState
          icon={WifiOff}
          title="Could not load cluster availability"
          description="Refresh to try the availability check again."
        />
      ) : !data || data.clusters.length === 0 ? (
        <EmptyState
          icon={Server}
          title="No clusters available"
          description="Clusters appear here when your organization has active workload access."
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {data.clusters.map((cluster) => {
            const meta = clusterStatusMeta(cluster.status);
            const StatusIcon = meta.icon;
            const label = cluster.available ? "Online" : "Unavailable";

            return (
              <Card key={cluster.name}>
                <CardContent className="flex min-w-0 items-center justify-between gap-4 p-4">
                  <div className="flex min-w-0 items-center gap-3">
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-muted">
                      <Server className="h-4 w-4" aria-hidden="true" />
                    </div>
                    <div className="min-w-0">
                      <p>
                        <OverflowText
                          className="font-medium"
                          label={`cluster name for ${cluster.name}`}
                          value={cluster.display_name || cluster.name}
                        />
                      </p>
                      {cluster.display_name && (
                        <p>
                          <OverflowText
                            className="text-xs text-muted-foreground"
                            label={`cluster id for ${cluster.display_name}`}
                            mono
                            value={cluster.name}
                          />
                        </p>
                      )}
                    </div>
                  </div>
                  <Badge variant={cluster.available ? "positive" : "negative"} className="shrink-0 gap-1">
                    <StatusIcon className="h-3 w-3" aria-hidden="true" />
                    {label}
                  </Badge>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </section>
  );
}
