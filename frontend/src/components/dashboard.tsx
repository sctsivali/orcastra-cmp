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
import { EmptyInstancesIllustration } from "@/components/illustrations/scenes/onboarding";

export function Dashboard() {
  const { data: vmsData, isLoading: vmsLoading, refetch, refreshNow: refreshInstances, dataUpdatedAt, isError: vmsFailed, error: vmsError } = useInstances();
  const { data: projectsData, isLoading: projectsLoading, error: projectsError, dataUpdatedAt: projectsUpdatedAt } = useAllProjects();
  const { data: clustersData, isLoading: clustersLoading } = useClusters();
  const queryClient = useQueryClient();
  const { isPartner } = usePermissions();
  const { scopeKey } = useActorQueryScope();

  // Read alongside the primary queries rather than after them.
  const { data: warningsData } = useWarnings();
  const { data: systemWarnings } = useSystemWarnings();

  // No success toast: the refresh control reports its own outcome inline, and with TOAST_LIMIT 1
  // a toast would evict whatever the operator was reading.
  // important: the fleet read and the cluster status read bypass the backend cache, like the
  // Instances page Refresh; a cached answer would report "Updated" over rows changed elsewhere
  // (ex; through the LXD CLI). Only these two are forced: each forced read spends the
  // per-operator budget the delete and create reconciles also draw on.
  // fetchQuery carries its own queryFn, so the forced read cannot fall back to whichever
  // observer's queryFn the shared key last registered.
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
  // the slowest cluster, up to the backend's 30 s inventory wall, and would hold the whole
  // Overview behind the loader; the instance figures and project counts carry their own loading
  // state.
  const isPrimaryLoading = projectsLoading || clustersLoading;
  // A primary read stuck past 25 seconds shows a retry panel instead of an endless loader.
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

  // important: a failed fleet read with no earlier data says it could not be read. Counters of 0
  // would claim the deployment is empty (ex; while Vault is sealed).
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
      <StatsOverview data={vmsData} clustersData={clustersData} isLoading={vmsLoading} clustersLoading={clustersLoading} />

      <AlarmStatistics prefetchedWarnings={warningsData} prefetchedSystemWarnings={systemWarnings} />

      {isPartner ? (
        <NodeStatusCards onRefresh={handleOverviewRefresh} />
      ) : (
        <TenantClusterStatusCards />
      )}

      {/* A Tenant whose access reaches nothing is told so instead of seeing an empty grid, and
          pointed at the one party who can change it. Only on a complete read: an unreachable
          cluster or a failed list is not "nothing". */}
      {!isPartner && tenantReachesNothing(projectsData, vmsData) ? (
        <EmptyState
          icon={KeyRound}
          illustration={EmptyInstancesIllustration}
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
  // `default` is listed for every cluster in scope, so it alone is not a workload.
  const named = projects.some((host) => host.projects.some((project) => project.name !== "default"));
  return !named && vms.vms.length === 0;
}
