import type { BadgeTone } from "@/components/ui/badge";

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
  switch (severity.toLowerCase()) {
    case "high":
    case "critical":
      return "negative";
    case "medium":
    case "warning":
      return "caution";
    default:
      return "neutral";
  }
}
