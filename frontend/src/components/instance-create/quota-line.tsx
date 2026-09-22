import { AlertTriangle } from "lucide-react";

import { formatLimitBytes, type ProjectLimits } from "@/lib/lxd-limits";

export interface ProjectUsed {
  cpu: number;
  memoryBytes: number;
  instances: number;
}

export interface QuotaCheck {
  /** The request alone is above the limit: a blocker, the cluster will refuse it. */
  cpuExceeds: boolean;
  memoryExceeds: boolean;
  /** The request plus what is already allocated is above the limit: a warning, usage may be stale. */
  cpuOverWithUsage: boolean;
  memoryOverWithUsage: boolean;
}

export function quotaCheck(
  limits: ProjectLimits,
  requestedCpu: number | null,
  requestedMemoryBytes: number | null,
  used: ProjectUsed | null,
): QuotaCheck {
  const cpuExceeds = limits.cpu !== undefined && requestedCpu !== null && requestedCpu > limits.cpu;
  const memoryExceeds =
    limits.memoryBytes !== undefined && requestedMemoryBytes !== null && requestedMemoryBytes > limits.memoryBytes;
  const cpuOverWithUsage =
    !cpuExceeds && !!used && limits.cpu !== undefined && requestedCpu !== null && used.cpu + requestedCpu > limits.cpu;
  const memoryOverWithUsage =
    !memoryExceeds &&
    !!used &&
    limits.memoryBytes !== undefined &&
    requestedMemoryBytes !== null &&
    used.memoryBytes + requestedMemoryBytes > limits.memoryBytes;
  return { cpuExceeds, memoryExceeds, cpuOverWithUsage, memoryOverWithUsage };
}

interface ProjectQuotaLineProps {
  id: string;
  project: string;
  limits: ProjectLimits;
  used: ProjectUsed | null;
  check: QuotaCheck;
}

/**
 * The line under the resources that says what the project allows and how much of it is
 * already allocated. Rendered only when the project sets a limit. A request that would take
 * the project past a limit is a warning, not a blocker: usage may be stale by the time the
 * operator submits, and the cluster's own refusal is what decides.
 */
export function ProjectQuotaLine({ id, project, limits, used, check }: ProjectQuotaLineProps) {
  const parts: string[] = [];
  if (limits.cpu !== undefined) {
    parts.push(used ? `${used.cpu} of ${limits.cpu} vCPU` : `${limits.cpu} vCPU`);
  }
  if (limits.memoryBytes !== undefined) {
    parts.push(
      used
        ? `${formatLimitBytes(used.memoryBytes)} of ${formatLimitBytes(limits.memoryBytes)} memory`
        : `${formatLimitBytes(limits.memoryBytes)} memory`,
    );
  }
  if (limits.instances !== undefined) {
    parts.push(used ? `${used.instances} of ${limits.instances} instances` : `${limits.instances} instances`);
  }
  if (parts.length === 0) return null;
  const summary = used
    ? `Project ${project}: ${parts.join(", ")} allocated.`
    : `Project ${project} allows ${parts.join(", ")} across its instances.`;
  const warning = check.cpuOverWithUsage || check.memoryOverWithUsage;
  return (
    <div id={id} className="space-y-1 text-xs">
      <p role="status" className="text-muted-foreground">
        {summary}
      </p>
      {warning && (
        <p role="status" className="flex items-start gap-2 text-status-pending">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>
            This instance would take {project} past its{" "}
            {[check.cpuOverWithUsage && "vCPU", check.memoryOverWithUsage && "memory"].filter(Boolean).join(" and ")}{" "}
            limit. The cluster will refuse the create unless capacity is freed first.
          </span>
        </p>
      )}
    </div>
  );
}
