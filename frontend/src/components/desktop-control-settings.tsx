"use client";

/**
 * Whether an agent may see and drive this organization's guest desktops.
 *
 * important: four switches, not one. They are four separate disclosures and the backend composes
 * them separately. A screenshot leaves the deployment for a third-party model; a keystroke acts
 * as the logged-in desktop user, inheriting their unlocked keyring and validated sudo; the
 * clipboard is the highest-yield exfiltration primitive on the surface; and guest access is a
 * shell inside the container, which is root by default.
 *
 * The last two used to ride on the first, so an organization that agreed to screenshots was
 * silently agreeing to both. That is why they default to off here and why turning capture off
 * clears all three rather than remembering them for next time: the server does exactly that, and
 * a form that showed otherwise would be describing a state that does not exist.
 *
 * Until this existed the consent was settable only by an API call, which meant the one decision
 * the whole feature rests on was reachable by curl and by nothing else.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { toast } from "@/hooks/use-toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { ShieldAlert } from "lucide-react";
import type { DesktopControlPolicy } from "@/types/api";

type Draft = {
  enabled: boolean;
  allow_input: boolean;
  allow_clipboard: boolean;
  allow_guest_access: boolean;
};

const EMPTY: Draft = {
  enabled: false,
  allow_input: false,
  allow_clipboard: false,
  allow_guest_access: false,
};

const toDraft = (policy: DesktopControlPolicy): Draft => ({
  enabled: policy.enabled,
  allow_input: policy.allow_input,
  allow_clipboard: policy.allow_clipboard,
  allow_guest_access: policy.allow_guest_access,
});

const sameDraft = (a: Draft, b: Draft) =>
  (Object.keys(a) as (keyof Draft)[]).every((key) => a[key] === b[key]);

/**
 * Under the `organizations` family, so the `organization.preferences` reconcile that follows a
 * save (and every other organization write) reads it again.
 */
const consentKey = (orgId: number | null) => ["organizations", orgId, "desktop-control"] as const;

/** Each switch, with the sentence an operator needs to decide it.
 *
 * note: `prompt`, not `label`. `check-heading-case` reads a `label:` key as a column label and
 * requires Title Case, which is right for something that NAMES a thing and wrong for these:
 * every one of them says something rather than naming something, and the same guard's own rule
 * puts a sentence in sentence case. Renaming the key is the honest way to be in the right slot.
 */
const CONTROLS: {
  key: keyof Draft;
  prompt: string;
  description: string;
  elevated?: boolean;
}[] = [
  {
    key: "enabled",
    prompt: "Let an agent see these screens",
    description:
      "Screenshots of a running desktop leave this deployment for the model the agent uses. Nothing else here is available until this is on.",
  },
  {
    key: "allow_input",
    prompt: "Let it type and click",
    description:
      "It acts as the logged-in desktop user, which inherits that session's signed-in browser, unlocked keyring and already-validated sudo.",
    elevated: true,
  },
  {
    key: "allow_clipboard",
    prompt: "Let it read the clipboard",
    description:
      "Whatever was last copied on that desktop, including anything pasted out of a password manager.",
    elevated: true,
  },
  {
    key: "allow_guest_access",
    prompt: "Let an installed add-on run commands inside guests",
    description:
      "Root in the container by default. Separate from the switches above because it is a different power, not a larger version of one.",
    elevated: true,
  },
];

