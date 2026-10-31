"use client";

import * as React from "react";
import { Cpu, Database, HardDrive, Sparkles, type LucideIcon } from "lucide-react";
import { EMPTY_VALUE } from "@/components/ui/table-text";
import { formatBytes } from "@/lib/utils";
import type { NodeStatus } from "@/types/api";
import { HardwareDetailDialog } from "./hardware-detail-dialog";
import {
  TechnologySummaryCard,
  type TechnologySummaryItem,
} from "./technology-summary-card";

/**
 * The fleet-wide hardware totals.
 *
 * Every figure is reduced from node.resources. Missing fleet readings stay missing rather than
 * becoming zero capacity, and partial totals state exactly how many nodes contributed.
 */

export interface HardwareCategory {
  id: "cpu" | "memory" | "storage" | "gpu";
  icon: LucideIcon;
  title: string;
  description: string;
  items: TechnologySummaryItem[];
}

export function buildHardwareCategories(nodes: NodeStatus[]): HardwareCategory[] | null {
  if (!nodes || nodes.length === 0) return null;

  const reportingNodes = nodes.filter((node) => node.resources);
  const reportingNodeCount = reportingNodes.length;
  const coverageDetail =
    reportingNodeCount === 0
      ? "No cluster reported this value"
      : reportingNodeCount < nodes.length
        ? `Reported by ${reportingNodeCount} of ${nodes.length} clusters`
        : "Aggregate across all reporting clusters";

  const totalThreads = reportingNodes.reduce((sum: number, node: NodeStatus) => {
    return sum + (node.resources?.cpu_total || 0);
  }, 0);

  const totalMemory = reportingNodes.reduce((sum: number, node: NodeStatus) => {
    return sum + (node.resources?.memory_total || 0);
  }, 0);

  // Get real architecture from node environment
  const cpuArchitectures = new Set<string>(
    reportingNodes
      .map((node: NodeStatus) => node.environment?.kernel_architecture)
      .filter((arch): arch is string => Boolean(arch))
  );

  // Compute GPUs (sellable capacity) and detected cards are different numbers about the
  // same hardware. Both are derived from /nodes, the endpoint this page already reads: two
  // endpoints for one fact is how the counts came to disagree in the first place.
  const totalGPUs = reportingNodes.reduce((sum: number, node: NodeStatus) => {
    return sum + (node.resources?.gpu_total || 0);
  }, 0);
  const totalGpuCards = reportingNodes.reduce((sum: number, node: NodeStatus) => {
    return sum + (node.resources?.gpu_cards_total ?? node.resources?.gpu_cards?.length ?? 0);
  }, 0);
  const gpuVendors = Array.from(
    new Set(
      reportingNodes.flatMap((node: NodeStatus) =>
        (node.resources?.gpu_cards ?? [])
          .filter((card) => card.is_compute)
          .map((card) => card.vendor)
          .filter((vendor): vendor is string => !!vendor)
      )
    )
  );
  const gpuModels = Array.from(
    new Set(
      reportingNodes.flatMap((node: NodeStatus) =>
        (node.resources?.gpu_cards ?? [])
          .filter((card) => card.is_compute)
          .map((card) => card.model_name || card.product)
          .filter((model): model is string => !!model)
      )
    )
  );

  const storageBackends = new Set<string>(
    reportingNodes.flatMap((node: NodeStatus) =>
      node.resources?.storage_pools
        ? node.resources.storage_pools.map((pool) => pool.driver)
        : []
    )
  );

  const categories: HardwareCategory[] = [
    {
      id: "cpu",
      icon: Cpu,
      title: "CPU Resources",
      description: "Processor information across all clusters",
      items: [
        {
          label: "Hardware Threads",
          value: reportingNodeCount > 0 ? totalThreads.toString() : EMPTY_VALUE,
          detail:
            reportingNodeCount > 0
              ? `${coverageDetail}. LXD reports this capacity as hardware threads`
              : coverageDetail,
        },
        {
          label: "Architectures",
          value: cpuArchitectures.size > 0 ? Array.from(cpuArchitectures).join(", ") : EMPTY_VALUE,
          detail: "Supported CPU architectures",
        },
        {
          label: "Reporting Clusters",
          value: `${reportingNodeCount} of ${nodes.length}`,
          detail: "Clusters included in these totals",
        },
      ],
    },
    {
      id: "memory",
      icon: HardDrive,
      title: "Memory",
      description: "Memory resources available across the fleet",
      items: [
        {
          label: "Total Memory",
          value:
            reportingNodeCount > 0
              ? formatBytes(totalMemory)
              : EMPTY_VALUE,
          detail: coverageDetail,
        },
        {
          label: "Memory per Cluster",
          value:
            reportingNodeCount > 0
              ? `~${formatBytes(totalMemory / reportingNodeCount)}`
              : EMPTY_VALUE,
          detail:
            reportingNodeCount > 0
              ? `Average across ${reportingNodeCount} reporting cluster${reportingNodeCount === 1 ? "" : "s"}`
              : "No cluster reported memory",
        },
      ],
    },
    {
      id: "storage",
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
          value:
            reportingNodeCount > 0
              ? reportingNodes
                  .reduce(
                    (sum: number, node: NodeStatus) =>
                      sum + (node.resources?.storage_pools?.length || 0),
                    0,
                  )
                  .toString()
              : EMPTY_VALUE,
          detail: coverageDetail,
        },
      ],
    },
  ];

  categories.push({
    id: "gpu",
    icon: Sparkles,
    title: "GPU Resources",
    description: "Graphics processing units for acceleration",
    items: [
      {
        label: "Compute GPUs",
        value: reportingNodeCount > 0 ? totalGPUs.toString() : EMPTY_VALUE,
        detail: coverageDetail,
      },
      {
        label: "Cards Detected",
        value: reportingNodeCount > 0 ? totalGpuCards.toString() : EMPTY_VALUE,
        detail:
          reportingNodeCount > 0
            ? `Includes display controllers; ${coverageDetail.toLowerCase()}`
            : coverageDetail,
      },
      {
        label: "Vendors",
        value: gpuVendors.length > 0 ? gpuVendors.join(", ") : EMPTY_VALUE,
        detail: "Reported by the clusters",
      },
      {
        label: "Models",
        value: gpuModels.length > 0 ? gpuModels.join(", ") : EMPTY_VALUE,
        detail: "Reported by the clusters",
      },
    ],
  });

  return categories;
}

