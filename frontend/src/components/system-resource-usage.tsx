"use client";

import { useEffect, useState } from "react";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { api } from "@/lib/api";
import { describeReadError } from "@/lib/read-error";
import { formatClockTime } from "@/lib/utils";
import type { SystemResourceUsage } from "@/types";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Skeleton } from "@/components/ui/skeleton";
import { HostSection } from "@/components/system-resources/host-section";
import { ContainerSection } from "@/components/system-resources/container-section";
import {
  containerCpuReading,
  hostCpuReading,
  throttleReading,
} from "@/components/system-resources/rates";

/** Two consecutive answers, kept together so a delta never straddles a render. */
interface Samples {
  previous: SystemResourceUsage | null;
  current: SystemResourceUsage | null;
}

/**
 * Settings > Dashboard Host. Two scopes, each labelled for what it is: the host machine the
 * backend runs on (what its kernel reports, which on an LXD virtual machine is the VM) and the
 * backend's own container (its cgroup limits and usage, what docker stats prints).
 *
 * important: CPU percents and network rates are measured here, between two consecutive answers,
 * from counters that are the same whichever uvicorn worker answers. The server's own CPU percent
 * is per worker and could cover an hour-long gap; it is used for the first frame only when its
 * window is as short as a poll.
 */
export function SystemResourceUsagePanel() {
  const [samples, setSamples] = useState<Samples>({ previous: null, current: null });

  const { data, isLoading, isError, error, refreshNow, dataUpdatedAt } = useRefreshableQuery({
    queryKey: ["systemResourceUsage"],
    queryFn: ({ fresh, signal }) => api.system.getResourceUsage({ fresh, signal }),
    refetchInterval: 5000,
    staleTime: 3000,
    gcTime: 60000,
  });

  // Both halves move in one state update and the panel renders from that state, so a frame
  // never pairs a new answer with the previous pair's window.
  useEffect(() => {
    if (!data) return;
    setSamples((s) => {
      if (s.current && s.current.timestamp === data.timestamp) return s;
      return { previous: s.current, current: data };
    });
  }, [data]);

  if (isLoading) {
    return (
      <div className="space-y-6" role="status" aria-label="Loading dashboard host resources">
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
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

  const usage = samples.current ?? data;

  if (!usage) {
    if (!isError) return null;
    return (
      <Card>
        <CardContent className="pt-6">
          <ReadErrorState noun="dashboard host figures" error={error} onRetry={() => void refreshNow().catch(() => undefined)} />
        </CardContent>
      </Card>
    );
  }

  const previous = samples.current ? samples.previous : null;
  const containerised = Boolean(usage.container?.is_containerized);

  return (
    <div className="space-y-8">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h2 className="text-2xl font-bold">Dashboard Host Resources</h2>
          <p className="text-muted-foreground text-sm mt-1">
            Resource use of the server that runs this dashboard and of its backend container.
            These figures do not describe your managed clusters.
          </p>
        </div>
        <RefreshButton label="Refresh host resources" lastUpdatedAt={dataUpdatedAt} onRefresh={refreshNow} />
      </div>

      {/* A failed poll keeps the last figures on screen: React Query keeps `data` beside the
          error, and polling carries on, so the next good answer clears this by itself. */}
      {isError && (
        <PartialReadNotice data-testid="host-resources-stale">
          Could not refresh: {describeReadError(error).reason}. Showing figures from{" "}
          {formatClockTime(dataUpdatedAt)}; the panel keeps trying every 5 s.
        </PartialReadNotice>
      )}

      <HostSection
        host={usage.host ?? null}
        os={usage.os}
        cpu={hostCpuReading(previous, usage)}
        containerised={containerised}
      />

      <ContainerSection
        container={usage.container ?? null}
        python={usage.python}
        hostCpus={usage.host?.cpu.count_logical ?? null}
        cpu={containerCpuReading(previous, usage)}
        throttle={throttleReading(previous, usage)}
        network={usage.network}
        networkScope={usage.network_scope ?? (containerised ? "container" : "host")}
        previousNetwork={previous ? { network: previous.network, timestamp: previous.timestamp } : null}
        timestamp={usage.timestamp}
      />
    </div>
  );
}

export default SystemResourceUsagePanel;
