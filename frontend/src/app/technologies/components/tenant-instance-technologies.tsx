"use client";

import { formatLxdSize } from "@/lib/lxd-limits";
import * as React from "react";
import {
  Cpu,
  Database,
  HardDrive,
  Loader2,
  Server,
  Sparkles,
  type LucideIcon,
} from "lucide-react";

import { DetailField, DetailGrid } from "@/components/detail/detail-field";
import { Badge } from "@/components/ui/badge";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { LiveTimestamp } from "@/components/ui/live-timestamp";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { PANEL_PAGE_SIZE } from "@/lib/pagination";
import { STALE_AFTER_MS } from "@/lib/query-config";
import { instanceStatusDisplay } from "@/lib/instance-status";
import { formatInstanceType } from "@/lib/utils";
import type {
  InstanceResourceSpecification,
  InstanceRootDiskSource,
  InstanceSpecification,
  InstanceSpecificationsResponse,
  InstanceSpecificationSource,
} from "@/types/api";
import {
  TechnologySummaryCard,
  type TechnologySummaryItem,
} from "./technology-summary-card";

type TenantCategoryId = "cpu" | "memory" | "storage" | "gpu";

interface TenantCategory {
  id: TenantCategoryId;
  icon: LucideIcon;
  title: string;
  description: string;
  items: TechnologySummaryItem[];
}

const RESOURCE_SOURCE_LABEL: Record<InstanceSpecificationSource, string> = {
  explicit: "Explicit",
  inherited: "Inherited",
  default: "LXD default",
  unlimited: "Unlimited",
  unknown: "Unknown",
};

const ROOT_SOURCE_LABEL: Record<InstanceRootDiskSource, string> = {
  explicit: "Explicit",
  inherited: "Inherited",
  pool_default: "Pool default",
  unknown: "Unknown",
};

function countResourceSource(
  instances: readonly InstanceSpecification[],
  field: "cpu" | "memory",
  source: InstanceSpecificationSource,
) {
  return instances.filter((instance) => instance[field].source === source).length;
}

function buildTenantCategories(instances: readonly InstanceSpecification[]): TenantCategory[] {
  const pools = Array.from(
    new Set(instances.map((instance) => instance.root_disk.pool).filter((pool): pool is string => !!pool)),
  );
  const gpuCount = instances.reduce((sum, instance) => sum + instance.gpu_count, 0);
  const gpuInstances = instances.filter((instance) => instance.gpu_count > 0).length;

  const limitItems = (field: "cpu" | "memory"): TechnologySummaryItem[] => [
    {
      label: "Instances",
      value: instances.length.toString(),
      detail: "Currently visible to your account",
    },
    {
      label: "Explicit Limits",
      value: countResourceSource(instances, field, "explicit").toString(),
      detail: "Set directly on the instance",
    },
    {
      label: "Inherited",
      value: countResourceSource(instances, field, "inherited").toString(),
      detail: "Applied through profiles",
    },
    {
      label: "LXD Defaults",
      value: countResourceSource(instances, field, "default").toString(),
      detail: "The daemon default for this instance type",
    },
    {
      label: "Unlimited",
      value: countResourceSource(instances, field, "unlimited").toString(),
      detail: "LXD reports no limit for this resource",
    },
    {
      label: "Unknown",
      value: countResourceSource(instances, field, "unknown").toString(),
      detail: "The effective limit could not be determined",
    },
  ];

  return [
    {
      id: "cpu",
      icon: Cpu,
      title: "vCPU",
      description: "vCPU limits applied to your instances",
      items: limitItems("cpu"),
    },
    {
      id: "memory",
      icon: HardDrive,
      title: "Memory",
      description: "Memory limits applied to your instances",
      items: limitItems("memory"),
    },
    {
      id: "storage",
      icon: Database,
      title: "Storage",
      description: "Root disk sizes and pools used by your instances",
      items: [
        {
          label: "Instances",
          value: instances.length.toString(),
          detail: "Current root disk configurations",
        },
        {
          label: "Explicit Sizes",
          value: instances.filter((instance) => instance.root_disk.source === "explicit").length.toString(),
          detail: "Set directly on the instance",
        },
        {
          label: "Pool Defaults",
          value: instances.filter((instance) => instance.root_disk.source === "pool_default").length.toString(),
          detail: "Capacity follows the storage pool",
        },
        {
          label: "Inherited Sizes",
          value: instances.filter((instance) => instance.root_disk.source === "inherited").length.toString(),
          detail: "Applied through a profile",
        },
        {
          label: "Unknown Sizes",
          value: instances.filter((instance) => instance.root_disk.source === "unknown").length.toString(),
          detail: "The effective size could not be determined",
        },
        {
          label: "Pools",
          value: pools.length > 0 ? pools.join(", ") : "Not reported",
          detail: "Pools used by visible instances",
        },
      ],
    },
    {
      id: "gpu",
      icon: Sparkles,
      title: "GPU",
      description: "GPU devices attached to your instances",
      items: [
        {
          label: "Attached GPUs",
          value: gpuCount.toString(),
          detail: "Devices currently declared in instance configuration",
        },
        {
          label: "Instances with GPUs",
          value: gpuInstances.toString(),
          detail: `Out of ${instances.length} visible instances`,
        },
      ],
    },
  ];
}

