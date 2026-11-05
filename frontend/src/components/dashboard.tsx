"use client";

import * as React from "react";
import { KeyRound, RotateCw, WifiOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { LoadingLottiePageEnhanced } from "@/components/loading-lottie";
import { NodeStatusCards } from "@/components/node-status";
import { TenantClusterStatusCards } from "@/components/node-status/tenant-cluster-status-cards";
import { StatsOverview } from "@/components/stats-overview";
import { AlarmStatistics } from "@/components/dashboard/alarm-statistics";
import { ProjectsSection } from "@/components/dashboard/projects-section";
import {
  useInstances,
  useAllProjects,
  useClusters,
  useWarnings,
  useSystemWarnings,
  queryKeys } from "@/hooks/use-lxd";
import { useQueryClient } from "@tanstack/react-query";
import { nodeAPI } from "@/lib/api";
import { usePermissions } from "@/hooks/use-permissions";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { EmptyState } from "@/components/ui/empty-state";
import type { ProjectListResponse, VMListResponse } from "@/types/api";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { collectedAtMs } from "@/lib/utils";

export function Dashboard() {
  const { data: vmsData, isLoading: vmsLoading, refetch, refreshNow: refreshInstances, dataUpdatedAt, isError: vmsFailed, error: vmsError } = useInstances();
  const { data: projectsData, isLoading: projectsLoading, error: projectsError, dataUpdatedAt: projectsUpdatedAt } = useAllProjects();
  const { data: clustersData, isLoading: clustersLoading } = useClusters();
  const queryClient = useQueryClient();
  const { isPartner } = usePermissions();
  const { scopeKey } = useActorQueryScope();

  // pre-fetch alarm & node data in parallel with primary queries (eliminates waterfall)
  const { data: warningsData } = useWarnings();
  const { data: systemWarnings } = useSystemWarnings();

  // note: no success toast. The refresh control reports its own outcome inline
  // (spinner, tick, "Updated just now"), and a toast here would evict whatever
  // the operator was actually reading, since TOAST_LIMIT is 1.
  // important: the fleet read and the cluster status read go past the backend cache, like the
  // Refresh on the Instances page. A plain refetch answered from that cache, so after a change
  // made elsewhere (OrcaHub, the LXD CLI) the button reported "Updated" over the old rows, and the
  // Clusters region it sits in still read "Updated 2m ago" a moment after the click (owner
  // decision 2026-11-05). Only these two are forced: each forced read spends the per-operator
  // budget the delete and create reconciles also draw on.
  // note: fetchQuery with its own queryFn, not another observer's refreshNow, so the forced
  // read cannot fall back to whichever observer's queryFn the shared key last registered.
  const handleOverviewRefresh = React.useCallback(async () => {
    await Promise.all([
      refreshInstances(),
      queryClient.refetchQueries({ queryKey: queryKeys.allProjectsFor(scopeKey), type: "active" }),
      queryClient.refetchQueries({ queryKey: queryKeys.clustersFor(scopeKey, false), type: "active" }),
      queryClient.fetchQuery({
        queryKey: queryKeys.nodesFor(scopeKey),
        queryFn: ({ signal }) => nodeAPI.status({ fresh: true, signal }),
        staleTime: 0,
      }),
      queryClient.refetchQueries({ queryKey: ["warnings", scopeKey], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["systemWarnings", scopeKey], type: "active" }),
    ]);
  }, [queryClient, refreshInstances, scopeKey]);

  // important: the page waits for the cluster and project reads only. The instance list waits for
  // the slowest cluster, up to the backend's 30 s inventory wall, and one slow cluster used to hold
  // the whole Overview behind the loader and then the retry panel. The instance figures and the
  // project counts carry their own loading state instead.
  const isPrimaryLoading = projectsLoading || clustersLoading;
  // important: never hard-block the dashboard forever on a slow/stuck query.
  // After a grace period, surface a retry panel instead of an infinite 95%
  // loader (the backend now responds fast; this is a defensive safety net).
  const [loaderTimedOut, setLoaderTimedOut] = React.useState(false);
  React.useEffect(() => {
    if (!isPrimaryLoading) {
      setLoaderTimedOut(false);
      return;
    }
    const t = setTimeout(() => setLoaderTimedOut(true), 25000);
    return () => clearTimeout(t);
  }, [isPrimaryLoading]);

  if (isPrimaryLoading && !loaderTimedOut) {
    return <LoadingLottiePageEnhanced context="dashboard" />;
  }

  if (isPrimaryLoading && loaderTimedOut) {
    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 text-center">
        <WifiOff className="h-10 w-10 text-muted-foreground" />
        <div className="space-y-1">
          <p className="text-base font-medium">This is taking longer than usual</p>
          <p className="max-w-md text-sm text-muted-foreground">
            The dashboard could not load all data in time. The backend or a cluster may be slow to respond.
          </p>
        </div>
        <Button
          onClick={() => {
            setLoaderTimedOut(false);
            void handleOverviewRefresh();
          }}
        >
          <RotateCw className="mr-2 h-4 w-4" />
          Retry
        </Button>
      </div>
    );
  }

  // important: a failed fleet read with nothing from before rendered every counter as 0, which a
  // sealed Vault turned into "this deployment is empty" (issue #15). Say that it could not be read.
  if (vmsFailed && !vmsData) {
    return (
      <ReadErrorState
        noun="inventory"
        error={vmsError}
        onRetry={() => void handleOverviewRefresh().catch(() => undefined)}
      />
    );
  }

  return (
    <div key={scopeKey} className="space-y-8">
      {/* Stats Overview - shows its own loading state */}
      <StatsOverview data={vmsData} clustersData={clustersData} isLoading={vmsLoading} clustersLoading={clustersLoading} />

      {/* Alarm Statistics - Only shows when there are warnings (data already prefetched above) */}
      <AlarmStatistics prefetchedWarnings={warningsData} prefetchedSystemWarnings={systemWarnings} />

      {/* Nodes Status */}
      {isPartner ? (
        <NodeStatusCards onRefresh={handleOverviewRefresh} />
      ) : (
        <TenantClusterStatusCards />
      )}

      {/* Projects Overview. A Tenant whose access reaches nothing is told so instead of reading
          an empty grid, and pointed at the one party who can change it. Only on a complete read:
          an unreachable cluster or a failed list is not "nothing". */}
      {!isPartner && tenantReachesNothing(projectsData, vmsData) ? (
        <EmptyState
          icon={KeyRound}
          title="Nothing to show yet"
          description="Your access does not reach any instance right now. If your subscription ended, contact your provider to renew it."
        />
      ) : (
      <ProjectsSection
        projectsData={projectsData}
        projectsLoading={projectsLoading}
        projectsError={projectsError}
        vmsData={vmsData?.vms || []}
        instancesPending={vmsLoading}
        instancesUnread={vmsData?.unreachable_nodes}
        instancesUnreadReasons={vmsData?.node_errors}
        refetch={refetch}
        lastUpdatedAt={collectedAtMs(vmsData?.collected_at) || dataUpdatedAt}
        projectsUpdatedAt={projectsUpdatedAt}
      />
      )}
    </div>
  );
}

function tenantReachesNothing(
  projects: ProjectListResponse[] | undefined,
  vms: VMListResponse | undefined,
): boolean {
  if (!projects || !vms) return false;
  if (projects.some((host) => host.reachable === false)) return false;
  if ((vms.unreachable_nodes?.length ?? 0) > 0) return false;
  // note: `default` is listed for every cluster in scope, so it alone is not a workload.
  const named = projects.some((host) => host.projects.some((project) => project.name !== "default"));
  return !named && vms.vms.length === 0;
}
