import type { VMStatus } from "@/types/api";
import { formatLabel } from "@/lib/utils";

export type ControlledStatusTone = "positive" | "caution" | "neutral" | "negative";

export interface ControlledStatusDisplay {
  label: string;
  tone: ControlledStatusTone;
}

type VMStatusKey = Lowercase<VMStatus>;

const VM_STATUS_DISPLAY: Record<VMStatusKey, ControlledStatusDisplay> = {
  running: { label: "Running", tone: "positive" },
  stopped: { label: "Stopped", tone: "neutral" },
  frozen: { label: "Frozen", tone: "caution" },
  error: { label: "Error", tone: "negative" },
  unknown: { label: "Unknown", tone: "neutral" },
};

/**
 * Display metadata for an instance status.
 *
 * The API contract currently uses Title Case, but runtime payloads can outlive a frontend build.
 * Normalising at this boundary keeps a future lowercase enum from leaking into the UI while the
 * raw value remains available to filters and requests.
 */
export function instanceStatusDisplay(status: VMStatus | string | null | undefined) {
  const raw = String(status ?? "").trim();
  const known = VM_STATUS_DISPLAY[raw.toLowerCase() as VMStatusKey];
  return known ?? { label: formatLabel(raw || "unknown"), tone: "neutral" as const };
}

type MemberStatusKey = "online" | "offline" | "evacuated";

const MEMBER_STATUS_DISPLAY: Record<MemberStatusKey, ControlledStatusDisplay> = {
  online: { label: "Online", tone: "positive" },
  offline: { label: "Offline", tone: "negative" },
  evacuated: { label: "Evacuated", tone: "caution" },
};

export function memberStatusDisplay(status: string | null | undefined) {
  const raw = String(status ?? "").trim();
  const key = raw.toLowerCase() as MemberStatusKey;
  const known = MEMBER_STATUS_DISPLAY[key];
  return {
    ...(known ?? { label: formatLabel(raw || "unknown"), tone: "neutral" as const }),
    online: key === "online",
  };
}
