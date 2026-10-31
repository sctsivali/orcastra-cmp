"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2 } from "lucide-react";

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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect, type SearchableSelectOption } from "@/components/ui/searchable-select";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import {
  OWNER_KEY_STATE,
  canOwnOrganizations,
  formatKeyDate,
  ownerKeyState,
  uncoveredClusters,
} from "@/lib/key-ownership-display";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import type { APIKeyInfo, IntegrationOrganization } from "@/types/api";

const REASON_LIMIT = 500;

interface TransferOwnerKeyDialogProps {
  organization: IntegrationOrganization | null;
  keys: APIKeyInfo[];
  onOpenChange: (open: boolean) => void;
}

/**
 * Move a synced organization to another owner key. Administrators only; the server enforces it.
 *
 * The owner is the only key that may sync the organization, update its members and policies and
 * provision tenant access for it, so the picker offers only keys that could do all of that: active
 * OrcaHub keys holding inventory.read and org.sync, never an add-on's credential. The slug has to
 * be typed, because a move to the wrong key hands one integration another's organization.
 */
export function TransferOwnerKeyDialog({ organization, keys, onOpenChange }: TransferOwnerKeyDialogProps) {
  const queryClient = useQueryClient();
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const submitInFlightRef = useRef(false);

  // A fresh form each time a different organization opens it.
  useEffect(() => {
    setTarget("");
    setReason("");
    setConfirmation("");
  }, [organization?.id]);

  const candidates = useMemo(
    () => keys.filter((key) => canOwnOrganizations(key) && key.id !== organization?.api_key_id),
    [keys, organization?.api_key_id],
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
  const chosen = candidates.find((key) => key.id === target) ?? null;
  const uncovered = organization && chosen ? uncoveredClusters(organization, chosen) : [];

  const mutation = useMutation({
    mutationFn: (move: { orgId: number; apiKeyId: string; reason: string }) =>
      api.integrations.transferOrganizationOwner(move.orgId, { api_key_id: move.apiKeyId, reason: move.reason }),
    onSuccess: (answer) => {
      void reconcileMutationImpact(queryClient, "integration.write").catch(() => undefined);
      toast({
        title: answer.changed ? "Owner key transferred" : "Owner key unchanged",
        description: answer.changed
          ? "OrcaHub confirms ownership on that connection's next sync, within about 5 minutes on its default schedule."
          : "That key already owns this organization.",
      });
      onOpenChange(false);
    },
    onError: (error: Error) => {
      toast({ title: "Could not transfer owner key", description: error.message, variant: "destructive" });
    },
  });

  if (!organization) return null;
  const state = ownerKeyState(organization);
  const coOwners = (organization.owner_keys ?? []).filter((key) => !key.is_primary);
  const ready =
    Boolean(chosen) && reason.trim().length > 0 && confirmation.trim() === organization.slug && !mutation.isPending;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitInFlightRef.current || !ready) return;
    submitInFlightRef.current = true;
    try {
      await mutation.mutateAsync({ orgId: organization.id, apiKeyId: target, reason: reason.trim() });
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
          <DialogTitle>Transfer Owner Key</DialogTitle>
          <DialogDescription>
            The new key replaces every owner key of this organization. Only it can then sync the
            organization, update its members and policies, and provision tenant access for it.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
          <DialogBody className="-mr-2 pr-2">
            <div className="grid gap-4 py-4">
              <div className="space-y-1 text-sm">
                <p className="text-muted-foreground">Organization</p>
                <p className="font-medium">
                  {organization.name} <span className="font-mono text-xs text-muted-foreground">{organization.slug}</span>
                </p>
              </div>
              <div className="space-y-1 text-sm">
                <p className="text-muted-foreground">Current owner key</p>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">
                    {organization.owner_key_name || organization.api_key_id || "No owner key"}
                  </span>
                  <Badge variant={OWNER_KEY_STATE[state].tone}>{OWNER_KEY_STATE[state].label}</Badge>
                </div>
                {coOwners.length > 0 && (
                  <p className="text-xs text-muted-foreground">
                    The transfer also removes {coOwners.length === 1 ? "its co-owner" : `its ${coOwners.length} co-owners`}:{" "}
                    {coOwners.map((key) => key.name || key.api_key_id).join(", ")}.
                  </p>
                )}
              </div>

              <div className="space-y-2">
                <Label htmlFor="transfer-owner-key">New Owner Key</Label>
                <SearchableSelect
                  id="transfer-owner-key"
                  value={target}
                  onValueChange={setTarget}
                  options={options}
                  placeholder={candidates.length ? "Choose an active OrcaHub key" : "No key can own it"}
                  searchPlaceholder="Search keys by name, application, or id"
                  emptyText="No active OrcaHub key with inventory.read and org.sync matches."
                  disabled={candidates.length === 0 || mutation.isPending}
                  ariaDescribedBy="transfer-owner-key-help"
                />
                <p id="transfer-owner-key-help" className="text-xs text-muted-foreground">
                  Only active OrcaHub keys holding inventory.read and org.sync are listed, never an
                  add-on credential.
                </p>
              </div>

              {chosen?.expiry_state === "expiring" && (
                <div role="status" className="flex items-start gap-2 text-xs">
                  <AlertTriangle aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-pending" />
                  <p className="text-muted-foreground">
                    This key expires on {formatKeyDate(chosen.expires_at)}. When it does, this
                    organization stops syncing again.
                  </p>
                </div>
              )}
              {uncovered.length > 0 && (
                <div role="status" className="flex items-start gap-2 text-xs">
                  <AlertTriangle aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-pending" />
                  <p className="text-muted-foreground">
                    This key&apos;s access does not include {uncovered.join(", ")}, so provisioning
                    tenant access there is refused until it does.
                  </p>
                </div>
              )}

              <div className="space-y-2">
                <Label htmlFor="transfer-owner-reason">Reason</Label>
                <Textarea
                  id="transfer-owner-reason"
                  value={reason}
                  onChange={(event) => setReason(event.target.value.slice(0, REASON_LIMIT))}
                  maxLength={REASON_LIMIT}
                  rows={3}
                  placeholder="Why the owner changes, kept in the audit record"
                  required
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="transfer-owner-confirm">Type the organization&apos;s slug to confirm</Label>
                <Input
                  id="transfer-owner-confirm"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  placeholder={organization.slug}
                  autoComplete="off"
                  spellCheck={false}
                  className="font-mono"
                />
              </div>
            </div>
          </DialogBody>
          <DialogFooter className="shrink-0 border-t pt-3">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!ready}>
              {mutation.isPending && <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden />}
              Transfer Owner
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
