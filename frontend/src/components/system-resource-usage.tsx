"use client";

import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { api } from "@/lib/api";
import type { NetworkInterfaceInfo, SystemResourceUsage } from "@/types";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Skeleton } from "@/components/ui/skeleton";
import { HostSection } from "@/components/system-resources/host-section";
import { ContainerSection } from "@/components/system-resources/container-section";

/** The sample before the current one, kept for the network rates. */
interface PreviousSample {
  network: NetworkInterfaceInfo[];
  timestamp: string;
}

/**
 * Settings > System Info. Two scopes, each labelled for what it is: the host the backend runs
 * on (what its kernel reports, which on an LXD virtual machine is the VM) and the backend's own
 * container (its cgroup limits and usage, what docker stats prints). The panel used to show one
 * blend of both under a single heading, so a 4 GB container limit read as the machine's memory
 * while the load average was the machine's.
 */
export function SystemResourceUsagePanel() {
  const [previous, setPrevious] = useState<PreviousSample | null>(null);
  const [current, setCurrent] = useState<SystemResourceUsage | null>(null);

  const { data, isLoading, isError, error, refreshNow, dataUpdatedAt } = useRefreshableQuery({
    queryKey: ["systemResourceUsage"],
    queryFn: () => api.system.getResourceUsage(),
    refetchInterval: 5000,
    staleTime: 3000,
    gcTime: 60000,
  });

  // Rates come from two consecutive samples and their own timestamps, so a manual refresh a
  // second after a poll still yields a true per-second figure.
  useEffect(() => {
    if (!data) return;
    setCurrent((prev) => {
      if (prev && prev.timestamp !== data.timestamp) {
        setPrevious({ network: prev.network, timestamp: prev.timestamp });
      }
      return data;
    });
  }, [data]);

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {[...Array(4)].map((_, i) => (
            <Card key={i}>
              <CardHeader>
                <Skeleton className="h-6 w-32" />
                <Skeleton className="h-4 w-48" />
              </CardHeader>
              <CardContent>
                <Skeleton className="h-24 w-full" />
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  if (isError) {
    return (
      <Card className="border-destructive">
        <CardHeader>
          <CardTitle className="text-destructive">Error Loading System Information</CardTitle>
          <CardDescription>
            {error instanceof Error ? error.message : "Failed to fetch system resource usage"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button onClick={() => void refreshNow()} variant="outline">
            <RefreshCw className="h-4 w-4 mr-2" />
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  const usage = data ?? current;
  if (!usage) return null;

  const containerised = Boolean(usage.container?.is_containerized);

  return (
    <div className="space-y-8">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h2 className="text-2xl font-bold flex items-center gap-2">Dashboard Host Resources</h2>
          <p className="text-muted-foreground text-sm mt-1">
            Host figures are what the kernel of the machine running this backend reports. Backend
            Container figures are the backend&apos;s own cgroup, capped by BACKEND_MEM_LIMIT and
            BACKEND_CPUS. Neither describes the managed clusters.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <RefreshButton label="Refresh host resources" lastUpdatedAt={dataUpdatedAt} onRefresh={refreshNow} />
        </div>
      </div>

      <HostSection host={usage.host ?? null} os={usage.os} python={usage.python} containerised={containerised} />

      <ContainerSection
        container={usage.container ?? null}
        network={usage.network}
        networkScope={usage.network_scope ?? (containerised ? "container" : "host")}
        previous={previous}
        timestamp={usage.timestamp}
      />
    </div>
  );
}

export default SystemResourceUsagePanel;
