"use client";

import { useState } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { formatBytes } from "@/lib/utils";
import type { StoragePoolMetrics } from "@/types/api";

interface StoragePoolsCardProps {
  pools: StoragePoolMetrics[];
  /** A cluster id, or "all". */
  selectedNode: string;
  isPartner: boolean;
}

export function StoragePoolsCard({ pools, selectedNode, isPartner }: StoragePoolsCardProps) {
  const [storagePoolsSortDesc, setStoragePoolsSortDesc] = useState(true);
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>
              Storage Pools
            </CardTitle>
            <CardDescription>
              Storage pool utilization across all clusters
            </CardDescription>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setStoragePoolsSortDesc(!storagePoolsSortDesc)}
            className="flex items-center gap-1.5 h-8 px-2.5 sm:px-3 shrink-0"
          >
            {storagePoolsSortDesc ? (
              <>
                <ArrowDown className="h-4 w-4" />
                <span className="text-xs">Highest</span>
              </>
            ) : (
              <>
                <ArrowUp className="h-4 w-4" />
                <span className="text-xs">Lowest</span>
              </>
            )}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        <div className="space-y-4">
          {[...pools]
            .filter(pool => selectedNode === "all" || pool.node === selectedNode)
            .sort((a, b) => storagePoolsSortDesc 
              ? (b.usage_percent || 0) - (a.usage_percent || 0)
              : (a.usage_percent || 0) - (b.usage_percent || 0))
            .map((pool) => (
            <div
              key={`${pool.node}-${pool.name}-${pool.driver}`}
              className="space-y-1.5"
            >
              <div className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{pool.name}</span>
                  <Badge variant="outline" className="text-xs">{pool.driver}</Badge>
                  {/* note: `node` is the registered cluster. On a MicroCloud the bytes live
                      on the members, and a shared pool is one store all of them can see,
                      so the row says which rather than implying the cluster owns a disk.
                      Member hostnames are operator content, gated like status_message
                      above: a tenant is told the shape, not the names. */}
                  <span className="text-muted-foreground">
                    ({pool.node}
                    {pool.members && pool.members.length > 0
                      ? isPartner
                        ? `: ${pool.scope === "shared" ? "shared by " : ""}${pool.members.join(", ")}`
                        : pool.scope === "shared"
                          ? `, shared by ${pool.members.length} members`
                          : `, ${pool.members.length} members`
                      : ""}
                    )
                  </span>
                </div>
                <span className="font-medium">
                  {formatBytes(pool.used_bytes)} / {formatBytes(pool.total_bytes)}
                </span>
              </div>
              <Progress value={pool.usage_percent} className="h-2" />
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
