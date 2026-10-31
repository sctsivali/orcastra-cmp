"use client";

/**
 * Branding editor: one organization's own branding, or (Administrator) the deployment default.
 *
 * What an account sees is resolved on the server: its organization's own row, filled from the
 * organization that sold to it (followed live), filled from the deployment default
 * (backend services/branding/). This editor writes one of those rows and says which.
 */

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Building2, Globe2, ShieldAlert } from "lucide-react";
import { api } from "@/lib/api";
import { brandingAPI } from "@/lib/branding-api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { toast } from "@/hooks/use-toast";
import { usePermissions } from "@/hooks/use-permissions";
import { cn } from "@/lib/utils";
import { DEFAULT_PERSONALIZATION, type PersonalizationSettings as Settings } from "@/types/personalization";
import type { MyBranding, OrgBranding } from "@/types/branding";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { usePersonalization } from "./personalization-provider";
import { mergeWithDefaults } from "./personalization/preferences";
import { PersonalizationSettings } from "./personalization-settings";
import { BrandingEditorContext } from "./branding/branding-editor-context";
import { BrandingSourceNotice } from "./branding/branding-source-notice";
import { useBrandingEditorState } from "./branding/use-branding-editor-state";

const DEPLOYMENT = "deployment";

type OrgOption = { id: string; name: string; isMember: boolean };

function hasSettings(value: object | undefined | null): boolean {
  return Boolean(value) && Object.keys(value as object).length > 0;
}

function resetLabelFor(target: string | null, meta: OrgBranding | null): string {
  if (target === DEPLOYMENT) return "Reset to Built-in Look";
  if (!meta || !hasSettings(meta.settings)) return "Reset to Defaults";
  if (meta.inherits_from === "provider" && meta.provider) return `Follow ${meta.provider.name}`;
  return meta.inherits_from === "deployment" ? "Use Deployment Default" : "Reset to Defaults";
}

