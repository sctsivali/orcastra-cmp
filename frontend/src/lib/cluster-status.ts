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
 * One vocabulary for "can this cluster be used right now".
 *
 * Before this, five files answered the question independently and disagreed on all three
 * axes. The shades drifted (emerald-500 in one file, green-500 in another, green-400 in a
 * third; zinc-400 against gray-400), the wording drifted ("Connected" against "Online",
 * "Some nodes not connected" against "Offline"), and the regions page shipped its dots with
 * no dark: variants at all. documentation/ui/frontend-design-language.md names exactly this
 * as the defect: semantic colour comes from a token, never from a raw palette step, and
 * never from two steps of the same semantic in two places.
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
  /** Sort weight. Lower sorts first, so reachable clusters lead a picker. */
  tone: ClusterStatusTone;
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
    // note: not formatLabel("not_authorized"), which gives "Not Authorized" and reads as a
    // permission denial. The cluster answered; the trust handshake is what is unfinished.
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
    // XCircle for offline, AlertTriangle for error: the existing clusters page already drew
    // that line and it is worth keeping. A host that is down and a fault in us are different
    // problems, and the icon is the only thing separating them once both go red.
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

// important: the monitoring endpoint emits "pending_trust", which is not in the enum, from
// routers/monitoring.py. Accepting it here keeps monitoring/node-card.tsx correct without a
// backend change. Drop this alias only once that endpoint speaks the enum.
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
 * summary hold booleans. The backend now sends a `status` field on NodeStatus, so this reads
 * it directly rather than pattern-matching `node.error` for words like "trust", which would
 * change what the UI decides the first time a backend message is reworded.
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
    // is_online wins because a body cached before the monitoring endpoint spoke the enum can
    // still carry the old cluster_status vocabulary, and "connected" is never ambiguous.
    key = input.is_online ? ClusterStatus.ONLINE : input.cluster_status;
  } else if (isNodeStatus(input)) {
    key =
      input.status ??
      // Fallback for a body cached before the backend carried `status`. Structural, not
      // textual: reachable=true with connected=false means the host answered and refused us.
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
 * writes the literal "unknown" when it has no endpoint; printing that under a name reads as a
 * fact about the cluster rather than a gap in what we know, so it shows as nothing.
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
