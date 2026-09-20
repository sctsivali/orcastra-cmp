import type { BadgeTone } from "@/components/ui/badge";
import { formatLabel } from "@/lib/utils";

type WarningSeverityKey =
  | "critical"
  | "error"
  | "high"
  | "warning"
  | "medium"
  | "moderate"
  | "low"
  | "info";

type WarningStatusKey = "new" | "acknowledged" | "resolved";

interface WarningDisplay {
  label: string;
  tone: BadgeTone;
}

const WARNING_SEVERITY_DISPLAY: Record<WarningSeverityKey, WarningDisplay> = {
  critical: { label: "Critical", tone: "negative" },
  error: { label: "Error", tone: "negative" },
  high: { label: "High", tone: "negative" },
  warning: { label: "Warning", tone: "caution" },
  medium: { label: "Medium", tone: "caution" },
  moderate: { label: "Moderate", tone: "caution" },
  low: { label: "Low", tone: "neutral" },
  info: { label: "Info", tone: "neutral" },
};

const WARNING_STATUS_DISPLAY: Record<WarningStatusKey, WarningDisplay> = {
  new: { label: "New", tone: "caution" },
  acknowledged: { label: "Acknowledged", tone: "neutral" },
  resolved: { label: "Resolved", tone: "positive" },
};

function normalized(value: string | null | undefined): string {
  return value?.trim().toLowerCase() ?? "";
}

export function warningSeverityDisplay(
  severity: string | null | undefined,
): WarningDisplay {
  const key = normalized(severity);
  if (key in WARNING_SEVERITY_DISPLAY) {
    return WARNING_SEVERITY_DISPLAY[key as WarningSeverityKey];
  }
  return { label: formatLabel(key || "unknown"), tone: "neutral" };
}

export function warningSeverityLabel(severity: string): string {
  return warningSeverityDisplay(severity).label;
}

export function warningStatusDisplay(status: string | null | undefined): WarningDisplay {
  const key = normalized(status);
  if (key in WARNING_STATUS_DISPLAY) {
    return WARNING_STATUS_DISPLAY[key as WarningStatusKey];
  }
  return { label: formatLabel(key || "unknown"), tone: "neutral" };
}

export function warningStatusLabel(status: string): string {
  return warningStatusDisplay(status).label;
}

/**
 * A warning's severity as a state tone.
 *
 * note: severity is not cluster reachability, but it runs the same urgency ladder and the
 * --status-* tokens behind these tones are the only measured set in the system. The glyph
 * from getSeverityIcon stays the primary carrier, so the tone is reinforcement rather than
 * the only signal.
 *
 * The map this replaces sent medium and warning to variant="default", which is bg-primary,
 * the user-configurable personalization accent.
 */
export function warningSeverityTone(severity: string): BadgeTone {
  return warningSeverityDisplay(severity).tone;
}