export function OrgPersonalizationSettings() {
  const queryClient = useQueryClient();
  const globalPersonalization = usePersonalization();
  const { isAdmin, isLoading: rolesLoading } = usePermissions();
  // ids are coerced to strings the moment they enter this component. OrgResponse.id is an int,
  // and a raw number in this state is what made the Select below render its placeholder while an
  // organization was in fact selected: Radix matches by value, and 122 is not "122".
  const [orgs, setOrgs] = React.useState<OrgOption[]>([]);
  const [mine, setMine] = React.useState<MyBranding | null>(null);
  const [orgId, setOrgId] = React.useState<string | null>(null);
  const [meta, setMeta] = React.useState<OrgBranding | null>(null);
  const [settings, setSettings] = React.useState<Settings>(DEFAULT_PERSONALIZATION);
  const [isLoading, setIsLoading] = React.useState(true);
  const [noOrg, setNoOrg] = React.useState(false);

  const showOrg = React.useCallback((answer: OrgBranding) => {
    setMeta(answer);
    setSettings(mergeWithDefaults(hasSettings(answer.settings) ? answer.settings : answer.effective));
  }, []);

  const loadTarget = React.useCallback(async (target: string) => {
    try {
      if (target === DEPLOYMENT) {
        const answer = await brandingAPI.getDeployment();
        setMeta(null);
        setSettings(mergeWithDefaults(answer.settings));
      } else {
        showOrg(await api.organizations.getPreferences(target));
      }
    } catch {
      setMeta(null);
      setSettings(DEFAULT_PERSONALIZATION);
      toast({ title: "Could not load branding", variant: "destructive" });
    }
  }, [showOrg]);

  React.useEffect(() => {
    if (rolesLoading) return;
    let cancelled = false;
    const init = async () => {
      setIsLoading(true);
      try {
        // important: BOTH, and the second one is the whole point. The list is ordered by name and
        // for an Admin it is every active organization, so "the first one" is whichever customer
        // sorts alphabetically first. Seeding the selection from the endpoint the sidebar reads
        // makes "what you edit" and "what you see" the same thing by construction.
        const [orgList, myBranding] = await Promise.all([
          api.organizations.list(),
          api.organizations.getMyOrgPreferences().catch(() => null),
        ]);
        if (cancelled) return;
        const list = (orgList ?? []).map((o) => ({ id: String(o.id), name: o.name, isMember: o.is_member === true }));
        setOrgs(list);
        setMine(myBranding);
        if (list.length === 0 && !isAdmin) {
          setNoOrg(true);
          return;
        }
        const primary = myBranding?.subject === "organization" ? String(myBranding.organization_id) : null;
        const target = primary && list.some((o) => o.id === primary)
          ? primary
          : isAdmin ? DEPLOYMENT : list[0].id;
        setOrgId(target);
        await loadTarget(target);
      } catch {
        toast({ title: "Could not load organizations", description: "Could not fetch your organizations.", variant: "destructive" });
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };
    void init();
    return () => { cancelled = true; };
  }, [rolesLoading, isAdmin, loadTarget]);

  const handleTargetChange = async (value: string) => {
    setOrgId(value);
    setIsLoading(true);
    await loadTarget(value);
    setIsLoading(false);
  };

  const renderedTarget = mine ? (mine.subject === "deployment" ? DEPLOYMENT : String(mine.organization_id)) : null;
  const previewsSidebar = orgId !== null && orgId === renderedTarget;
  // Silent: the sidebar, the cookie hint and every organization inheriting this row re-read.
  const { refreshSettings } = globalPersonalization;

  const persist = React.useCallback(async (toSave: Settings) => {
    if (!orgId) return;
    if (orgId === DEPLOYMENT) {
      await brandingAPI.updateDeployment(toSave);
      void reconcileMutationImpact(queryClient, "branding.deployment").catch(() => undefined);
    } else {
      setMeta(await api.organizations.updatePreferences(orgId, { ...toSave }));
      void reconcileMutationImpact(queryClient, "organization.preferences").catch(() => undefined);
    }
    await refreshSettings();
  }, [orgId, queryClient, refreshSettings]);

  const remove = React.useCallback(async () => {
    if (!orgId) return;
    if (orgId === DEPLOYMENT) {
      await brandingAPI.deleteDeployment();
      void reconcileMutationImpact(queryClient, "branding.deployment").catch(() => undefined);
    } else {
      await api.organizations.deletePreferences(orgId);
      void reconcileMutationImpact(queryClient, "organization.preferences").catch(() => undefined);
    }
    await refreshSettings();
    await loadTarget(orgId);
  }, [orgId, queryClient, refreshSettings, loadTarget]);

  const onEdit = React.useCallback((edited: Settings) => {
    // Previewed in the sidebar only when it is the branding the sidebar shows; another
    // organization's colours must not paint this operator's own screen.
    if (previewsSidebar) {
      globalPersonalization.applySettingsOptimistically({ ...edited, theme: globalPersonalization.settings.theme });
    }
  }, [previewsSidebar, globalPersonalization]);

  const editor = useBrandingEditorState({
    targetKey: orgId, settings, setSettings, isLoading, resetLabel: resetLabelFor(orgId, meta), persist, remove, onEdit,
  });

  const handleProviderChange = async (providerId: number | null) => {
    if (!orgId || orgId === DEPLOYMENT) return;
    try {
      showOrg(await api.organizations.setProvider(orgId, providerId));
      void reconcileMutationImpact(queryClient, "organization.preferences").catch(() => undefined);
      await refreshSettings();
      toast({ title: providerId === null ? "Provider removed" : "Provider set" });
    } catch {
      toast({ title: "Could not set provider", variant: "destructive" });
    }
  };

  if (noOrg) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <AlertCircle className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
            No organization
          </CardTitle>
          <CardDescription>
            You are not a member of any organization. Create or join one first to manage organization branding.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const deployment = orgId === DEPLOYMENT;
  const selectedOrg = orgs.find((o) => o.id === orgId);
  // An Admin's list is every active organization, so "selected" and "yours" are different
  // questions. is_member is the server's answer to the second one.
  const foreignOrg = selectedOrg !== undefined && !selectedOrg.isMember;
  const choices = isAdmin ? orgs.length + 1 : orgs.length;

  return (
    <div className="space-y-4">
      {choices > 1 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Building2 className="h-4 w-4" aria-hidden="true" />
              Branding To Edit
            </CardTitle>
          </CardHeader>
          <CardContent>
            <Select value={orgId ?? undefined} onValueChange={handleTargetChange}>
              <SelectTrigger className="w-full sm:w-[320px]" aria-label="Branding to edit">
                <SelectValue placeholder="Select organization" />
              </SelectTrigger>
              <SelectContent>
                {isAdmin && <SelectItem value={DEPLOYMENT}>Deployment Default</SelectItem>}
                {orgs.map((o) => (
                  <SelectItem key={o.id} value={o.id}>
                    {o.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </CardContent>
        </Card>
      )}

      {/* Which branding every control below writes to.
          Sticky on purpose. This is the only thing standing between an operator and rewriting a
          customer's branding, and the controls that do the writing are a scroll away, which is
          exactly how an operator came to change ALIMRUGI's colours while watching their own.
          Offset by the app header, which is sticky at h-14 on mobile and h-16 from md. */}
      <div
        className={cn(
          "sticky top-14 md:top-16 z-20 flex items-start gap-2 rounded-md border px-4 py-3 text-sm",
          foreignOrg ? "border-warning bg-warning/10 text-foreground" : "bg-muted/40 text-muted-foreground",
        )}
      >
        {deployment ? (
          <Globe2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        ) : foreignOrg ? (
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
        ) : (
          <Building2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        )}
        {deployment ? (
          <span>
            Changes here apply to the <strong>sign-in page</strong>, the browser tab, Administrators, and every
            organization without branding of its own or of a provider.
          </span>
        ) : (
          <span>
            Changes here apply to <strong>all users</strong> in{" "}
            <Badge variant="outline" className="mx-1">
              {selectedOrg?.name ?? "this organization"}
            </Badge>
            {meta && meta.followers_count > 0 ? <> and the organizations that follow it</> : null}
            {foreignOrg ? (
              <>
                . You are not a member of this organization, so this is someone else&apos;s branding and not the
                one your own sidebar shows.
              </>
            ) : null}
          </span>
        )}
      </div>

      {!deployment && meta && selectedOrg && (
        <BrandingSourceNotice
          orgName={selectedOrg.name}
          meta={meta}
          providerOptions={orgs}
          canChooseProvider={isAdmin}
          onProviderChange={(id) => void handleProviderChange(id)}
        />
      )}

      <BrandingEditorContext.Provider value={editor}>
        <PersonalizationSettings />
      </BrandingEditorContext.Provider>
    </div>
  );
}
