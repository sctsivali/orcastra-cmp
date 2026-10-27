import type { BadgeTone } from "@/components/ui/badge";
import type { HostCheckId, HostCheckStatus } from "@/types/host-health";
import { Database, Fingerprint, HardDrive, KeyRound, ScrollText, Zap, type LucideIcon } from "lucide-react";

/** Label and tone stay paired: one closed vocabulary, never the raw wire value on screen. */
export const HOST_STATUS: Record<HostCheckStatus, { label: string; tone: BadgeTone | "muted" }> = {
  ok: { label: "Healthy", tone: "positive" },
  degraded: { label: "Degraded", tone: "caution" },
  down: { label: "Down", tone: "negative" },
  unknown: { label: "Unknown", tone: "neutral" },
  not_configured: { label: "Not Configured", tone: "muted" },
};

export const HOST_CHECK_ICON: Record<HostCheckId, LucideIcon> = {
  vault: KeyRound,
  identity: Fingerprint,
  logging: ScrollText,
  database: Database,
  cache: Zap,
  storage: HardDrive,
};

export const NEEDS_ATTENTION: ReadonlySet<HostCheckStatus> = new Set(["down", "degraded", "unknown"]);
