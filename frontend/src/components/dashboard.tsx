"use client";

import * as React from "react";
import { RotateCw, WifiOff } from "lucide-react";
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
import { usePermissions } from "@/hooks/use-permissions";
import { useActorQueryScope } from "@/hooks/use-query-scope";

export function Dashboard() {
  const { data: vmsData, isLoading: vmsLoading, refetch, dataUpdatedAt } = useInstances();
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
  const handleOverviewRefresh = React.useCallback(async () => {
    await Promise.all([
      refetch({ throwOnError: true }),
      queryClient.refetchQueries({ queryKey: queryKeys.allProjectsFor(scopeKey), type: "active" }),
      queryClient.refetchQueries({ queryKey: queryKeys.clustersFor(scopeKey, false), type: "active" }),
      queryClient.refetchQueries({ queryKey: queryKeys.nodesFor(scopeKey), type: "active" }),
      queryClient.refetchQueries({ queryKey: ["warnings", scopeKey], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["systemWarnings", scopeKey], type: "active" }),
    ]);
  }, [queryClient, refetch, scopeKey]);

  const isPrimaryLoading = vmsLoading || projectsLoading || clustersLoading;
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

  return (
    <div key={scopeKey} className="space-y-8">
      {/* Stats Overview - shows its own loading state */}
      <StatsOverview data={vmsData} clustersData={clustersData} isLoading={vmsLoading} clustersLoading={clustersLoading} />

      {/* Alarm Statistics - Only shows when there are warnings (data already prefetched above) */}
      <AlarmStatistics prefetchedWarnings={warningsData} prefetchedSystemWarnings={systemWarnings} />

      {/* Nodes Status */}
      {isPartner ? (
        <NodeStatusCards lastUpdatedAt={dataUpdatedAt} onRefresh={handleOverviewRefresh} />
      ) : (
        <TenantClusterStatusCards />
      )}

      {/* Projects Overview */}
      <ProjectsSection
        projectsData={projectsData}
        projectsLoading={projectsLoading}
        projectsError={projectsError}
        vmsData={vmsData?.vms || []}
        refetch={refetch}
        lastUpdatedAt={dataUpdatedAt}
        projectsUpdatedAt={projectsUpdatedAt}
      />
    </div>
  );
}
