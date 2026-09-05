/**
 * How add-on state is spelled in the UI. One spelling per idea, in one file.
 *
 * Sibling of `integration-display.ts`, and split out for the same reason: a status word or a
 * badge tone invented at a call site is how the same state ends up reading two ways on two
 * screens.
 */

import type {
  ExtensionInstallStatus,
  WebhookDeliveryState,
  WebhookSubscriptionStatus,
} from "@/types/api";

type Tone = "positive" | "caution" | "neutral" | "negative";

/**
 * important: the solid `--status-*` tones, not success/warning/error.
 *
 * The translucent tints measure as low as 2.73:1 against the card in light mode, under the 4.5:1
 * the badge's own 12px semibold asks for. These four are measured at 4.72:1 or better in both
 * themes.
 */
export const INSTALL_STATUS: Record<ExtensionInstallStatus, { label: string; tone: Tone }> = {
  active: { label: "Active", tone: "positive" },
  suspended: { label: "Suspended", tone: "caution" },
  revoked: { label: "Revoked", tone: "negative" },
};

export const SUBSCRIPTION_STATUS: Record<
  WebhookSubscriptionStatus,
  { label: string; tone: Tone }
> = {
  active: { label: "Active", tone: "positive" },
  pending_verification: { label: "Not Verified", tone: "caution" },
  suspended: { label: "Suspended", tone: "negative" },
  disabled: { label: "Disabled", tone: "neutral" },
};

export const DELIVERY_STATE: Record<WebhookDeliveryState, { label: string; tone: Tone }> = {
  delivered: { label: "Delivered", tone: "positive" },
  pending: { label: "Queued", tone: "neutral" },
  inflight: { label: "Sending", tone: "neutral" },
  failed: { label: "Retrying", tone: "caution" },
  dead: { label: "Gave up", tone: "negative" },
  // Not a failure. The installation stopped reaching that cluster, so the event was withheld
  // rather than sent, and an operator reading "Failed" would go looking for a broken endpoint.
  dropped: { label: "Withheld", tone: "neutral" },
};

/** How many clusters a grant reaches, worded so one reads as a sentence rather than "1 clusters". */
export function describeClusterReach(clusterIds: string[]): string {
  if (clusterIds.length === 0) return "No clusters";
  if (clusterIds.length === 1) return clusterIds[0];
  return `${clusterIds.length} clusters`;
}

/**
 * Why a capability is marked, taken from the server.
 *
 * Never a hardcoded sentence. There are five distinct reasons and a single label was wrong for
 * most of them, which is why the field is nullable and per-capability.
 */
export function capabilityNote(
  elevated: boolean,
  reason: string | null | undefined,
): string | null {
  if (!elevated) return null;
  return reason ?? "Elevated";
}
