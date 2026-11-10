import { formatDate } from "@/lib/utils";
import { getAPIKeyStatus } from "@/lib/integration-display";
import type {
  APIKeyInfo,
  IntegrationOrganization,
  KeyOwnsOrganizationsDetail,
  OwnerKeyState,
  ProvisioningWouldStopDetail,
} from "@/types/api";

/**
 * An owner key's state as the list shows it. "hidden" means the caller is neither an
 * administrator nor a partner of the key's own organization, which only a sale through the key
 * proves. A partner of the buyer, which carries the seller's key, does not see it either.
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
 * `unknown` is not frozen: the key's record could not be read, and the read can be retried.
 * `unclaimed` is not frozen either: any key may claim it.
 */
export function ownerKeyIsFrozen(state: OwnerStateShown): boolean {
  return state === "expired" || state === "revoked" || state === "inactive" || state === "missing";
}

/**
 * An organization no owner key can sync any more. With several owner keys, a dead primary is not
 * enough: another owner that is active or expiring keeps it syncing (OrcaHub calls back through it).
 */
export function organizationIsFrozen(org: Pick<IntegrationOrganization, "owner_key_state" | "owner_keys">): boolean {
  if (!ownerKeyIsFrozen(ownerKeyState(org))) return false;
  return !(org.owner_keys ?? []).some((key) => key.state === "active" || key.state === "expiring");
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

/** A stored timestamp as a date, empty when there is none. */
export function formatKeyDate(value: string | null | undefined): string {
  return value ? formatDate(value) : "";
}

/** The structured 409 a revoke or delete of an owner key answers with, if this error is one. */
export function keyOwnsOrganizationsDetail(error: unknown): KeyOwnsOrganizationsDetail | null {
  const detail = (error as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && (detail as { error?: unknown }).error === "key_owns_organizations") {
    return detail as KeyOwnsOrganizationsDetail;
  }
  return null;
}

/** The 409 a capability edit asks with before provisioning stops for owned organizations, if this is one. */
export function provisioningWouldStopDetail(error: unknown): ProvisioningWouldStopDetail | null {
  const detail = (error as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && (detail as { error?: unknown }).error === "provisioning_would_stop") {
    return detail as ProvisioningWouldStopDetail;
  }
  return null;
}

const ENDING: Record<"revoke" | "delete", string> = { revoke: "Revoking it", delete: "Deleting it" };

/**
 * The sentence the server answers a revoke or delete of this key with, composed before asking.
 *
 * Mirrors _owned_message in backend/app/services/integration_key_ownership.py. The server stays
 * the authority: an out-of-date list still gets the server's own sentence in the error toast.
 * With co-owners only organizations no other key owns block the action; the key list counts
 * them without names, so that sentence does too.
 */
export function ownedKeySentence(key: APIKeyInfo, action: "revoke" | "delete", isAdmin: boolean): string {
  const shown = key.owned_organization_slugs ?? [];
  const hidden = key.owned_organization_hidden_count ?? 0;
  const total = key.owned_organization_count ?? shown.length + hidden;
  const stopNow = key.is_add_on_credential
    ? "Suspend the add-on to stop it now."
    : "To stop a leaked secret now, rotate the secret instead: the key ID and its organizations stay.";
  const sole = key.sole_owned_organization_count;
  if (typeof sole === "number" && sole > 0 && sole < total) {
    const oneSole = sole === 1;
    const solely = oneSole ? "it" : "them";
    const others = total - sole;
    return [
      `This key is the only owner of ${sole} of the ${total} OrcaHub organizations it owns.`,
      `${ENDING[action]} would leave ${oneSole ? "that organization" : "those organizations"} with an owner no other key can replace, so ${oneSole ? "its" : "their"} sync, member updates and provisioning would stop.`,
      `${others === 1 ? "The other one has" : `The other ${others} have`} another owner key, which keeps ${others === 1 ? "it" : "them"} syncing.`,
      isAdmin
        ? `Transfer ${solely} to another active key first, or delete ${solely}.`
        : `Ask an administrator to transfer ${solely} to another active key first, or to delete ${solely}.`,
      stopNow,
    ].join(" ");
  }
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
    stopNow,
  ];
  return parts.join(" ");
}

/**
 * Bring the Synced Organizations panel into view, where an administrator transfers owners.
 *
 * Focus moves with the scroll (the panel carries tabIndex -1), so a keyboard user lands on the
 * list instead of a dialog button that has just closed.
 */
export function openSyncedOrganizations(): void {
  const panel = document.getElementById("synced-organizations");
  if (!panel) return;
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  panel.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  panel.focus({ preventScroll: true });
}
