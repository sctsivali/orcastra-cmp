"use client";

import { Cpu, HardDrive, MemoryStick, Sparkles } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { GpuPanel } from "@/components/gpu";
import { GPU_UTILIZATION_NOTE } from "@/lib/gpu";
import { usePermissions } from "@/hooks/use-permissions";
import { formatBytes, formatLabel } from "@/lib/utils";
import type { NodeResources } from "@/types";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { UsageMeter, usagePercent } from "./usage-meter";

/**
 * The four tabs that read NodeResources.
 *
 * They share a file because they share a data source: whatever the modal decides is the
 * freshest NodeResources it has, whether that came from the list row or from the live
 * per-cluster read. The System tab is separate for the same reason, in the other direction.
 */

const UNAVAILABLE = "did not report its hardware";

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-sm font-medium text-muted-foreground">{label}</p>
      <p className="text-lg font-semibold">{value}</p>
    </div>
  );
}

function Unavailable({ what }: { what: string }) {
  return <p className="text-muted-foreground">No {what} information available</p>;
}

export function NodeCpuPanel({
  resources,
  architecture,
}: {
  resources: NodeResources | null;
  architecture: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Cpu className="h-5 w-5" />
          CPU Information
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {resources ? (
          <>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Architecture" value={architecture} />
              <Field label="Total Cores" value={String(resources.cpu_total)} />
              <Field label="CPU Sockets" value={String(resources.cpu_sockets)} />
              <Field
                label="Cores per Socket"
                value={
                  resources.cpu_sockets > 0
                    ? String(Math.floor(resources.cpu_total / resources.cpu_sockets))
                    : "N/A"
                }
              />
            </div>

            <div className="border-t pt-4">
              <h4 className="mb-3 text-sm font-semibold">Resource Usage</h4>
              <div className="space-y-3">
                <UsageMeter
                  label="Used"
                  total={resources.cpu_total}
                  used={resources.cpu_used}
                  value={`${resources.cpu_used} / ${resources.cpu_total} cores (${usagePercent(
                    resources.cpu_used,
                    resources.cpu_total,
                  ).toFixed(1)}%)`}
                />
                <div className="flex justify-between text-sm">
                  <span className="text-muted-foreground">Available</span>
                  <span className="font-semibold tabular-nums">
                    {resources.cpu_available} cores
                  </span>
                </div>
              </div>
            </div>
          </>
        ) : (
          <Unavailable what="CPU" />
        )}
      </CardContent>
    </Card>
  );
}

export function NodeGpuPanel({
  resources,
  nodeLabel,
}: {
  resources: NodeResources | null;
  nodeLabel: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="h-5 w-5" />
          GPU
        </CardTitle>
        <CardDescription>{GPU_UTILIZATION_NOTE}</CardDescription>
      </CardHeader>
      <CardContent>
        {resources ? (
          <GpuPanel src={resources} />
        ) : (
          <EmptyState
            description={`${nodeLabel} ${UNAVAILABLE}, so its GPUs cannot be listed. This does not mean it has none.`}
            icon={Sparkles}
            title="Resources unavailable"
          />
        )}
      </CardContent>
    </Card>
  );
}

export function NodeMemoryPanel({ resources }: { resources: NodeResources | null }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MemoryStick className="h-5 w-5" />
          Memory Information
        </CardTitle>
      </CardHeader>
      <CardContent>
        {resources ? (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <Field label="Total Memory" value={formatBytes(resources.memory_total)} />
              <Field
                label="Available Memory"
                value={formatBytes(resources.memory_available)}
              />
            </div>

            <div className="border-t pt-4">
              <h4 className="mb-3 text-sm font-semibold">Resource Usage</h4>
              <UsageMeter
                label="Used"
                total={resources.memory_total}
                used={resources.memory_used}
                value={`${formatBytes(resources.memory_used)} / ${formatBytes(
                  resources.memory_total,
                )} (${usagePercent(resources.memory_used, resources.memory_total).toFixed(1)}%)`}
              />
            </div>
          </div>
        ) : (
          <Unavailable what="memory" />
        )}
      </CardContent>
    </Card>
  );
}

/**
 * Storage for one registered cluster.
 *
 * important: on a clustered host `storage_total` is USABLE capacity, with a shared backing
 * store counted once however many members report it, while `storage_raw_total` keeps the
 * per-member sum. On the three-member reference cluster those differ by 400 GB, so the panel
 * shows the raw figure whenever it disagrees rather than letting an operator meet the gap in
 * the middle of a capacity decision. Backend: services/lxd/storage_model.py.
 */
