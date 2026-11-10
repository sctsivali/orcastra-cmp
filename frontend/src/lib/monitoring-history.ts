import { format } from "date-fns";
import type { MetricsHistoryPoint, NodeMetricsPoint } from "@/types/api";

/**
 * Chart ranges, in seconds of history rather than point counts: the collector writes a point
 * about every 30 s.
 */
export const HISTORY_WINDOWS = [
  { label: "5m", seconds: 300, description: "Last 5 minutes" },
  { label: "15m", seconds: 900, description: "Last 15 minutes" },
  { label: "30m", seconds: 1800, description: "Last 30 minutes" },
  { label: "1h", seconds: 3600, description: "Last 1 hour" },
  { label: "6h", seconds: 21600, description: "Last 6 hours" },
  { label: "24h", seconds: 86400, description: "Last 24 hours" },
] as const;

export const DEFAULT_HISTORY_WINDOW_SECONDS = 300;

export interface ChartPoint {
  time: string;
  cpu: number | null;
  memory: number;
  memoryUsedBytes: number;
  storage: number;
  storageUsedBytes: number;
  networkRxRate: number | null;
  networkTxRate: number | null;
  instances: number;
  [perNode: string]: number | string | null;
}

/** Bytes per second between two legacy points' lifetime counters; null across a reset. */
function legacyRate(
  current: number | undefined,
  previous: number | undefined,
  seconds: number,
): number | null {
  if (current === undefined || previous === undefined || seconds <= 0) return null;
  const delta = current - previous;
  return delta >= 0 ? delta / seconds : null;
}

function rates(
  point: { network_rx_bytes_per_sec?: number | null; network_tx_bytes_per_sec?: number | null; network_rx_bytes?: number; network_tx_bytes?: number },
  previous: typeof point | undefined,
  seconds: number,
): [number | null, number | null] {
  // Newer points carry a network rate; older points carry lifetime counters, which give a rate
  // only between two points that both have them.
  if (point.network_rx_bytes_per_sec !== undefined || point.network_tx_bytes_per_sec !== undefined) {
    return [point.network_rx_bytes_per_sec ?? null, point.network_tx_bytes_per_sec ?? null];
  }
  if (!previous || previous.network_rx_bytes_per_sec !== undefined) return [null, null];
  return [
    legacyRate(point.network_rx_bytes, previous.network_rx_bytes, seconds),
    legacyRate(point.network_tx_bytes, previous.network_tx_bytes, seconds),
  ];
}

/**
 * History points as chart rows, for the whole fleet or one Cluster.
 *
 * A point with no CPU reading stays null so the chart draws a gap rather than a dive to zero.
 */
export function toChartPoints(
  history: readonly MetricsHistoryPoint[] | undefined,
  selectedNode: string,
  totals: { memoryBytes: number; storageBytes: number },
  windowSeconds: number,
): ChartPoint[] {
  if (!history) return [];
  const timeFormat = windowSeconds > 3600 ? "HH:mm" : "HH:mm:ss";
  return history.map((point, index) => {
    const previous = index > 0 ? history[index - 1] : undefined;
    const seconds = previous
      ? (new Date(point.timestamp).getTime() - new Date(previous.timestamp).getTime()) / 1000
      : 0;
    const time = format(new Date(point.timestamp), timeFormat);

    if (selectedNode !== "all") {
      const node: NodeMetricsPoint | undefined = point.nodes?.[selectedNode];
      const previousNode = previous?.nodes?.[selectedNode];
      const memory = node?.memory_usage_percent ?? 0;
      const storage = node?.storage_usage_percent ?? 0;
      const [rx, tx] = node ? rates(node, previousNode, seconds) : [null, null];
      return {
        time,
        cpu: node?.cpu_usage_percent ?? null,
        memory,
        memoryUsedBytes: (memory / 100) * totals.memoryBytes,
        storage,
        storageUsedBytes: (storage / 100) * totals.storageBytes,
        networkRxRate: rx,
        networkTxRate: tx,
        instances: node?.running_instances ?? 0,
      };
    }

    const perNode: Record<string, number | null> = {};
    for (const [name, node] of Object.entries(point.nodes ?? {})) {
      perNode[`cpu_${name}`] = node.cpu_usage_percent ?? null;
      perNode[`memory_${name}`] = node.memory_usage_percent ?? 0;
      perNode[`storage_${name}`] = node.storage_usage_percent ?? 0;
    }
    const [rx, tx] = rates(point, previous, seconds);
    const storage = point.storage_usage_percent ?? 0;
    return {
      time,
      cpu: point.cpu_usage_percent ?? null,
      memory: point.memory_usage_percent,
      memoryUsedBytes: (point.memory_usage_percent / 100) * totals.memoryBytes,
      storage,
      storageUsedBytes: (storage / 100) * totals.storageBytes,
      networkRxRate: rx,
      networkTxRate: tx,
      instances: point.running_instances,
      ...perNode,
    };
  });
}

/**
 * When a dashboard payload was collected, from its own timestamp; 0 when it carries none.
 *
 * The browser's receive time would mislead: the dashboard is served from a 35 s cache rebuilt
 * by a collector, so a body received just now can be half a minute old.
 */
export function collectedAtOf(data?: { timestamp?: string | null } | null): number {
  const parsed = data?.timestamp ? Date.parse(data.timestamp) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : 0;
}