function resourceValue(resource: InstanceResourceSpecification, format: (raw: string) => string = String) {
  if (resource.value) return format(resource.value);
  if (resource.source === "explicit") return "Explicit value not reported";
  if (resource.source === "inherited") return "Inherited value not reported";
  if (resource.source === "default") return "LXD default";
  if (resource.source === "unlimited") return "Unlimited";
  return "Unknown";
}

function rootDiskValue(instance: InstanceSpecification) {
  if (instance.root_disk.value) return formatLxdSize(instance.root_disk.value);
  if (instance.root_disk.source === "explicit") return "Explicit size not reported";
  if (instance.root_disk.source === "inherited") return "Inherited size not reported";
  if (instance.root_disk.source === "pool_default") return "Pool default";
  return "Unknown";
}

function InstanceCategoryFields({
  category,
  instance,
}: {
  category: TenantCategoryId;
  instance: InstanceSpecification;
}) {
  if (category === "cpu") {
    return (
      <DetailGrid cols={3}>
        <DetailField label="vCPU Limit" value={resourceValue(instance.cpu)} mono />
        <DetailField label="Limit Source" value={RESOURCE_SOURCE_LABEL[instance.cpu.source]} />
        <DetailField label="Architecture" value={instance.architecture || "Not reported"} mono />
      </DetailGrid>
    );
  }

  if (category === "memory") {
    return (
      <DetailGrid cols={3}>
        <DetailField label="Memory Limit" value={resourceValue(instance.memory, formatLxdSize)} mono />
        <DetailField label="Limit Source" value={RESOURCE_SOURCE_LABEL[instance.memory.source]} />
        <DetailField label="Architecture" value={instance.architecture || "Not reported"} mono />
      </DetailGrid>
    );
  }

  if (category === "storage") {
    return (
      <DetailGrid cols={3}>
        <DetailField label="Root Disk Size" value={rootDiskValue(instance)} mono />
        <DetailField label="Storage Pool" value={instance.root_disk.pool || "Not reported"} />
        <DetailField label="Size Source" value={ROOT_SOURCE_LABEL[instance.root_disk.source]} />
      </DetailGrid>
    );
  }

  return (
    <DetailGrid cols={2}>
      <DetailField label="Attached GPUs" value={instance.gpu_count} />
      <DetailField
        label="Devices"
        value={
          instance.gpu_devices.length > 0 ? (
            <span className="break-words font-mono">{instance.gpu_devices.join(", ")}</span>
          ) : (
            "None attached"
          )
        }
      />
    </DetailGrid>
  );
}

