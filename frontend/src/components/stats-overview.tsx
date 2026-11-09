"use client";

import type * as React from "react";
import { Card, CardContent } from "@/components/ui/card";
import type { IllustrationProps } from "@/components/illustrations/illustration-frame";
import { ClustersIllustration, InstancesIllustration, RunningIllustration, StoppedIllustration } from "@/components/illustrations/scenes/kpi";
import { Skeleton } from "@/components/ui/skeleton";
import { VMListResponse, ClusterListResponse, ClusterStatus } from "@/types";
import { plural } from "@/lib/utils";

interface StatsOverviewProps {
  data?: VMListResponse;
  clustersData?: ClusterListResponse;
  isLoading?: boolean;
  clustersLoading?: boolean;
}

interface StatCardProps {
  label: string;
  /** null while the read behind this card is outstanding: the card holds its place and its label. */
  value: string | number | null;
  subtext?: string;
  delay: number;
  /** The tile's subject; it drops out when the tile is too narrow (illustrations.css, .illo-kpi). */
  illustration: React.ComponentType<IllustrationProps>;
}

function StatCard({
  label,
  value,
  subtext,
  delay,
  illustration: Illustration,
}: StatCardProps) {
  return (
    <Card
      className="illo-kpi animate-fade-in"
      style={{ animationDelay: `${delay}ms` }}
    >
      <CardContent className="flex items-start justify-between gap-4 p-6">
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-medium text-muted-foreground">{label}</p>
          {value === null ? (
            <>
              {/* Same line boxes as the loaded card (text-3xl, text-xs), so nothing shifts. */}
              <Skeleton className="my-1 h-7 w-12" aria-hidden="true" />
              <Skeleton className="h-3 w-24" aria-hidden="true" />
              <span className="sr-only">Loading {label}</span>
            </>
          ) : (
            <>
              <p className="text-3xl font-bold tracking-tight">{value}</p>
              {subtext && (
                <p className="text-xs text-muted-foreground">{subtext}</p>
              )}
            </>
          )}
        </div>
        <Illustration size="sm" />
      </CardContent>
    </Card>
  );
}

export function StatsOverview({ data, clustersData, isLoading, clustersLoading }: StatsOverviewProps) {
  // important: each card waits for its own read. The instance list can take up to the backend's
  // 30 s inventory wall when one cluster is slow, and the cluster count must not wait with it.
  const instances = isLoading ? null : data;
  const clusters = clustersLoading ? null : clustersData;

  const totalVMs = instances?.total || 0;
  const runningVMs = instances?.vms.filter((vm) => vm.status === "Running").length || 0;
  const stoppedVMs = instances?.vms.filter((vm) => vm.status === "Stopped").length || 0;
  const containers = instances?.vms.filter((vm) => vm.type === "container").length || 0;
  const virtualMachines =
    instances?.vms.filter((vm) => vm.type === "virtual-machine").length || 0;
  
  // Count registered clusters by status
  const totalClusters = clusters?.total || 0;
  const onlineClusters = clusters?.clusters?.filter(c => c.status === ClusterStatus.ONLINE).length || 0;
  const offlineClusters = Math.max(totalClusters - onlineClusters, 0);

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <StatCard
        label="Total Instances"
        illustration={InstancesIllustration}
        value={instances === null ? null : totalVMs}
        subtext={`${plural(virtualMachines, "VM")}, ${plural(containers, "container")}`}
        delay={0}
      />
      <StatCard
        label="Running"
        illustration={RunningIllustration}
        value={instances === null ? null : runningVMs}
        subtext={`${Math.round((runningVMs / Math.max(totalVMs, 1)) * 100)}% of total`}
        delay={50}
      />
      <StatCard
        label="Stopped"
        illustration={StoppedIllustration}
        value={instances === null ? null : stoppedVMs}
        subtext="Available to start"
        delay={100}
      />
      <StatCard
        label="Registered Clusters"
        illustration={ClustersIllustration}
        value={clusters === null ? null : totalClusters}
        subtext={totalClusters > 0 ? `${onlineClusters} online, ${offlineClusters} offline` : "No clusters"}
        delay={150}
      />
    </div>
  );
}
