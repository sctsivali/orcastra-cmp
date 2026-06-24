"use client";

import * as React from "react";
import { Cpu, HardDrive, Database, Monitor, Info } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { formatBytes, formatLabel } from "@/lib/utils";
import type { NodeStatus } from "@/types/api";

interface HardwareCategory {
  icon: React.ElementType;
  title: string;
  description: string;
  items: { label: string; value: string; detail?: string }[];
}

// Cluster-wide hardware, read live from /nodes. Every value is real - no fabricated specs.
// Per-node specifics (CPU model, disks, NICs, firmware) live in the Node Detail section below.
export function HardwareSummary({ nodes }: { nodes: NodeStatus[] }) {
  const categories = React.useMemo<HardwareCategory[]>(() => {
    const connected = nodes.filter((n) => n.connected && n.resources);

    const totalCores = connected.reduce((s, n) => s + (n.resources?.cpu_total || 0), 0);
    const totalMemory = connected.reduce((s, n) => s + (n.resources?.memory_total || 0), 0);
    const usedMemory = connected.reduce((s, n) => s + (n.resources?.memory_used || 0), 0);
    const availMemory = connected.reduce((s, n) => s + (n.resources?.memory_available || 0), 0);
    const totalStorage = connected.reduce((s, n) => s + (n.resources?.storage_total || 0), 0);
    const availStorage = connected.reduce((s, n) => s + (n.resources?.storage_available || 0), 0);
    const totalGPUs = connected.reduce((s, n) => s + (n.resources?.gpu_total || 0), 0);

    const architectures = Array.from(
      new Set(
        connected
          .map((n) => n.environment?.kernel_architecture)
          .filter((a): a is string => Boolean(a))
      )
    );
    const storageBackends = Array.from(
      new Set(connected.flatMap((n) => n.resources?.storage_pools?.map((p) => p.driver) || []))
    );
    const storagePools = connected.reduce(
      (s, n) => s + (n.resources?.storage_pools?.length || 0),
      0
    );
    const gpuModels = Array.from(
      new Set(
        connected.flatMap((n) =>
          (n.resources?.gpu_cards || [])
            .filter((c) => c.is_compute)
            .map((c) => c.model_name || c.product || c.vendor || "")
            .filter(Boolean)
        )
      )
    );

    const cats: HardwareCategory[] = [
      {
        icon: Cpu,
        title: "CPU Resources",
        description: "Processor information across all nodes",
        items: [
          { label: "Total Cores", value: totalCores.toString(), detail: "Aggregate across all nodes" },
          ...(architectures.length > 0
            ? [{ label: "Architectures", value: architectures.join(", "), detail: "Detected CPU architectures" }]
            : []),
          { label: "Active Nodes", value: connected.length.toString(), detail: "Connected compute nodes" },
        ],
      },
      {
        icon: HardDrive,
        title: "Memory",
        description: "RAM resources available in the cluster",
        items: [
          { label: "Total Memory", value: totalMemory ? formatBytes(totalMemory) : "—", detail: "Aggregate across all nodes" },
          ...(usedMemory ? [{ label: "Used", value: formatBytes(usedMemory), detail: "Currently allocated" }] : []),
          ...(availMemory ? [{ label: "Available", value: formatBytes(availMemory), detail: "Free for new workloads" }] : []),
        ],
      },
      {
        icon: Database,
        title: "Storage",
        description: "Storage systems and backends",
        items: [
          ...(storageBackends.length > 0
            ? [{ label: "Storage Backends", value: storageBackends.map(formatLabel).join(", "), detail: "Detected pool drivers" }]
            : []),
          { label: "Storage Pools", value: storagePools.toString(), detail: "Total configured storage pools" },
          ...(totalStorage ? [{ label: "Total Capacity", value: formatBytes(totalStorage), detail: "Aggregate across all pools" }] : []),
          ...(availStorage ? [{ label: "Available", value: formatBytes(availStorage), detail: "Free pool capacity" }] : []),
        ],
      },
    ];

    if (totalGPUs > 0) {
      cats.push({
        icon: Monitor,
        title: "GPU Resources",
        description: "Graphics processing units for acceleration",
        items: [
          { label: "Total GPUs", value: totalGPUs.toString(), detail: "Compute-capable, aggregate" },
          ...(gpuModels.length > 0
            ? [{ label: "GPU Models", value: gpuModels.join(", "), detail: "Detected from connected nodes" }]
            : []),
        ],
      });
    }

    return cats;
  }, [nodes]);

  return (
    <div className="space-y-6">
      <Card className="bg-muted/50">
        <CardContent className="p-6">
          <div className="flex items-start gap-3">
            <Info className="h-5 w-5 text-blue-500 mt-0.5 shrink-0" />
            <p className="text-sm text-muted-foreground">
              Aggregated live from every connected node. Expand a node under Node Detail for its full
              hardware (CPU topology, memory, storage devices, network interfaces, firmware).
            </p>
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {categories.map((category) => {
          const Icon = category.icon;
          return (
            <Card key={category.title} className="hover:shadow-md transition-all duration-200">
              <CardHeader>
                <div className="flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-foreground text-background">
                    <Icon className="h-5 w-5" />
                  </div>
                  <div>
                    <CardTitle>{category.title}</CardTitle>
                    <CardDescription>{category.description}</CardDescription>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                {category.items.map((item) => (
                  <div
                    key={item.label}
                    className="flex flex-col gap-2 pb-4 mb-4 border-b last:border-b-0 last:pb-0 last:mb-0"
                  >
                    <div className="flex justify-between items-start gap-4">
                      <span className="text-sm font-medium text-muted-foreground flex-shrink-0">
                        {item.label}
                      </span>
                      <span className="text-sm font-semibold text-right break-words">{item.value}</span>
                    </div>
                    {item.detail && (
                      <span className="text-xs text-muted-foreground mt-1">{item.detail}</span>
                    )}
                  </div>
                ))}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
