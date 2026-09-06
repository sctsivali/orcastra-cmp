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

import { api } from "@/lib/api";
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
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { MonitorPlay, ShieldAlert } from "lucide-react";
import type {
  DesktopControlPolicy,
  Organization,
} from "@/types/api";

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
  const [organizations, setOrganizations] = React.useState<Organization[]>([]);
  const [orgId, setOrgId] = React.useState<number | null>(null);
  const [draft, setDraft] = React.useState<Draft>(EMPTY);
  const [saved, setSaved] = React.useState<Draft>(EMPTY);
  const [loading, setLoading] = React.useState(true);
  const [loadingPolicy, setLoadingPolicy] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const orgs = await api.organizations.list();
        if (cancelled) return;
        setOrganizations(orgs);
        setOrgId(orgs.length > 0 ? orgs[0].id : null);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Could not load organizations");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  React.useEffect(() => {
    if (orgId === null) return;
    let cancelled = false;
    setLoadingPolicy(true);
    setError(null);
    (async () => {
      try {
        const policy: DesktopControlPolicy = await api.organizations.desktopControl.get(orgId);
        if (cancelled) return;
        const next: Draft = {
          enabled: policy.enabled,
          allow_input: policy.allow_input,
          allow_clipboard: policy.allow_clipboard,
          allow_guest_access: policy.allow_guest_access,
        };
        setDraft(next);
        setSaved(next);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Could not read this consent");
        }
      } finally {
        if (!cancelled) setLoadingPolicy(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [orgId]);

  // Turning capture off withdraws the other three, because that is what the server stores. A form
  // that kept them ticked would be showing a state the deployment does not have.
  const update = (key: keyof Draft, value: boolean) => {
    setDraft((current) => {
      if (key === "enabled" && !value) return { ...EMPTY };
      return { ...current, [key]: value };
    });
  };

  const dirty = (Object.keys(draft) as (keyof Draft)[]).some((k) => draft[k] !== saved[k]);

  const save = async () => {
    if (orgId === null) return;
    setSaving(true);
    try {
      const policy = await api.organizations.desktopControl.set(orgId, draft);
      const next: Draft = {
        enabled: policy.enabled,
        allow_input: policy.allow_input,
        allow_clipboard: policy.allow_clipboard,
        allow_guest_access: policy.allow_guest_access,
      };
      setDraft(next);
      setSaved(next);
      toast({
        title: "Consent updated",
        description: next.enabled
          ? "An agent may now work with this organization's desktops."
          : "An agent can no longer reach this organization's desktops.",
      });
    } catch (err) {
      toast({
        title: "Nothing was changed",
        description: err instanceof Error ? err.message : "The consent could not be saved",
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <CardTitle className="flex items-center gap-2">
              <MonitorPlay className="h-4 w-4 text-muted-foreground" />
              Desktop Control
            </CardTitle>
            <CardDescription>
              What an AI agent may do with the desktops running on this organization&apos;s
              clusters. Off unless you turn it on.
            </CardDescription>
          </div>
          {saved.enabled ? (
            <Badge variant="secondary">Allowed</Badge>
          ) : (
            <Badge variant="outline">Not allowed</Badge>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-5">
        {loading ? (
          <p className="text-sm text-muted-foreground">Loading organizations...</p>
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
                onValueChange={(value) => setOrgId(Number(value))}
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

            {error ? (
              <p className="text-sm text-destructive" role="alert">
                {error}
              </p>
            ) : null}

            <div className="space-y-4">
              {CONTROLS.map((control) => {
                const gated = control.key !== "enabled" && !draft.enabled;
                return (
                  <div
                    key={control.key}
                    className="flex items-start justify-between gap-4 rounded-lg border p-3"
                  >
                    <div className="space-y-1">
                      <Label
                        htmlFor={`desktop-control-${control.key}`}
                        className="flex items-center gap-2"
                      >
                        {control.prompt}
                        {control.elevated ? (
                          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                            <ShieldAlert className="h-3 w-3" />
                            Elevated
                          </span>
                        ) : null}
                      </Label>
                      <p className="text-sm text-muted-foreground">{control.description}</p>
                    </div>
                    <Switch
                      id={`desktop-control-${control.key}`}
                      checked={draft[control.key]}
                      disabled={gated || loadingPolicy || saving}
                      onCheckedChange={(value) => update(control.key, value)}
                    />
                  </div>
                );
              })}
            </div>

            <div className="flex items-center gap-3">
              <Button onClick={save} disabled={!dirty || saving || loadingPolicy}>
                {saving ? "Saving..." : "Save consent"}
              </Button>
              {dirty ? (
                <span className="text-sm text-muted-foreground">Not saved yet</span>
              ) : null}
            </div>

            <p className="text-sm text-muted-foreground">
              A cluster shared with another organization needs every organization on it to agree,
              so turning these on here may not be enough on its own.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
