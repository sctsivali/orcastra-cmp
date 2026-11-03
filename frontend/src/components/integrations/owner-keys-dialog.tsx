"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { OverflowText } from "@/components/ui/overflow-text";
import { SearchableSelect, type SearchableSelectOption } from "@/components/ui/searchable-select";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import { OWNER_KEY_STATE, canOwnOrganizations, formatKeyDate } from "@/lib/key-ownership-display";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import type { APIKeyInfo, IntegrationOrganization, OwnerSetResponse } from "@/types/api";

const REASON_LIMIT = 500;

type OwnerChange =
  | { kind: "add"; apiKeyId: string }
  | { kind: "remove"; apiKeyId: string }
  | { kind: "primary"; apiKeyId: string };

interface OwnerKeysDialogProps {
  organization: IntegrationOrganization | null;
  keys: APIKeyInfo[];
  onOpenChange: (open: boolean) => void;
}

function describeError(error: Error): string {
  // note: the owner-set routes answer 404 while INTEGRATION_MULTI_OWNER_ENABLED is off
  if (/not found/i.test(error.message) && !/organization/i.test(error.message)) {
    return "Several owner keys per organization are turned off on this control plane.";
  }
  return error.message;
}

/**
 * The keys that own a synced organization, for an administrator to add to, take from or choose the
 * primary of. Any owner key syncs the organization, updates its members and policies and provisions
 * tenant access for it; the primary is the one OrcaHub callbacks use. The last owner cannot be
 * removed here (a transfer replaces the whole set instead), and every change needs a reason.
 */