function TenantCategoryDialog({
  category,
  instances,
  open,
  isRefreshing,
  refreshError,
  unreachableNodes,
  unreachableReasons,
  onOpenChange,
}: {
  category: TenantCategory | null;
  instances: readonly InstanceSpecification[];
  open: boolean;
  isRefreshing: boolean;
  refreshError: boolean;
  unreachableNodes: string[];
  unreachableReasons?: Record<string, string>;
  onOpenChange: (open: boolean) => void;
}) {
  const [page, setPage] = React.useState(1);
  const ordered = React.useMemo(
    () =>
      [...instances].sort(
        (a, b) =>
          a.name.localeCompare(b.name) ||
          a.project.localeCompare(b.project) ||
          a.host.localeCompare(b.host),
      ),
    [instances],
  );
  const totalPages = Math.max(1, Math.ceil(ordered.length / PANEL_PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const paged = ordered.slice(
    (currentPage - 1) * PANEL_PAGE_SIZE,
    currentPage * PANEL_PAGE_SIZE,
  );

  React.useEffect(() => {
    if (open) setPage(1);
  }, [open, category?.id]);

  const Icon = category?.icon ?? Server;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] max-w-4xl overflow-hidden flex flex-col">
        <DialogHeader className="pr-8 pb-4">
          <DialogTitle className="flex items-center gap-2">
            <Icon className="h-5 w-5 shrink-0" aria-hidden="true" />
            {category ? `${category.title} by Instance` : "Instance specifications"}
          </DialogTitle>
          <DialogDescription>
            Current LXD configuration for every instance your account can access. Missing values
            are not replaced with host capacity.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-3 pr-2">
          {isRefreshing ? (
            <div
              className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground"
              role="status"
            >
              <Loader2
                className="h-4 w-4 animate-spin motion-reduce:animate-none"
                aria-hidden="true"
              />
              Refreshing instance specifications…
            </div>
          ) : null}
          <UnreachableNodesBanner
            nodes={unreachableNodes}
            reasons={unreachableReasons}
            noun={`${category?.title ?? "instance"} specifications`}
          />
          {refreshError ? (
            <PartialReadNotice>
              The latest refresh failed. These are the last successful specifications.
            </PartialReadNotice>
          ) : null}
          {category && paged.length > 0 ? (
            paged.map((instance) => {
              const status = instanceStatusDisplay(instance.status);
              return (
                <section
                  key={`${instance.host}:${instance.project}:${instance.name}`}
                  className="space-y-4 rounded-lg border p-4"
                  aria-label={`${instance.name} ${category.title} specification`}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="break-words text-sm font-semibold">{instance.name}</h3>
                      <p className="break-words text-xs text-muted-foreground">
                        {instance.host} · {instance.project}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge variant="outline" className="font-normal">
                        {formatInstanceType(instance.type)}
                      </Badge>
                      <Badge variant={status.tone}>{status.label}</Badge>
                    </div>
                  </div>
                  <InstanceCategoryFields category={category.id} instance={instance} />
                </section>
              );
            })
          ) : (
            <EmptyState
              icon={Server}
              title="No instance specifications available"
              description="Specifications appear here after an instance is assigned to your account."
            />
          )}
        </DialogBody>

        {totalPages > 1 ? (
          <DataTablePagination
            variant="compact"
            label={`${category?.title ?? "instance"} specifications`}
            currentPage={currentPage}
            totalPages={totalPages}
            onPageChange={setPage}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

export function TenantInstanceTechnologies({
  response,
  isRefreshing = false,
  refreshError = false,
}: {
  response: InstanceSpecificationsResponse;
  isRefreshing?: boolean;
  refreshError?: boolean;
}) {
  const [selectedId, setSelectedId] = React.useState<TenantCategoryId | null>(null);
  const categories = React.useMemo(
    () => buildTenantCategories(response.instances),
    [response.instances],
  );
  const selected = categories.find((category) => category.id === selectedId) ?? null;
  const collectedAt = Date.parse(response.collected_at);

  return (
    <>
      <section className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-xl font-semibold">Your Instances</h2>
            <p className="text-sm text-foreground">
              Current limits and attached resources for the instances your account can access.
            </p>
          </div>
          <LiveTimestamp
            updatedAt={Number.isNaN(collectedAt) ? null : collectedAt}
            staleAfterMs={STALE_AFTER_MS.active}
          />
        </div>

        <UnreachableNodesBanner
          nodes={response.unreachable_nodes}
          reasons={response.node_errors}
          noun="instance specifications"
        />

        {response.instances.length > 0 ? (
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
        ) : (
          <div className="rounded-xl border bg-card px-6">
            <EmptyState
              icon={Server}
              title="No instances assigned"
              description={
                response.unreachable_nodes.length > 0
                  ? "Some instance specifications could not be read. The list may be incomplete."
                  : "This page will show specifications after an instance is assigned to your account."
              }
            />
          </div>
        )}
      </section>

      <TenantCategoryDialog
        category={selected}
        instances={response.instances}
        open={selected !== null}
        isRefreshing={isRefreshing}
        refreshError={refreshError}
        unreachableNodes={response.unreachable_nodes}
        unreachableReasons={response.node_errors}
        onOpenChange={(open) => {
          if (!open) setSelectedId(null);
        }}
      />
    </>
  );
}
