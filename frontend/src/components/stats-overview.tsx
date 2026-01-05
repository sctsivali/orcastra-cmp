"use client";

import { Server, Activity, HardDrive, Cpu } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { VMListResponse, ClusterListResponse, ClusterStatus } from "@/types";
import { cn } from "@/lib/utils";

interface StatsOverviewProps {
  data?: VMListResponse;
  clustersData?: ClusterListResponse;
  isLoading?: boolean;
  clustersLoading?: boolean;
}

interface StatCardProps {
  icon: React.ReactNode;
  label: string;
  value: string | number;
  subtext?: string;
  color: string;
  delay: number;
}

function StatCard({
  icon,
  label,
  value,
  subtext,
  color,
  delay,
}: StatCardProps) {
  return (
    <Card
      className="animate-fade-in overflow-hidden"
      style={{ animationDelay: `${delay}ms` }}
    >
      <CardContent className="p-6">
        <div className="flex items-center justify-between">
          <div className="space-y-1">
            <p className="text-sm font-medium text-muted-foreground">{label}</p>
            <p className="text-3xl font-bold tracking-tight">{value}</p>
            {subtext && (
              <p className="text-xs text-muted-foreground">{subtext}</p>
            )}
          </div>
          <div
            className={cn(
              "flex h-12 w-12 items-center justify-center rounded-xl",
              color
            )}
          >
            {icon}
          </div>
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
  // Calculate cluster types from registered clusters
  const lxdClusters = clustersData?.clusters?.filter(c => c.cluster_type?.toUpperCase() === "LXD").length || 0;
  const microcloudClusters = clustersData?.clusters?.filter(c => c.cluster_type?.toUpperCase() === "MICROCLOUD").length || 0;

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <StatCard
        icon={<Server className="h-6 w-6 text-violet-600" />}
        label="Total Instances"
        value={totalVMs}
        subtext={`${virtualMachines} VMs, ${containers} containers`}
        color="bg-violet-100 dark:bg-violet-950"
        delay={0}
      />
      <StatCard
        icon={<Activity className="h-6 w-6 text-emerald-600" />}
        label="Running"
        value={runningVMs}
        subtext={`${Math.round((runningVMs / Math.max(totalVMs, 1)) * 100)}% of total`}
        color="bg-emerald-100 dark:bg-emerald-950"
        delay={50}
      />
      <StatCard
        icon={<HardDrive className="h-6 w-6 text-amber-600" />}
        label="Stopped"
        value={stoppedVMs}
        subtext="Available to start"
        color="bg-amber-100 dark:bg-amber-950"
        delay={100}
      />
      <StatCard
        icon={<Cpu className="h-6 w-6 text-blue-600" />}
        label="Registered Clusters"
        value={totalClusters}
        subtext={`${lxdClusters} LXD, ${microcloudClusters} MicroCloud`}
        color="bg-blue-100 dark:bg-blue-950"
        delay={150}
      />
    </div>
  );
}