export function DesktopControlSettings() {
  const queryClient = useQueryClient();
  const orgsQuery = useQuery({
    queryKey: ["organizations"],
    queryFn: () => api.organizations.list(),
  });
  const organizations = React.useMemo(() => orgsQuery.data ?? [], [orgsQuery.data]);

  // The chosen organization, falling back to the first one, and to it again if the chosen one
  // leaves the list. Derived on read rather than set from an effect.
  const [chosenOrgId, setChosenOrgId] = React.useState<number | null>(null);
  const orgId = organizations.some((org) => org.id === chosenOrgId)
    ? chosenOrgId
    : organizations[0]?.id ?? null;
  const orgName = organizations.find((org) => org.id === orgId)?.name ?? "this organization";

  const policyQuery = useQuery({
    queryKey: consentKey(orgId),
    queryFn: () => api.organizations.desktopControl.get(orgId as number),
    enabled: orgId !== null,
  });
  const saved = policyQuery.data ? toDraft(policyQuery.data) : null;

  // important: unsaved edits belong to one organization and are never overwritten by a re-read.
  // A refetch from any organization write elsewhere used to be able to land on top of them, and
  // switching organizations used to carry the previous one's switches over, even into an
  // organization whose read had failed, where Save would then have written them.
  const [edits, setEdits] = React.useState<{ orgId: number; draft: Draft } | null>(null);
  const draft = edits && edits.orgId === orgId ? edits.draft : saved ?? EMPTY;
  const dirty = saved !== null && edits !== null && edits.orgId === orgId && !sameDraft(edits.draft, saved);
  const [pendingOrgId, setPendingOrgId] = React.useState<number | null>(null);

  // Turning capture off withdraws the other three, because that is what the server stores. A form
  // that kept them ticked would be showing a state the deployment does not have.
  const update = (key: keyof Draft, value: boolean) => {
    if (orgId === null) return;
    const next = key === "enabled" && !value ? { ...EMPTY } : { ...draft, [key]: value };
    setEdits({ orgId, draft: next });
  };

  const switchOrg = (next: number) => {
    if (next === orgId) return;
    if (dirty) {
      setPendingOrgId(next);
      return;
    }
    setEdits(null);
    setChosenOrgId(next);
  };

  const saveMutation = useMutation({
    mutationFn: ({ id, values }: { id: number; values: Draft }) =>
      api.organizations.desktopControl.set(id, values),
    onSuccess: (policy, { id }) => {
      queryClient.setQueryData(consentKey(id), policy);
      setEdits((current) => (current?.orgId === id ? null : current));
      void reconcileMutationImpact(queryClient, "organization.preferences").catch(
        () => undefined,
      );
      toast({
        title: "Consent updated",
        description: policy.enabled
          ? "An agent may now work with this organization's desktops."
          : "An agent can no longer reach this organization's desktops.",
      });
    },
    onError: (err: Error) => {
      toast({
        title: "Could not update consent",
        description: err.message || "The consent could not be saved.",
        variant: "destructive",
      });
    },
  });
  const saving = saveMutation.isPending;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <CardTitle>Desktop Control</CardTitle>
            <CardDescription>
              What an AI agent may do with the desktops running on this organization&apos;s
              clusters. Off unless you turn it on.
            </CardDescription>
          </div>
          {/* The stored answer, and nothing until it has been read: "Not allowed" over a read
              still in flight claimed a state nobody had seen. */}
          {saved ? (
            saved.enabled ? (
              <Badge variant="secondary">Allowed</Badge>
            ) : (
              <Badge variant="outline">Not allowed</Badge>
            )
          ) : null}
        </div>
      </CardHeader>

      <CardContent className="space-y-5">
        {orgsQuery.isLoading ? (
          <p className="text-sm text-muted-foreground" role="status">Loading organizations…</p>
        ) : orgsQuery.error && !orgsQuery.data ? (
          <ReadErrorState noun="organizations" error={orgsQuery.error} onRetry={() => void orgsQuery.refetch()} />
        ) : organizations.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            You are not a member of an organization, so there is no consent to give here.
          </p>
        ) : (
          <>
            <div className="space-y-2">
              <Label htmlFor="desktop-control-org">Organization</Label>
              <Select
                value={orgId === null ? undefined : String(orgId)}
                onValueChange={(value) => switchOrg(Number(value))}
                disabled={saving}
              >
                <SelectTrigger id="desktop-control-org" className="w-full sm:w-80">
                  <SelectValue placeholder="Choose an organization" />
                </SelectTrigger>
                <SelectContent>
                  {organizations.map((org) => (
                    <SelectItem key={org.id} value={String(org.id)}>
                      {org.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {policyQuery.error && !policyQuery.data ? (
              <ReadErrorState
                noun="desktop control consent"
                error={policyQuery.error}
                onRetry={() => void policyQuery.refetch()}
              />
            ) : (
              <div className="space-y-4" aria-busy={policyQuery.isLoading}>
                {CONTROLS.map((control) => {
                  const gated = control.key !== "enabled" && !draft.enabled;
                  const id = `desktop-control-${control.key}`;
                  return (
                    <div
                      key={control.key}
                      className="flex items-start justify-between gap-4 rounded-lg border p-3"
                    >
                      <div className="space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <Label htmlFor={id}>{control.prompt}</Label>
                          {control.elevated ? (
                            <span id={`${id}-elevated`} className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                              <ShieldAlert className="h-3 w-3" aria-hidden="true" />
                              Elevated
                            </span>
                          ) : null}
                        </div>
                        <p id={`${id}-description`} className="text-sm text-muted-foreground">
                          {control.description}
                        </p>
                      </div>
                      <Switch
                        id={id}
                        checked={draft[control.key]}
                        disabled={gated || saved === null || saving}
                        onCheckedChange={(value) => update(control.key, value)}
                        aria-describedby={
                          control.elevated ? `${id}-elevated ${id}-description` : `${id}-description`
                        }
                      />
                    </div>
                  );
                })}
              </div>
            )}

            <div className="flex flex-wrap items-center gap-3">
              <Button
                onClick={() => orgId !== null && saveMutation.mutate({ id: orgId, values: draft })}
                disabled={!dirty || saving}
              >
                {saving ? "Saving…" : "Save consent"}
              </Button>
              {dirty ? (
                <>
                  <Button variant="outline" onClick={() => setEdits(null)} disabled={saving}>
                    Discard changes
                  </Button>
                  <span className="text-sm text-muted-foreground" role="status">Not saved yet</span>
                </>
              ) : null}
            </div>

            <p className="text-sm text-muted-foreground">
              A cluster shared with another organization needs every organization on it to agree,
              so turning these on here may not be enough on its own.
            </p>
          </>
        )}
      </CardContent>

      <AlertDialog open={pendingOrgId !== null} onOpenChange={(open) => !open && setPendingOrgId(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard Unsaved Consent?</AlertDialogTitle>
            <AlertDialogDescription>
              The changes to {orgName} have not been saved. Switching organizations discards them.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setEdits(null);
                setChosenOrgId(pendingOrgId);
                setPendingOrgId(null);
              }}
            >
              Discard and switch
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}
