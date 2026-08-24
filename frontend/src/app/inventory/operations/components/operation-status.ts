import type { BadgeTone } from "@/components/ui/badge";

/**
 * An LXD operation's status as a state tone.
 *
 * The map this replaces returned Badge variants directly, and its success case was
 * variant="default", which is bg-primary. app/globals.css rebinds --primary from the
 * operator's personalization accent in an unlayered :root block, and the picker has no
 * contrast clamp, so a completed operation used to render in whatever colour the operator
 * happened to choose.
 */
export function operationStatusTone(status: string): BadgeTone {
  switch (status.toLowerCase()) {
    case "success":
    case "completed":
      return "positive";
    // Still in flight, so it is unsettled rather than either good or bad. Same tone as a
    // cluster that answered but has not finished its trust handshake.
    case "running":
      return "caution";
    case "pending":
      return "neutral";
    default:
      return "negative";
  }
}
