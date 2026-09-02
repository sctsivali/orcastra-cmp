"use client";

import * as React from "react";
import { Cpu, Database, HardDrive, Network, Sparkles } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EMPTY_VALUE } from "@/components/ui/table-text";
import type { NodeStatus } from "@/types/api";

/**
 * The fleet-wide hardware totals.
 *
 * Moved out of page.tsx unchanged. Every figure is reduced from node.resources, and the two
 * fallbacks that used to invent a value when nothing was reported now resolve to the shared
 * placeholder instead.
 */

export interface HardwareCategory {
  icon: React.ElementType;
  title: string;
  description: string;
  items: { label: string; value: string; detail?: string }[];
}

export function buildHardwareCategories(nodes: NodeStatus[]): HardwareCategory[] | null {

  if (!nodes || nodes.length === 0) return null;

  const connectedNodes = nodes.filter(n => n.connected);

  const totalCores = nodes.reduce((sum: number, node: NodeStatus) => {
    return sum + (node.resources?.cpu_total || 0);
  }, 0);

  const totalMemory = nodes.reduce((sum: number, node: NodeStatus) => {
    return sum + (node.resources?.memory_total || 0);
  }, 0);

  // Get real architecture from node environment
  const cpuArchitectures = new Set<string>(
    nodes
      .map((node: NodeStatus) => node.environment?.kernel_architecture)
      .filter((arch): arch is string => Boolean(arch))
  );

  // Get total storage capacity
  const totalStorageCapacity = nodes.reduce((sum: number, node: NodeStatus) => {
    return sum + (node.resources?.storage_total || 0);
  }, 0);

  const totalStorageAvailable = nodes.reduce((sum: number, node: NodeStatus) => {
    return sum + (node.resources?.storage_available || 0);
  }, 0);

  // Compute GPUs (sellable capacity) and detected cards are different numbers about the
  // same hardware. Both are derived from /nodes, the endpoint this page already reads: two
  // endpoints for one fact is how the counts came to disagree in the first place.
  const totalGPUs = nodes.reduce((sum: number, node: NodeStatus) => {
    return sum + (node.resources?.gpu_total || 0);
  }, 0);
  const totalGpuCards = nodes.reduce((sum: number, node: NodeStatus) => {
    return sum + (node.resources?.gpu_cards_total ?? node.resources?.gpu_cards?.length ?? 0);
  }, 0);
  const gpuVendors = Array.from(
    new Set(
      nodes.flatMap((node: NodeStatus) =>
        (node.resources?.gpu_cards ?? [])
          .filter((card) => card.is_compute)
          .map((card) => card.vendor)
          .filter((vendor): vendor is string => !!vendor)
      )
    )
  );
  const gpuModels = Array.from(
    new Set(
      nodes.flatMap((node: NodeStatus) =>
        (node.resources?.gpu_cards ?? [])
          .filter((card) => card.is_compute)
          .map((card) => card.model_name || card.product)
          .filter((model): model is string => !!model)
      )
    )
  );

  const storageBackends = new Set<string>(
    nodes.flatMap((node: NodeStatus) =>
      node.resources?.storage_pools
        ? node.resources.storage_pools.map((pool) => pool.driver)
        : []
    )
  );

  const categories: HardwareCategory[] = [
    {
      icon: Cpu,
      title: "CPU Resources",
      description: "Processor information across all nodes",
      items: [
        {
          label: "Total Cores",
          value: totalCores.toString(),
          detail: "Aggregate across all nodes",
        },
        {
          label: "Architectures",
          value: cpuArchitectures.size > 0 ? Array.from(cpuArchitectures).join(", ") : EMPTY_VALUE,
          detail: "Supported CPU architectures",
        },
        {
          label: "Nodes",
          value: connectedNodes.length.toString(),
          detail: "Active compute nodes",
        },
      ],
    },
    {
      icon: HardDrive,
      title: "Memory",
      description: "RAM resources available in the cluster",
      items: [
        {
          label: "Total Memory",
          value: `${(totalMemory / (1024 ** 3)).toFixed(2)} GiB`,
          detail: "Aggregate across all nodes",
        },
        {
          label: "Memory per Node",
          value: connectedNodes.length > 0 ? `~${(totalMemory / connectedNodes.length / (1024 ** 3)).toFixed(2)} GiB` : EMPTY_VALUE,
          detail: "Average memory allocation",
        },
      ],
    },
    {
      icon: Database,
      title: "Storage",
      description: "Storage systems and backends",
      items: [
        {
          label: "Storage Backends",
          value: storageBackends.size > 0 ? Array.from(storageBackends).join(", ") : EMPTY_VALUE,
          detail: "Available storage types",
        },
        {
          label: "Storage Pools",
          value: nodes
            .reduce(
              (sum: number, node: NodeStatus) =>
                sum + (node.resources?.storage_pools?.length || 0),
              0
            )
            .toString(),
          detail: "Total configured storage pools",
        },
      ],
    },
  ];

  categories.push({
    icon: Sparkles,
    title: "GPU Resources",
    description: "Graphics processing units for acceleration",
    items: [
      {
        label: "Compute GPUs",
        value: totalGPUs.toString(),
        detail: "Allocatable across all nodes",
      },
      {
        label: "Cards Detected",
        value: totalGpuCards.toString(),
        detail: "Includes display controllers",
      },
      {
        label: "Vendors",
        value: gpuVendors.length > 0 ? gpuVendors.join(", ") : EMPTY_VALUE,
        detail: "Reported by the nodes",
      },
      {
        label: "Models",
        value: gpuModels.length > 0 ? gpuModels.join(", ") : EMPTY_VALUE,
        detail: "Reported by the nodes",
      },
    ],
  });

  return categories;
}

export function HardwareAggregate({ categories }: { categories: HardwareCategory[] }) {
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-xl font-semibold">Fleet Totals</h2>
        {/* note: --foreground, not --muted-foreground. On --background the muted token measures
            4.27:1 where AA wants 4.5:1, which is why the page subtitle is a declared exception
            in the contrast sweep. One exception is enough. */}
        <p className="text-sm text-foreground">
          Summed across every node the dashboard can reach.
        </p>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-start">
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
                      <span className="text-sm font-semibold text-right break-words">
                        {item.value}
                      </span>
                    </div>
                    {item.detail && (
                      <span className="text-xs text-muted-foreground mt-1">
                        {item.detail}
                      </span>
                    )}
                  </div>
                ))}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </section>
  );
}
