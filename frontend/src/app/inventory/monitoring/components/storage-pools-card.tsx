"use client";

import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Database } from "lucide-react";

import { DetailListDialog } from "@/components/detail/detail-list-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { UsageBar } from "@/components/ui/usage-bar";
import { UsageCaption } from "@/components/ui/usage-caption";
import { TableLink } from "@/components/ui/table-link";
import { useByteFormat } from "@/hooks/use-byte-format";
import { formatLabel } from "@/lib/utils";
import { usageLevel, usagePercent } from "@/lib/usage-tone";
import type { StoragePoolMetrics } from "@/types/api";

interface StoragePoolsCardProps {
  pools: StoragePoolMetrics[];
  /** A cluster id, or "all". */
  selectedNode: string;
  /** The cluster's display name, from useClusterLabel. */
  labelOf: (nodeName: string) => string;
}

/**
 * The members a pool row covers, behind a count. Rendered only for more than one member: a
 * standalone host lists itself as its one member (services/lxd/storage_model.py). The names sit
 * behind a control because a MicroCloud row listing every hostname would set the card's width.
 */
function PoolMembers({ pool, clusterLabel }: { pool: StoragePoolMetrics; clusterLabel: string }) {
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const members = pool.members ?? [];
  const shared = pool.scope === "shared";
  const count = `${members.length} members`;
  return (
    <>
      <TableLink
        onClick={() => {
          setMounted(true);
          setOpen(true);
        }}
        aria-label={`Show the ${members.length} members holding ${pool.name} on ${clusterLabel}`}
      >
        {shared ? `shared by ${count}` : count}
      </TableLink>
      {mounted ? (
        <DetailListDialog
          open={open}
          onOpenChange={setOpen}
          title={`Members holding ${pool.name}`}
          description={
            shared
              ? `${clusterLabel}: one shared store, counted once.`
              : `${clusterLabel}: one pool per member, added together.`
          }
          items={members}
          itemLabel="member name"
          emptyTitle="No members"
        />
      ) : null}
    </>
  );
}

export function StoragePoolsCard({ pools, selectedNode, labelOf }: StoragePoolsCardProps) {
  const [sortDesc, setSortDesc] = useState(true);
  const { formatBytes } = useByteFormat();
  const rows = useMemo(
    () =>
      pools
        .filter((pool) => selectedNode === "all" || pool.node === selectedNode)
        .sort((a, b) =>
          sortDesc
            ? (b.usage_percent || 0) - (a.usage_percent || 0)
            : (a.usage_percent || 0) - (b.usage_percent || 0),
        ),
    [pools, selectedNode, sortDesc],
  );
  const scopeLabel = selectedNode === "all" ? null : labelOf(selectedNode);

  return (
    <Card data-slot="storage-pools">
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>
              Storage Pools
            </CardTitle>
            <CardDescription>
              {scopeLabel ? `Storage pool utilization on ${scopeLabel}` : "Storage pool utilization across all clusters"}
            </CardDescription>
          </div>
          {rows.length > 1 ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setSortDesc(!sortDesc)}
              className="flex items-center gap-1.5 h-8 px-2.5 sm:px-3 shrink-0"
            >
              {sortDesc ? (
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
          ) : null}
        </div>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <EmptyState
            icon={Database}
            title="No storage pools"
            description={
              scopeLabel
                ? `${scopeLabel} reported no storage pool.`
                : "No cluster reported a storage pool."
            }
            className="py-6"
          />
        ) : (
          <div className="space-y-4">
            {rows.map((pool) => {
              const clusterLabel = labelOf(pool.node);
              // From the byte counters, so the bar and the figure beside it cannot disagree.
              const percent = usagePercent(pool.used_bytes, pool.total_bytes);
              const figure = `${formatBytes(pool.used_bytes)} / ${formatBytes(pool.total_bytes)}`;
              return (
                <div key={`${pool.node}-${pool.name}-${pool.driver}`} className="space-y-1.5">
                  <div className="flex items-start justify-between gap-3 text-sm">
                    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="font-medium">{pool.name}</span>
                      <Badge variant="outline">{formatLabel(pool.driver)}</Badge>
                      {/* `node` is the registered cluster, named as the Clusters page names it.
                          The bytes of a MicroCloud pool live on its members. */}
                      <span className="text-muted-foreground">{clusterLabel}</span>
                      {(pool.members?.length ?? 0) > 1 ? (
                        <span className="text-muted-foreground">
                          <PoolMembers pool={pool} clusterLabel={clusterLabel} />
                        </span>
                      ) : null}
                    </div>
                    <span className="font-medium tabular-nums shrink-0">{figure}</span>
                  </div>
                  <UsageBar
                    percent={percent}
                    label={`${pool.name} on ${clusterLabel}`}
                    valueText={`${figure} (${percent.toFixed(1)}%)`}
                    className="h-2"
                  />
                  <p className="text-right text-xs">
                    <UsageCaption level={usageLevel(percent)}>{`${percent.toFixed(1)}% used`}</UsageCaption>
                  </p>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
