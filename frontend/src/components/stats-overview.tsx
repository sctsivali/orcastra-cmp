"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { VMListResponse, ClusterListResponse, ClusterStatus } from "@/types";

interface StatsOverviewProps {
  data?: VMListResponse;
  clustersData?: ClusterListResponse;
  isLoading?: boolean;
  clustersLoading?: boolean;
}

interface StatCardProps {
  label: string;
  value: string | number;
  subtext?: string;
  delay: number;
}

function StatCard({
  label,
  value,
  subtext,
  delay,
}: StatCardProps) {
  return (
    <Card
      className="animate-fade-in overflow-hidden"
      style={{ animationDelay: `${delay}ms` }}
    >
      <CardContent className="p-6">
        <div className="space-y-1">
          <p className="text-sm font-medium text-muted-foreground">{label}</p>
          <p className="text-3xl font-bold tracking-tight">{value}</p>
          {subtext && (
            <p className="text-xs text-muted-foreground">{subtext}</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function LoadingSkeleton() {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      {[...Array(4)].map((_, i) => (
        <Card key={i}>
          <CardContent className="p-6">
            <div className="flex items-center justify-between">
              <div className="space-y-2">
                <Skeleton className="h-4 w-20" />
                <Skeleton className="h-8 w-12" />
                <Skeleton className="h-3 w-24" />
              </div>
              <Skeleton className="h-12 w-12 rounded-xl" />
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export function StatsOverview({ data, clustersData, isLoading, clustersLoading }: StatsOverviewProps) {
  if (isLoading || clustersLoading) {
    return <LoadingSkeleton />;
  }

  const totalVMs = data?.total || 0;
  const runningVMs = data?.vms.filter((vm) => vm.status === "Running").length || 0;
  const stoppedVMs = data?.vms.filter((vm) => vm.status === "Stopped").length || 0;
  const containers = data?.vms.filter((vm) => vm.type === "container").length || 0;
  const virtualMachines =
    data?.vms.filter((vm) => vm.type === "virtual-machine").length || 0;
  
  // Count registered clusters by status
  const totalClusters = clustersData?.total || 0;
  const onlineClusters = clustersData?.clusters?.filter(c => c.status === ClusterStatus.ONLINE).length || 0;
  const offlineClusters = Math.max(totalClusters - onlineClusters, 0);

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <StatCard
        label="Total Instances"
        value={totalVMs}
        subtext={`${virtualMachines} VMs, ${containers} containers`}
        delay={0}
      />
      <StatCard
        label="Running"
        value={runningVMs}
        subtext={`${Math.round((runningVMs / Math.max(totalVMs, 1)) * 100)}% of total`}
        delay={50}
      />
      <StatCard
        label="Stopped"
        value={stoppedVMs}
        subtext="Available to start"
        delay={100}
      />
      <StatCard
        label="Registered Clusters"
        value={totalClusters}
        subtext={totalClusters > 0 ? `${onlineClusters} online, ${offlineClusters} offline` : "No clusters"}
        delay={150}
      />
    </div>
  );
}
