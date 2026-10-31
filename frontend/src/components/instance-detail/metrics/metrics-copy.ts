import { APIError, type InstanceMetricSection, type VMStatus } from "@/types/api";
import { plural } from "@/lib/utils";

export const METRIC_SECTION_LABELS: Record<InstanceMetricSection, string> = {
  cpu: "vCPU",
  memory: "Memory",
  disk: "Disk",
  network: "Network",
  processes: "Processes",
};

export function formatSectionList(sections: readonly InstanceMetricSection[]): string {
  const labels = sections.map((section) => METRIC_SECTION_LABELS[section]);
  if (labels.length <= 1) return labels[0] ?? "Metrics";
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
  return `${labels.slice(0, -1).join(", ")}, and ${labels.at(-1)}`;
}

export interface MetricsErrorState {
  title: string;
  description: string;
  permissionDenied: boolean;
  staleReason: string;
}

export function describeMetricsError(error: unknown): MetricsErrorState {
  const status = error instanceof APIError ? error.status : null;
  if (status === 401) {
    return {
      title: "Session expired",
      description: "Sign in again before requesting live metrics.",
      permissionDenied: true,
      staleReason: "session expired",
    };
  }
  if (status === 403) {
    return {
      title: "Metrics access denied",
      description: "Your current role cannot view metrics for this instance.",
      permissionDenied: true,
      staleReason: "access denied",
    };
  }
  if (status === 429) {
    const wait = error instanceof APIError && error.retryAfter ? error.retryAfter : 30;
    return {
      title: "Metrics temporarily limited",
      description: `The request budget was reached. Live updates resume on their own in about ${plural(wait, "second")}.`,
      permissionDenied: false,
      staleReason: "request limited",
    };
  }
  if (status === 503 || status === 504 || status === 0) {
    return {
      title: "Cluster did not answer",
      description: "The instance may still be running. Check cluster connectivity and retry.",
      permissionDenied: false,
      staleReason: "cluster did not answer",
    };
  }
  return {
    title: "Metrics could not be refreshed",
    description: "The latest request failed. Retry after checking the cluster status.",
    permissionDenied: false,
    staleReason: "refresh failed",
  };
}

export function notRunningCopy(status: VMStatus): { title: string; description: string } {
  switch (status) {
    case "Stopped":
      return {
        title: "Instance is not running",
        description: "Start the instance to see live vCPU, memory, disk and network figures.",
      };
    case "Frozen":
      return {
        title: "Instance is frozen",
        description:
          "A frozen instance is paused, so it reports no live figures. Unfreeze it to resume them.",
      };
    case "Error":
      return {
        title: "Instance is in an error state",
        description:
          "The cluster reports no live figures for an instance in an error state. Check the instance log.",
      };
    default:
      return {
        title: "Runtime state unknown",
        description: "The cluster did not report a runtime state. Check again in a moment.",
      };
  }
}

export function formatUptime(seconds?: number): string | undefined {
  if (!seconds) return undefined;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}
