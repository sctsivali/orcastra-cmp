import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  HelpCircle,
  ShieldAlert,
  XCircle,
  type LucideIcon,
} from "lucide-react";

import {
  ClusterStatus,
  type ClusterMonitoringMetrics,
  type NodeStatus,
} from "@/types/api";

/**
 * One vocabulary for "can this cluster be used right now": label, icon, tone and colour.
 *
 * Every surface resolves a cluster's status here, so the wording and shades agree. Semantic
 * colour comes from the status tokens, never from a raw palette step.
 */

export type ClusterStatusTone = "positive" | "caution" | "neutral" | "negative";

export interface ClusterStatusMeta {
  /** Normalised enum value. Never undefined: unknown input resolves to a real member. */
  status: ClusterStatus;
  /** Display label. Title case, hand-written, safe to render anywhere. */
  label: string;
  icon: LucideIcon;
  /** Foreground utility for an inline icon. */
  iconClassName: string;
  tone: ClusterStatusTone;
  /** Sort weight. Lower sorts first, so reachable clusters lead a picker. */
  toneRank: number;
  /** True only for ONLINE. Never derive this by comparing the label. */
  reachable: boolean;
  /** Announced text, so colour is never the only carrier. */
  srLabel: string;
}

const TONE_RANK: Record<ClusterStatusTone, number> = {
  positive: 0,
  caution: 1,
  neutral: 2,
  negative: 3,
};

/** Foreground utility for an icon toned by a tone rather than by a cluster status. */
export const TONE_TEXT: Record<ClusterStatusTone, string> = {
  positive: "text-status-online",
  caution: "text-status-pending",
  neutral: "text-status-unknown",
  negative: "text-status-offline",
};

type Entry = Omit<ClusterStatusMeta, "srLabel" | "toneRank">;

const ENTRIES: Record<ClusterStatus, Entry> = {
  [ClusterStatus.ONLINE]: {
    status: ClusterStatus.ONLINE,
    label: "Online",
    icon: CheckCircle2,
    iconClassName: "text-status-online",
    tone: "positive",
    reachable: true,
  },
  [ClusterStatus.NOT_AUTHORIZED]: {
    status: ClusterStatus.NOT_AUTHORIZED,
    // Not formatLabel("not_authorized"), which gives "Not Authorized" and reads as a
    // permission denial. The cluster answered; the trust handshake is unfinished.
    label: "Pending Trust",
    icon: ShieldAlert,
    iconClassName: "text-status-pending",
    tone: "caution",
    reachable: false,
  },
  [ClusterStatus.PENDING]: {
    status: ClusterStatus.PENDING,
    label: "Pending",
    icon: Clock,
    iconClassName: "text-status-unknown",
    tone: "neutral",
    reachable: false,
  },
  [ClusterStatus.OFFLINE]: {
    status: ClusterStatus.OFFLINE,
    label: "Offline",
    // XCircle for offline, AlertTriangle for error: a host that is down and a fault on the
    // dashboard's side are different problems, and once both are red the icon tells them
    // apart.
    icon: XCircle,
    iconClassName: "text-status-offline",
    tone: "negative",
    reachable: false,
  },
  [ClusterStatus.ERROR]: {
    status: ClusterStatus.ERROR,
    label: "Error",
    icon: AlertTriangle,
    iconClassName: "text-status-offline",
    tone: "negative",
    reachable: false,
  },
};

const UNKNOWN: Entry = {
  status: ClusterStatus.PENDING,
  label: "Unknown",
  icon: HelpCircle,
  iconClassName: "text-status-unknown",
  tone: "neutral",
  reachable: false,
};

// Status words outside the enum that a cached or older response can still carry
// (ex; "pending_trust" from the monitoring endpoint), mapped onto the enum.
const ALIASES: Record<string, ClusterStatus> = {
  pending_trust: ClusterStatus.NOT_AUTHORIZED,
  not_trusted: ClusterStatus.NOT_AUTHORIZED,
  connected: ClusterStatus.ONLINE,
  disconnected: ClusterStatus.OFFLINE,
};

function isNodeStatus(value: unknown): value is NodeStatus {
  return typeof value === "object" && value !== null && "connected" in value;
}

// The monitoring payload names the same idea differently: is_online where NodeStatus says
// connected. Disjoint keys, so the two shapes can be told apart structurally.
function isMonitoringNode(value: unknown): value is ClusterMonitoringMetrics {
  return typeof value === "object" && value !== null && "is_online" in value;
}

/**
 * Resolve any shape the product carries into the one vocabulary.
 *
 * Accepts a NodeStatus as well as the enum because the dashboard cards and the connection
 * summary hold booleans. NodeStatus carries a `status` field, which is read directly;
 * matching words like "trust" in `node.error` would break when a backend message is reworded.
 */
export function clusterStatusMeta(
  input:
    | ClusterStatus
    | NodeStatus
    | ClusterMonitoringMetrics
    | string
    | null
    | undefined,
): ClusterStatusMeta {
  let key: string | undefined;

  if (isMonitoringNode(input)) {
    // is_online wins: a cached body can carry cluster_status words outside the enum, and the
    // boolean is never ambiguous.
    key = input.is_online ? ClusterStatus.ONLINE : input.cluster_status;
  } else if (isNodeStatus(input)) {
    key =
      input.status ??
      // Fallback for a cached body without `status`, read from the booleans: reachable=true
      // with connected=false means the host answered and refused the dashboard.
      (input.connected
        ? ClusterStatus.ONLINE
        : input.reachable === true
          ? ClusterStatus.NOT_AUTHORIZED
          : ClusterStatus.OFFLINE);
  } else if (typeof input === "string") {
    key = input;
  }

  const normalized = key ? (ALIASES[key] ?? key) : undefined;
  const entry =
    normalized && normalized in ENTRIES
      ? ENTRIES[normalized as ClusterStatus]
      : UNKNOWN;

  return {
    ...entry,
    toneRank: TONE_RANK[entry.tone],
    srLabel: `Status: ${entry.label}`,
  };
}

/**
 * The line under a cluster's name in a picker: the id when it differs from the display name,
 * then the endpoint, so two clusters an operator named alike still tell apart. The backend
 * writes the literal "unknown" when it has no endpoint; under a name that reads as a fact
 * about the cluster, so it shows as nothing.
 */
export function clusterSublabel(
  id: string,
  displayName: string | undefined,
  endpoint: string | null | undefined,
): string | undefined {
  const shownEndpoint = endpoint && endpoint !== "unknown" ? endpoint : undefined;
  const label = displayName || id;
  const parts = label !== id ? [id, shownEndpoint] : [shownEndpoint];
  const joined = parts.filter(Boolean).join("  ");
  return joined || undefined;
}