export function NodeStoragePanel({
  resources,
  nodeName,
}: {
  resources: NodeResources | null;
  nodeName?: string;
}) {
  const { isPartner } = usePermissions();
  const pools = resources?.storage_pools ?? [];
  const lostParts = resources?.degraded_parts ?? [];
  const storageLost = lostParts.some((part) => part.startsWith("storage"));
  const rawTotal = resources?.storage_raw_total ?? 0;
  const sharedSavings = rawTotal - (resources?.storage_total ?? 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <HardDrive className="h-5 w-5" />
          Storage Information
        </CardTitle>
        {sharedSavings > 0 && (
          <CardDescription>
            Usable capacity. A shared backing store is counted once, not once per member.
          </CardDescription>
        )}
      </CardHeader>
      <CardContent>
        {resources && (pools.length > 0 || storageLost) ? (
          <div className="space-y-4">
            {storageLost && (
              <PartialReadNotice>
                {pools.length > 0
                  ? "One or more pools did not answer, so the totals below are floors rather than measurements."
                  : "The pool list did not come back, so this cluster's storage is unknown. That is not the same as having none."}{" "}
                Refreshing usually clears it.
              </PartialReadNotice>
            )}

            {pools.length > 0 && (
              <>
                <div className="grid grid-cols-2 gap-4 border-b pb-4 sm:grid-cols-3">
                  <Field label="Total Storage" value={formatBytes(resources.storage_total)} />
                  <Field label="Used Storage" value={formatBytes(resources.storage_used)} />
                  <Field
                    label="Available Storage"
                    value={formatBytes(resources.storage_available)}
                  />
                  {sharedSavings > 0 && (
                    <p className="col-span-2 text-sm text-muted-foreground sm:col-span-3">
                      Members report {formatBytes(rawTotal)} between them. The difference of{" "}
                      {formatBytes(sharedSavings)} is shared storage every member can see.
                    </p>
                  )}
                </div>

                <div>
                  <h4 className="mb-3 text-sm font-semibold">Storage Pools</h4>
                  <div className="space-y-4">
                    {pools.map((pool) => (
                      <div
                        className="space-y-3 rounded-lg border p-4"
                        key={`${pool.name}-${pool.driver}`}
                      >
                        <div>
                          <p className="font-semibold">{pool.name}</p>
                          <p className="text-sm text-muted-foreground">
                            Driver: {formatLabel(pool.driver)}
                            {pool.scope === "shared" && " · shared by the members below"}
                          </p>
                          {/* important: member hostnames are operator detail, gated the same
                              way the control-plane endpoint is on the node card. A tenant sees
                              how many machines the pool spans, not what they are called.
                              /cluster-members, the other place these names appear, is
                              require_partner() on the backend.

                              note: a standalone host reports one member, itself, so the line
                              would only repeat the name in the dialog title. */}
                          {pool.members &&
                            pool.members.length > 0 &&
                            !(pool.members.length === 1 && pool.members[0] === nodeName) && (
                            <p className="text-sm text-muted-foreground">
                              {isPartner ? (
                                <>
                                  {pool.members.length === 1 ? "Member" : "Members"}:{" "}
                                  <span className="font-mono">{pool.members.join(", ")}</span>
                                </>
                              ) : pool.members.length === 1 ? (
                                "On one cluster member"
                              ) : (
                                `Across ${pool.members.length} cluster members`
                              )}
                            </p>
                          )}
                        </div>

                        <div className="grid grid-cols-3 gap-2 text-sm">
                          <div>
                            <p className="text-muted-foreground">Total</p>
                            <p className="font-semibold">{formatBytes(pool.space_total)}</p>
                          </div>
                          <div>
                            <p className="text-muted-foreground">Used</p>
                            <p className="font-semibold">{formatBytes(pool.space_used)}</p>
                          </div>
                          <div>
                            <p className="text-muted-foreground">Available</p>
                            <p className="font-semibold">{formatBytes(pool.space_available)}</p>
                          </div>
                        </div>

                        <UsageMeter
                          label="Usage"
                          total={pool.space_total}
                          used={pool.space_used}
                          value={`${usagePercent(pool.space_used, pool.space_total).toFixed(1)}%`}
                        />
                      </div>
                    ))}
                  </div>
                </div>
              </>
            )}
          </div>
        ) : (
          <Unavailable what="storage" />
        )}
      </CardContent>
    </Card>
  );
}
