"use client";

import { useRef } from "react";
import { Building2 } from "lucide-react";

import {
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { OverflowText } from "@/components/ui/overflow-text";
import { getAPIKeyStatus } from "@/lib/integration-display";
import { formatKeyDate, openSyncedOrganizations, ownedKeySentence } from "@/lib/key-ownership-display";
import type { APIKeyInfo } from "@/types/api";

/**
 * Which OrcaHub organizations a key owns, on its card.
 *
 * The owner is the only key that may sync an organization, update its members and provision for
 * it, so this is what an operator needs before ending a key, and what an expiry is about to stop.
 * Nothing renders for a key that owns none, or whose ownership could not be read.
 */
export function KeyOwnershipSummary({ apiKey }: { apiKey: APIKeyInfo }) {
  const count = apiKey.owned_organization_count ?? 0;
  if (count === 0) return null;
  const slugs = apiKey.owned_organization_slugs ?? [];
  const hidden = apiKey.owned_organization_hidden_count ?? 0;
  const status = getAPIKeyStatus(apiKey);
  const expiring = status === "Active" && apiKey.expiry_state === "expiring";
  const dead = status === "Expired" || status === "Revoked" || status === "Inactive";
  const coOwned = count - (apiKey.sole_owned_organization_count ?? count);

  return (
    <div className="space-y-2 rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Building2 className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
        <span className="font-medium">
          Owns {count} {count === 1 ? "organization" : "organizations"}
        </span>
        {expiring && <Badge variant="caution">Owner Key Expiring</Badge>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {slugs.map((slug) => (
          <span key={slug} className="min-w-0 max-w-[12rem] rounded bg-muted px-2 py-0.5 font-mono text-xs">
            <OverflowText value={slug} label={`organization ${slug}`} />
          </span>
        ))}
        {slugs.length < count - hidden && (
          <span className="text-xs text-muted-foreground">+{count - hidden - slugs.length} more</span>
        )}
        {hidden > 0 && <span className="text-xs text-muted-foreground">+{hidden} you cannot see</span>}
      </div>
      {coOwned > 0 && (
        <p className="text-xs text-muted-foreground">
          Another owner key also owns {coOwned === count ? (count === 1 ? "it" : "all of them") : `${coOwned} of them`}, which
          keeps {coOwned === 1 ? "that organization" : "those organizations"} syncing if this key ends.
        </p>
      )}
      {expiring && (
        <p className="text-xs text-muted-foreground">
          When this key expires, the organizations it owns stop syncing. Transfer them before{" "}
          {formatKeyDate(apiKey.expires_at)}.
        </p>
      )}
      {dead && (
        <p className="text-xs text-muted-foreground">
          This key no longer works, so the organizations it owns are frozen until an administrator
          transfers them.
        </p>
      )}
    </div>
  );
}

/**
 * What a Revoke or Delete of an owner key opens instead of a destructive confirmation.
 *
 * The server refuses to end a key that owns an organization, so offering the action would only
 * produce a refusal. This says why and, for an administrator, opens the list where the
 * organizations are moved.
 *
 * important: the jump runs from onCloseAutoFocus. Radix returns focus to the trigger as the dialog
 * closes, so moving focus from the button's click handler was undone a moment later and a keyboard
 * user was left on the card instead of the list.
 */
export function TransferOwnershipFirst({
  apiKey,
  action,
  isAdmin,
}: {
  apiKey: APIKeyInfo;
  action: "revoke" | "delete";
  isAdmin: boolean;
}) {
  const jumpRef = useRef(false);
  return (
    <AlertDialogContent
      onCloseAutoFocus={(event) => {
        if (!jumpRef.current) return;
        jumpRef.current = false;
        event.preventDefault();
        openSyncedOrganizations();
      }}
    >
      <AlertDialogHeader>
        <AlertDialogTitle>Transfer Ownership First</AlertDialogTitle>
        <AlertDialogDescription>{ownedKeySentence(apiKey, action, isAdmin)}</AlertDialogDescription>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel>Close</AlertDialogCancel>
        {isAdmin && (
          <AlertDialogAction
            onClick={() => {
              jumpRef.current = true;
            }}
          >
            Open Synced Organizations
          </AlertDialogAction>
        )}
      </AlertDialogFooter>
    </AlertDialogContent>
  );
}