export function HardwareAggregate({
  categories,
  nodes,
  isRefreshing = false,
  refreshError = false,
}: {
  categories: HardwareCategory[];
  nodes: NodeStatus[];
  isRefreshing?: boolean;
  refreshError?: boolean;
}) {
  const [selectedId, setSelectedId] = React.useState<HardwareCategory["id"] | null>(null);
  const selected = categories.find((category) => category.id === selectedId) ?? null;
  const reportingNodeCount = nodes.filter((node) => node.resources).length;

  return (
    <>
      <section className="space-y-3">
        <div>
          <h2 className="text-xl font-semibold">Fleet Totals</h2>
          {/* note: --foreground, not --muted-foreground. On --background the muted token measures
              4.27:1 where AA wants 4.5:1, which is why the page subtitle is a declared exception
              in the contrast sweep. One exception is enough. */}
          <p className="text-sm text-foreground">
            {reportingNodeCount === nodes.length
              ? `Summed across all ${nodes.length} reporting cluster${nodes.length === 1 ? "" : "s"}.`
              : `Partial totals from ${reportingNodeCount} of ${nodes.length} clusters.`}{" "}
            Open a category for cluster detail.
          </p>
        </div>
        <div className="grid grid-cols-1 items-start gap-6 md:grid-cols-2">
          {categories.map((category) => (
            <TechnologySummaryCard
              key={category.id}
              icon={category.icon}
              title={category.title}
              description={category.description}
              items={category.items}
              onOpen={() => setSelectedId(category.id)}
            />
          ))}
        </div>
      </section>

      <HardwareDetailDialog
        category={selected}
        nodes={nodes}
        open={selected !== null}
        isRefreshing={isRefreshing}
        refreshError={refreshError}
        onOpenChange={(open) => {
          if (!open) setSelectedId(null);
        }}
      />
    </>
  );
}
