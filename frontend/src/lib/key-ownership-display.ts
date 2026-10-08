import { getAPIKeyStatus } from "@/lib/integration-display";
import type { APIKeyInfo, IntegrationOrganization, KeyOwnsOrganizationsDetail, OwnerKeyState } from "@/types/api";

/**
 * An owner key's state as the list shows it. "hidden" is a state the list does not show: the
 * caller is neither an administrator nor one of the organization's partners.
 */
export type OwnerStateShown = OwnerKeyState | "hidden";

/**
 * How an organization's owner key stands, as the synced-organization list reports it.
 *
 * The owner is the only key that may sync an OrcaHub organization, update its members and
 * policies and provision tenant access for it. A word and a tone per state, paired here so the
 * badge and the filter facet cannot disagree.
 */
export const OWNER_KEY_STATE: Record<
  OwnerStateShown,
  { label: string; tone: "positive" | "caution" | "negative" | "destructive" | "secondary" | "muted" | "outline" }
> = {
  active: { label: "Active", tone: "positive" },
  expiring: { label: "Expiring", tone: "caution" },
  expired: { label: "Expired", tone: "negative" },
  revoked: { label: "Revoked", tone: "destructive" },
  inactive: { label: "Inactive", tone: "secondary" },
  missing: { label: "Missing", tone: "negative" },
  unknown: { label: "Unknown", tone: "muted" },
  unclaimed: { label: "Unclaimed", tone: "outline" },
  hidden: { label: "Not Shown", tone: "muted" },
};

/** Working first, then the ways an owner stops working, then the two that are not a key's state. */
export const OWNER_KEY_STATE_ORDER: readonly string[] = [
  "Active", "Expiring", "Expired", "Revoked", "Inactive", "Missing", "Unknown", "Unclaimed", "Not Shown",
];

export function ownerKeyState(org: Pick<IntegrationOrganization, "owner_key_state">): OwnerStateShown {
  if (org.owner_key_state === null) return "hidden";
  return org.owner_key_state ?? "unknown";
}

/**
 * An owner that will never sync again, so the organization is frozen until it is transferred.
 *
 * note: unknown is not frozen. It means the key's record could not be read, which is a
 * question to retry, not a verdict. Unclaimed is not frozen either: any key may claim it.
 */
export function ownerKeyIsFrozen(state: OwnerStateShown): boolean {
  return state === "expired" || state === "revoked" || state === "inactive" || state === "missing";
}

/** What an owner syncs with, as the backend's SYNC_CAPABILITIES: /sync needs both. */
const SYNC_CAPABILITIES = ["inventory.read", "org.sync"] as const;

/** Whether a key could own an OrcaHub organization, as the transfer endpoint judges it. */
export function canOwnOrganizations(key: APIKeyInfo): boolean {
  if (key.is_add_on_credential) return false;
  if (getAPIKeyStatus(key) !== "Active") return false;
  if (key.app_type && key.app_type !== "orcahub") return false;
  const granted = key.capabilities;
  // null capabilities is a legacy key, which holds every capability it was issued with.
  return granted == null || SYNC_CAPABILITIES.every((name) => granted.includes(name));
}

/** Clusters bound to the organization that the key's grant does not name. */
export function uncoveredClusters(org: IntegrationOrganization, key: APIKeyInfo): string[] {
  const granted = new Set((key.cluster_permissions ?? []).map((grant) => grant.cluster_id));
  return (org.cluster_ids ?? []).filter((clusterId) => !granted.has(clusterId));
}

/** A stored timestamp as a date. A value without an offset is UTC, as the backend writes it. */
export function formatKeyDate(value: string | null | undefined): string {
  if (!value) return "";
  const iso = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

/** The structured 409 a revoke or delete of an owner key answers with, if this error is one. */
export function keyOwnsOrganizationsDetail(error: unknown): KeyOwnsOrganizationsDetail | null {
  const detail = (error as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && (detail as { error?: unknown }).error === "key_owns_organizations") {
    return detail as KeyOwnsOrganizationsDetail;
  }
  return null;
}

const ENDING: Record<"revoke" | "delete", string> = { revoke: "Revoking it", delete: "Deleting it" };

/**
 * The sentence the server answers a revoke or delete of this key with, composed before asking.
 *
 * note: mirrors _owned_message in backend/app/services/integration_key_ownership.py so the dialog
 * says what the refusal would say. The server stays the authority: a list that is out of date
 * still gets the server's own sentence in the error toast.
 */
export function ownedKeySentence(key: APIKeyInfo, action: "revoke" | "delete", isAdmin: boolean): string {
  const shown = key.owned_organization_slugs ?? [];
  const hidden = key.owned_organization_hidden_count ?? 0;
  const total = key.owned_organization_count ?? shown.length + hidden;
  const one = total === 1;
  const them = one ? "it" : "them";
  const their = one ? "its" : "their";
  const named = shown.slice(0, 5).join(", ");
  const rest: string[] = [];
  if (shown.length > 5) rest.push(`${shown.length - 5} more`);
  if (hidden > 0) rest.push(`${hidden} you cannot see`);
  const listed = rest.length === 0 ? named : named ? `${named} and ${rest.join(" and ")}` : rest.join(" and ");
  const parts = [
    `This key owns ${total} OrcaHub ${one ? "organization" : "organizations"} (${listed}).`,
    `${ENDING[action]} would leave ${them} with an owner no other key can replace, so ${their} sync, member updates and provisioning would stop.`,
    isAdmin
      ? `Transfer ${them} to another active key first, or delete ${them}.`
      : `Ask an administrator to transfer ${them} to another active key first, or to delete ${them}.`,
    key.is_add_on_credential
      ? "Suspend the add-on to stop it now."
      : "To stop a leaked secret now, rotate the secret instead: the key ID and its organizations stay.",
  ];
  return parts.join(" ");
}

/**
 * Bring the Synced Organizations panel into view, where an administrator transfers owners.
 *
 * note: focus moves with the scroll (the panel carries tabIndex -1), so a keyboard user lands on
 * the list rather than staying on a dialog button that has just closed.
 */
export function openSyncedOrganizations(): void {
  const panel = document.getElementById("synced-organizations");
  if (!panel) return;
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  panel.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  panel.focus({ preventScroll: true });
}
