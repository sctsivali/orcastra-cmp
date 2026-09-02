"use client";

import * as React from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Progress } from "@/components/ui/progress";
import { TableLink } from "@/components/ui/table-link";
import { Cpu, HardDrive, Layers, MemoryStick, Sparkles } from "lucide-react";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { formatLabel } from "@/lib/utils";
import { usageTone } from "@/lib/usage-tone";
import type { MemberResources } from "@/types/api";
import { GpuPanel } from "../gpu";
import { GPU_UTILIZATION_NOTE } from "@/lib/gpu";
import { formatBytes, formatBytesCompact } from "./format";

interface MemberResourcePanelsProps {
  resources: MemberResources;
  memberName: string | null;
  onShowInstances: (filter: "running" | "stopped") => void;
}

/**
 * The populated body of the member resource dialog.
 *
 * important: `degraded_parts` is not decoration. A storage or instance read that failed used to
 * be swallowed and the panel rendered zeroes, so "0 B / 0 B" and a missing Storage Pools card
 * read as "this member has no storage" rather than as "we could not find out". Anything the
 * backend marks lost is named here, next to the number it affects.
 */
export function MemberResourcePanels({
  resources,
  memberName,
  onShowInstances,
}: MemberResourcePanelsProps) {
  const lostParts = resources.degraded_parts ?? [];
  const storageLost = lostParts.some((part) => part.startsWith("storage"));
  const instancesLost = lostParts.includes("instances");

  return (
    <div className="space-y-6">
      {resources.degraded && (
        <PartialReadNotice>
          <>
            Part of this reading did not come back from the cluster
            {storageLost && instancesLost
              ? ", so the storage and instance figures below are floors rather than measurements."
              : storageLost
                ? ", so the storage figures below are floors rather than measurements."
                : instancesLost
                  ? ", so the instance and allocation figures below are floors rather than measurements."
                  : "."}{" "}
            Refreshing usually clears it.
          </>
        </PartialReadNotice>
      )}

      {/* Overview Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* CPU */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <Cpu className="h-4 w-4 text-muted-foreground" />
              CPU
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              <div className="flex items-baseline justify-center gap-1">
                <span className="text-2xl font-bold">{resources.cpu_used}</span>
                <span className="text-sm text-muted-foreground">/ {resources.cpu_total} cores</span>
              </div>
              <Progress 
                value={resources.cpu_usage_percent} 
                className="h-2"
              />
              <p className={`text-sm font-medium text-center ${usageTone(resources.cpu_usage_percent).text}`}>
                {resources.cpu_usage_percent.toFixed(1)}% used
              </p>
            </div>
          </CardContent>
        </Card>

        {/* Memory */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <MemoryStick className="h-4 w-4 text-muted-foreground" />
              Memory
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              <div className="text-center text-sm">
                <span className="text-2xl font-bold">{formatBytesCompact(resources.memory_used).value}</span>
                <span className="text-xs text-muted-foreground"> {formatBytesCompact(resources.memory_used).unit}</span>
                <span className="text-xs text-muted-foreground"> / </span>
                <span className="text-base font-semibold">{formatBytesCompact(resources.memory_total).value}</span>
                <span className="text-xs text-muted-foreground"> {formatBytesCompact(resources.memory_total).unit}</span>
              </div>
              <Progress 
                value={resources.memory_usage_percent} 
                className="h-2"
              />
              <p className={`text-sm font-medium text-center ${usageTone(resources.memory_usage_percent).text}`}>
                {resources.memory_usage_percent.toFixed(1)}% used
              </p>
            </div>
          </CardContent>
        </Card>

        {/* Storage */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <HardDrive className="h-4 w-4 text-muted-foreground" />
              Storage
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              <div className="text-center text-sm">
                <span className="text-2xl font-bold">{formatBytesCompact(resources.storage_used).value}</span>
                <span className="text-xs text-muted-foreground"> {formatBytesCompact(resources.storage_used).unit}</span>
                <span className="text-xs text-muted-foreground"> / </span>
                <span className="text-base font-semibold">{formatBytesCompact(resources.storage_total).value}</span>
                <span className="text-xs text-muted-foreground"> {formatBytesCompact(resources.storage_total).unit}</span>
              </div>
              <Progress 
                value={resources.storage_usage_percent} 
                className="h-2"
              />
              <p className={`text-sm font-medium text-center ${usageTone(resources.storage_usage_percent).text}`}>
                {resources.storage_usage_percent.toFixed(1)}% used
              </p>
            </div>
          </CardContent>
        </Card>

        {/* Instances */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <Layers className="h-4 w-4 text-muted-foreground" />
              Instances
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              <div className="flex items-baseline justify-center gap-1">
                <span className="text-2xl font-bold">{resources.instances_running}</span>
                <span className="text-sm text-muted-foreground">/ {resources.instances_count} total</span>
              </div>
              <div className="flex flex-col items-center justify-center gap-2">
                {resources.instances_running > 0 && (
                  <TableLink
                    onClick={() => onShowInstances("running")}
                    aria-label={`Show the running instances on ${memberName}`}
                  >
                    {`${resources.instances_running} Running`}
                  </TableLink>
                )}
                {resources.instances_count - resources.instances_running > 0 && (
                  <TableLink
                    onClick={() => onShowInstances("stopped")}
                    aria-label={`Show the stopped instances on ${memberName}`}
                  >
                    {`${resources.instances_count - resources.instances_running} Stopped`}
                  </TableLink>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* GPU */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Sparkles className="h-4 w-4" />
            GPU
          </CardTitle>
          <CardDescription>{GPU_UTILIZATION_NOTE}</CardDescription>
        </CardHeader>
        <CardContent>
          <GpuPanel src={resources} dense />
        </CardContent>
      </Card>

      {/* Storage Pools */}
      {/* important: an empty list used to make this card disappear with no explanation, which
          is the same silence the degraded marker exists to break. A failed read says so. */}
      {storageLost && !(resources.storage_pools ?? []).length ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Storage Pools</CardTitle>
          </CardHeader>
          <CardContent>
            <EmptyState
              icon={HardDrive}
              title="Storage could not be read"
              description="The cluster did not answer for this member's storage pools, so no pool is listed and the totals above exclude them."
            />
          </CardContent>
        </Card>
      ) : null}
      {resources.storage_pools && resources.storage_pools.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Storage Pools</CardTitle>
            <CardDescription>Individual storage pool utilization</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {resources.storage_pools.map((pool, index) => {
                const usagePercent = pool.space_total > 0
                  ? ((pool.space_used / pool.space_total) * 100)
                  : 0;
            
                return (
                  <div key={index} className="space-y-2">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <HardDrive className="h-4 w-4 text-muted-foreground" />
                        <span className="font-medium">{pool.name}</span>
                        <span className="text-muted-foreground">{formatLabel(pool.driver)}</span>
                      </div>
                      <span className="text-sm text-muted-foreground">
                        {formatBytes(pool.space_used)} / {formatBytes(pool.space_total)}
                      </span>
                    </div>
                    <div className="space-y-1">
                      <Progress value={usagePercent} className="h-2" />
                      <p className={`text-xs ${usageTone(usagePercent).text}`}>
                        {usagePercent.toFixed(1)}% used • {formatBytes(pool.space_available)} available
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Resource Summary */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Resource Summary</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm">
            <div>
              <p className="text-muted-foreground mb-1">Available CPU</p>
              <p className="font-medium">{resources.cpu_available} cores</p>
            </div>
            <div>
              <p className="text-muted-foreground mb-1">Available Memory</p>
              <p className="font-medium">{formatBytes(resources.memory_available)}</p>
            </div>
            <div>
              <p className="text-muted-foreground mb-1">Available Storage</p>
              <p className="font-medium">{formatBytes(resources.storage_available)}</p>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
