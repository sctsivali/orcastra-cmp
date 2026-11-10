/**
 * How add-on state is spelled in the UI, one spelling per idea.
 *
 * Like `integration-display.ts`: a status word or badge tone invented at a call site lets the
 * same state read two ways on two screens.
 */

import type {
  ExtensionInstallStatus,
  WebhookDeliveryState,
  WebhookSubscriptionStatus,
} from "@/types/api";

type Tone = "positive" | "caution" | "neutral" | "negative";

/**
 * Use the Badge variants on the solid `--status-*` tones, never success/warning/error.
 * Those translucent tints measure as low as 2.73:1 against the card in light mode, under the
 * 4.5:1 a 12px semibold badge needs; these four measure 4.72:1 or better in both themes.
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
  // Not a failure: the installation's reach excludes that cluster, so the event was withheld.
  // "Failed" would send an operator looking for a broken endpoint.
  dropped: { label: "Withheld", tone: "neutral" },
};

/** How many clusters a grant reaches, worded so one reads as a sentence rather than "1 clusters". */
export function describeClusterReach(clusterIds: string[]): string {
  if (clusterIds.length === 0) return "No clusters";
  if (clusterIds.length === 1) return clusterIds[0];
  return `${clusterIds.length} clusters`;
}

/**
 * Why a capability is marked elevated, as the server words it. The reasons differ per
 * capability, so no fixed sentence fits; "Elevated" is the fallback when the server gives none.
 */
export function capabilityNote(
  elevated: boolean,
  reason: string | null | undefined,
): string | null {
  if (!elevated) return null;
  return reason ?? "Elevated";
}
