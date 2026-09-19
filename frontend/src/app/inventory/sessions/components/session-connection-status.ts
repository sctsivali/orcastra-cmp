import type { BadgeTone } from "@/components/ui/badge";
import { formatLabel } from "@/lib/utils";

export type SessionConnectionStatus =
  | "connected"
  | "connecting"
  | "disconnected"
  | "error";

type SessionConnectionStatusDisplay = {
  label: string;
  tone: BadgeTone;
};

const SESSION_CONNECTION_STATUS: Record<
  SessionConnectionStatus,
  SessionConnectionStatusDisplay
> = {
  connected: { label: "Connected", tone: "positive" },
  connecting: { label: "Connecting", tone: "caution" },
  disconnected: { label: "Disconnected", tone: "negative" },
  error: { label: "Error", tone: "negative" },
};

export function sessionConnectionStatusDisplay(
  status: string | null | undefined,
): SessionConnectionStatusDisplay {
  const normalized = status?.trim().toLowerCase() as SessionConnectionStatus | undefined;
  return normalized && SESSION_CONNECTION_STATUS[normalized]
    ? SESSION_CONNECTION_STATUS[normalized]
    : { label: formatLabel(status ?? "unknown"), tone: "neutral" };
}
