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
  const incusClusters = clustersData?.clusters?.filter(c => c.cluster_type?.toUpperCase() === "INCUS").length || 0;

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <StatCard
        icon={<div className="h-8 w-8 border border-white dark:border-black rounded-full  flex items-center justify-center"><Server className="h-4 w-4 text-violet-600" /></div>}
        label="Total Instances"
        value={totalVMs}
        subtext={`${virtualMachines} VMs, ${containers} containers`}
        color="bg-transparent"
        delay={0}
      />
      <StatCard
        icon={<div className="h-8 w-8 border border-white dark:border-black rounded-full  flex items-center justify-center"><Activity className="h-4 w-4 text-emerald-600" /></div>}
        label="Running"
        value={runningVMs}
        subtext={`${Math.round((runningVMs / Math.max(totalVMs, 1)) * 100)}% of total`}
        color="bg-transparent"
        delay={50}
      />
      <StatCard
        icon={<div className="h-8 w-8 border border-white dark:border-black rounded-full  flex items-center justify-center"><HardDrive className="h-4 w-4 text-amber-600" /></div>}
        label="Stopped"
        value={stoppedVMs}
        subtext="Available to start"
        color="bg-transparent"
        delay={100}
      />
      <StatCard
        icon={<div className="h-8 w-8 border border-white dark:border-black rounded-full  flex items-center justify-center"><Cpu className="h-4 w-4 text-blue-600" /></div>}
        label="Registered Clusters"
        value={totalClusters}
        subtext={[
          lxdClusters > 0 ? `${lxdClusters} LXD` : null,
          microcloudClusters > 0 ? `${microcloudClusters} MicroCloud` : null,
          incusClusters > 0 ? `${incusClusters} Incus` : null,
        ].filter(Boolean).join(", ") || "No clusters"}
        color="bg-transparent"
        delay={150}
      />
    </div>
  );
}