export function OwnerKeysDialog({ organization, keys, onOpenChange }: OwnerKeysDialogProps) {
  const queryClient = useQueryClient();
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");
  const submitInFlightRef = useRef(false);

  useEffect(() => {
    setTarget("");
    setReason("");
  }, [organization?.id]);

  const owners = useMemo(() => organization?.owner_keys ?? [], [organization?.owner_keys]);
  const candidates = useMemo(
    () => keys.filter((key) => canOwnOrganizations(key) && !owners.some((owner) => owner.api_key_id === key.id)),
    [keys, owners],
  );
  const options: SearchableSelectOption[] = candidates.map((key) => ({
    value: key.id,
    label: String(key.name || key.id),
    sublabel: `${key.app_name} · ${key.id}`,
    status:
      key.expiry_state === "expiring"
        ? { label: `Expires ${formatKeyDate(key.expires_at)}`, tone: "caution" as const }
        : undefined,
  }));

  const mutation = useMutation({
    mutationFn: (change: OwnerChange): Promise<OwnerSetResponse> => {
      const orgId = organization?.id ?? 0;
      const why = reason.trim();
      if (change.kind === "add") return api.integrations.addOrganizationOwner(orgId, { api_key_id: change.apiKeyId, reason: why });
      if (change.kind === "remove") return api.integrations.removeOrganizationOwner(orgId, change.apiKeyId, why);
      return api.integrations.setOrganizationPrimary(orgId, { api_key_id: change.apiKeyId, reason: why });
    },
    onSuccess: (answer, change) => {
      void reconcileMutationImpact(queryClient, "integration.write").catch(() => undefined);
      const titles = { add: "Owner key added", remove: "Owner key removed", primary: "Primary owner key changed" };
      toast({
        title: answer.changed ? titles[change.kind] : "Owner keys unchanged",
        description: answer.changed
          ? "OrcaHub sees the change on that connection's next sync, within about 5 minutes on its default schedule."
          : "The owner keys were already as asked.",
      });
      setTarget("");
    },
    onError: (error: Error) => {
      toast({ title: "Could not change owner keys", description: describeError(error), variant: "destructive" });
    },
  });

  if (!organization) return null;
  const hasReason = reason.trim().length > 0;
  const busy = mutation.isPending;
  // note: the change in flight, so only its own button spins; Add used to spin for a Remove too.
  const pending = busy ? mutation.variables : undefined;

  const run = async (change: OwnerChange) => {
    if (submitInFlightRef.current || !hasReason) return;
    submitInFlightRef.current = true;
    try {
      await mutation.mutateAsync(change);
    } catch {
      // The toast says why, and the dialog keeps the operator's input.
    } finally {
      submitInFlightRef.current = false;
    }
  };

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0">
          <DialogTitle>Owner Keys</DialogTitle>
          <DialogDescription>
            Any of these keys may sync {organization.name}, update its members and policies, and provision
            tenant access for it. The primary is the one OrcaHub calls back with.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="-mr-2 pr-2">
          <div className="grid gap-4 py-4">
            <ul className="space-y-2" aria-label={`Owner keys of ${organization.slug}`}>
              {owners.map((owner) => (
                <li key={owner.api_key_id} className="flex flex-wrap items-center gap-2 rounded-md border p-2 text-sm">
                  <div className="min-w-0 flex-1">
                    <p className="font-medium">
                      <OverflowText value={owner.name || owner.api_key_id} label={`owner key of ${organization.slug}`} />
                    </p>
                    <p className="font-mono text-xs text-muted-foreground">
                      <OverflowText value={owner.api_key_id} label={`owner key id of ${organization.slug}`} />
                    </p>
                  </div>
                  {owner.is_primary && <Badge variant="outline">Primary</Badge>}
                  <Badge variant={OWNER_KEY_STATE[owner.state].tone}>{OWNER_KEY_STATE[owner.state].label}</Badge>
                  {!owner.is_primary && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={!hasReason || busy}
                      onClick={() => void run({ kind: "primary", apiKeyId: owner.api_key_id })}
                      aria-describedby={hasReason ? undefined : "owner-keys-reason-hint"}
                    >
                      {pending?.kind === "primary" && pending.apiKeyId === owner.api_key_id && (
                        <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden />
                      )}
                      {/* note: the key in the name, after the visible words, so a repeated
                          "Make Primary" says which key and the name still starts with what is shown */}
                      Make Primary<span className="sr-only">: {owner.name || owner.api_key_id}</span>
                    </Button>
                  )}
                  {owners.length > 1 && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={!hasReason || busy}
                      onClick={() => void run({ kind: "remove", apiKeyId: owner.api_key_id })}
                      aria-describedby={hasReason ? undefined : "owner-keys-reason-hint"}
                    >
                      {pending?.kind === "remove" && pending.apiKeyId === owner.api_key_id && (
                        <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden />
                      )}
                      Remove<span className="sr-only">: {owner.name || owner.api_key_id}</span>
                    </Button>
                  )}
                </li>
              ))}
            </ul>
            {owners.length === 1 && (
              <p className="text-xs text-muted-foreground">
                An organization always keeps one owner key. To replace it, use Transfer Owner.
              </p>
            )}

            <div className="space-y-2">
              <Label htmlFor="owner-keys-add">Add Owner Key</Label>
              <SearchableSelect
                id="owner-keys-add"
                value={target}
                onValueChange={setTarget}
                options={options}
                placeholder={candidates.length ? "Choose an active OrcaHub key" : "No other key can own it"}
                searchPlaceholder="Search keys by name, application, or id"
                emptyText="No active OrcaHub key with inventory.read and org.sync matches."
                disabled={candidates.length === 0 || busy}
                ariaDescribedBy="owner-keys-add-help"
              />
              <p id="owner-keys-add-help" className="text-xs text-muted-foreground">
                Only active OrcaHub keys holding inventory.read and org.sync are listed, never an add-on
                credential.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="owner-keys-reason">Reason</Label>
              <Textarea
                id="owner-keys-reason"
                value={reason}
                onChange={(event) => setReason(event.target.value.slice(0, REASON_LIMIT))}
                maxLength={REASON_LIMIT}
                rows={2}
                placeholder="Why the owner keys change, kept in the audit record"
                required
                aria-describedby="owner-keys-reason-hint"
              />
              <p id="owner-keys-reason-hint" className="text-xs text-muted-foreground">
                Every change here needs a reason first.
              </p>
            </div>
          </div>
        </DialogBody>
        <DialogFooter className="shrink-0 border-t pt-3">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          <Button
            type="button"
            disabled={!target || !hasReason || busy}
            onClick={() => void run({ kind: "add", apiKeyId: target })}
          >
            {pending?.kind === "add" && <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden />}
            Add Owner Key
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
