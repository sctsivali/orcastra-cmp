"use client";

/**
 * Organization Personalization Settings
 *
 * Wraps the existing PersonalizationSettings component with an
 * org-level PersonalizationContext so that all edits are saved
 * to the organization_preferences table instead of user_preferences.
 *
 * This is used by Partners/Admins in the Settings → Personalization tab
 * to manage branding that Tenant users inherit.
 */

import * as React from "react";
import { useSession } from "next-auth/react";
import { useQueryClient } from "@tanstack/react-query";
import {
  PersonalizationSettings as PersonalizationSettingsType,
  PersonalizationContextType,
  LogoSettings,
  BackgroundSettings,
  BrandingSettings,
  ColorSettings,
  ThemePreference,
  DEFAULT_PERSONALIZATION,
} from "@/types/personalization";
import { PersonalizationContext, usePersonalization } from "./personalization-provider";
import { PersonalizationSettings } from "./personalization-settings";
import { api } from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { toast } from "@/hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Building2, AlertCircle, ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// merge partial API data with defaults
function mergeWithDefaults(
  data: Partial<PersonalizationSettingsType>,
): PersonalizationSettingsType {
  return {
    ...DEFAULT_PERSONALIZATION,
    ...data,
    logo: { ...DEFAULT_PERSONALIZATION.logo, ...data.logo },
    background: { ...DEFAULT_PERSONALIZATION.background, ...data.background },
    branding: { ...DEFAULT_PERSONALIZATION.branding, ...data.branding },
    colors: { ...DEFAULT_PERSONALIZATION.colors, ...data.colors },
    theme: data.theme ?? DEFAULT_PERSONALIZATION.theme,
  };
}

export function OrgPersonalizationSettings() {
  const queryClient = useQueryClient();
  const { data: session } = useSession();
  // access global personalization context so we can refresh after saving
  const globalPersonalization = usePersonalization();
  const [orgId, setOrgId] = React.useState<string | null>(null);
  // ids are coerced to strings the moment they enter this component. OrgResponse.id is an int,
  // and a raw number in this state is what made the Select below render its placeholder while an
  // organization was in fact selected: Radix matches by value, and 122 is not "122".
  const [orgs, setOrgs] = React.useState<{ id: string; name: string; isMember: boolean }[]>([]);
  const [settings, setSettings] = React.useState<PersonalizationSettingsType>(
    DEFAULT_PERSONALIZATION,
  );
  const [isLoading, setIsLoading] = React.useState(true);
  const [isSaving, setIsSaving] = React.useState(false);
  const [noOrg, setNoOrg] = React.useState(false);

  // debounce ref: delays API call so rapid edits (e.g. color picks) batch
  const saveTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSettingsRef = React.useRef<PersonalizationSettingsType | null>(null);
  // Undo/Redo support
  const settingsRef = React.useRef<PersonalizationSettingsType>(DEFAULT_PERSONALIZATION);
  const undoStackRef = React.useRef<PersonalizationSettingsType[]>([]);
  const redoStackRef = React.useRef<PersonalizationSettingsType[]>([]);
  const isUndoRedoRef = React.useRef(false);
  const MAX_HISTORY = 50;
  const [canUndo, setCanUndo] = React.useState(false);
  const [canRedo, setCanRedo] = React.useState(false);

  // Keep settingsRef always in sync with latest settings state
  React.useEffect(() => {
    settingsRef.current = settings;
  }, [settings]);

  // Load orgs then prefs
  React.useEffect(() => {
    let cancelled = false;

    const init = async () => {
      setIsLoading(true);
      try {
        // important: BOTH, and the second one is the whole point. The list is ordered by name and
        // for an Admin it is every active organization, so "the first one" is whichever customer
        // sorts alphabetically first. The sidebar resolves its organization differently, through
        // _get_primary_org_id, which is the caller's earliest membership. Two rules for "your
        // organization" that usually agree and silently do not for anyone who can see more than
        // one: the editor wrote to ALIMRUGI while the operator watched Sivali Cloud Technology,
        // so every change looked like it reverted, and a customer's branding was overwritten by
        // someone who never chose it. Seeding the selection from the same endpoint the sidebar
        // reads is what makes "what you edit" and "what you see" the same thing by construction.
        const [orgList, mine] = await Promise.all([
          api.organizations.list(),
          api.organizations.getMyOrgPreferences().catch(() => null),
        ]);
        if (cancelled) return;

        if (!orgList || orgList.length === 0) {
          setNoOrg(true);
          setIsLoading(false);
          return;
        }

        const list = orgList.map((o) => ({
          id: String(o.id),
          name: o.name,
          isMember: o.is_member === true,
        }));
        setOrgs(list);

        const primary = mine?.organization_id ? String(mine.organization_id) : null;
        const target =
          primary && list.some((o) => o.id === primary) ? primary : list[0].id;
        setOrgId(target);

        await loadPreferences(target);
      } catch {
        toast({
          title: "Failed to load organizations",
          description: "Could not fetch your organizations.",
          variant: "destructive",
        });
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    init();
    return () => {
      cancelled = true;
      // cleanup debounce timer
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadPreferences = async (id: string) => {
    try {
      const res = await api.organizations.getPreferences(id);
      if (res?.settings && Object.keys(res.settings).length > 0) {
        setSettings(mergeWithDefaults(res.settings as Partial<PersonalizationSettingsType>));
      } else {
        setSettings(DEFAULT_PERSONALIZATION);
      }
    } catch {
      // 404 = no prefs yet - use defaults
      setSettings(DEFAULT_PERSONALIZATION);
    }
  };

  const handleOrgChange = async (value: string) => {
    setOrgId(value);
    setIsLoading(true);
    await loadPreferences(value);
    setIsLoading(false);
  };

  // save handler - debounced to prevent spamming API on rapid edits
  // immediate=true skips debounce (used for undo/redo discrete actions)
  const saveSettings = React.useCallback(
    async (newSettings: PersonalizationSettingsType, { immediate = false }: { immediate?: boolean } = {}) => {
      if (!orgId) return;

      const updated: PersonalizationSettingsType = {
        ...newSettings,
        lastModified: new Date().toISOString(),
      };

      // Only push to undo stack at the START of a new edit sequence
      // (when no debounce timer is pending). This prevents slider drags
      // from overwriting the original value on every intermediate tick.
      if (!saveTimerRef.current && !isUndoRedoRef.current) {
        undoStackRef.current = [...undoStackRef.current, { ...settingsRef.current }];
        if (undoStackRef.current.length > MAX_HISTORY) {
          undoStackRef.current = undoStackRef.current.slice(-MAX_HISTORY);
        }
        // New edit invalidates redo history
        redoStackRef.current = [];
        setCanUndo(true);
        setCanRedo(false);
      }
      isUndoRedoRef.current = false;

      // optimistic update - instant UI feedback
      setSettings(updated);
      // Instantly push to global context so sidebar/header update immediately.
      // Preserve the user's personal theme (org edits shouldn't flip the theme).
      globalPersonalization.applySettingsOptimistically({
        ...updated,
        theme: globalPersonalization.settings.theme,
      });

      // clear any pending save timer
      if (saveTimerRef.current) {
        clearTimeout(saveTimerRef.current);
        saveTimerRef.current = null;
      }

      // flush function that persists to API
      const flush = async (toSave: PersonalizationSettingsType) => {
        setIsSaving(true);
        try {
          await api.organizations.updatePreferences(orgId, toSave);
          void reconcileMutationImpact(queryClient, "organization.preferences").catch(
            () => undefined,
          );
          // After successful save, refresh global context so the cookie
          // (FOUC hint) and any other consumers stay up-to-date.
          // This is a silent refresh - no loading flash.
          globalPersonalization.refreshSettings();
        } catch {
          toast({
            title: "Failed to save branding",
            description: "Could not save organization preferences.",
            variant: "destructive",
          });
        } finally {
          setIsSaving(false);
        }
      };

      if (immediate) {
        // undo/redo: save to API right away (no debounce for discrete actions)
        pendingSettingsRef.current = null;
        await flush(updated);
      } else {
        // normal edit: debounce 800ms for continuous interactions (sliders, color pickers)
        pendingSettingsRef.current = updated;
        saveTimerRef.current = setTimeout(async () => {
          saveTimerRef.current = null;
          const toSave = pendingSettingsRef.current;
          if (!toSave) return;
          pendingSettingsRef.current = null;
          await flush(toSave);
        }, 800);
      }
    },
    [orgId, globalPersonalization, queryClient],
  );

  // field-level updaters matching PersonalizationContextType
  const updateLogo = React.useCallback(
    (logo: Partial<LogoSettings>) =>
      saveSettings({ ...settings, logo: { ...settings.logo, ...logo } }),
    [settings, saveSettings],
  );
  const updateBackground = React.useCallback(
    (bg: Partial<BackgroundSettings>) =>
      saveSettings({ ...settings, background: { ...settings.background, ...bg } }),
    [settings, saveSettings],
  );
  const updateBranding = React.useCallback(
    (b: Partial<BrandingSettings>) =>
      saveSettings({ ...settings, branding: { ...settings.branding, ...b } }),
    [settings, saveSettings],
  );
  const updateColors = React.useCallback(
    (c: Partial<ColorSettings>) =>
      saveSettings({ ...settings, colors: { ...settings.colors, ...c } }),
    [settings, saveSettings],
  );
  const updateTheme = React.useCallback(
    (theme: ThemePreference) => saveSettings({ ...settings, theme }),
    [settings, saveSettings],
  );

  const resetToDefaults = React.useCallback(async () => {
    if (!orgId) return;

    // Push current settings onto undo stack so user can undo the reset
    undoStackRef.current = [...undoStackRef.current, { ...settingsRef.current }];
    if (undoStackRef.current.length > MAX_HISTORY) {
      undoStackRef.current = undoStackRef.current.slice(-MAX_HISTORY);
    }
    redoStackRef.current = [];
    setCanUndo(true);
    setCanRedo(false);

    setSettings(DEFAULT_PERSONALIZATION);
    // push global context to defaults (preserve user theme)
    globalPersonalization.applySettingsOptimistically({
      ...DEFAULT_PERSONALIZATION,
      theme: globalPersonalization.settings.theme,
    });
    try {
      await api.organizations.deletePreferences(orgId);
      void reconcileMutationImpact(queryClient, "organization.preferences").catch(
        () => undefined,
      );
      toast({ title: "Organization branding reset to defaults" });
      // refresh global sidebar/header after a short delay
      setTimeout(() => globalPersonalization.refreshSettings(), 300);
    } catch {
      toast({
        title: "Failed to reset branding",
        variant: "destructive",
      });
    }
  }, [orgId, queryClient]);

  const refreshSettings = React.useCallback(async () => {
    if (!orgId) return;
    setIsLoading(true);
    await loadPreferences(orgId);
    setIsLoading(false);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId]);

  // Undo: pop from undo stack, push current onto redo stack
  const undoSettings = React.useCallback(() => {
    const stack = undoStackRef.current;
    if (stack.length === 0) return;

    // Push current state onto redo stack
    redoStackRef.current = [...redoStackRef.current, { ...settingsRef.current }];

    // Pop last state from undo stack
    const prev = stack[stack.length - 1];
    undoStackRef.current = stack.slice(0, -1);

    // Apply without adding to undo stack - immediate save (no debounce)
    isUndoRedoRef.current = true;
    saveSettings(prev, { immediate: true });
    setCanUndo(undoStackRef.current.length > 0);
    setCanRedo(true);
  }, [saveSettings]);

  // Redo: pop from redo stack, push current onto undo stack
  const redoSettings = React.useCallback(() => {
    const stack = redoStackRef.current;
    if (stack.length === 0) return;

    // Push current state onto undo stack
    undoStackRef.current = [...undoStackRef.current, { ...settingsRef.current }];

    // Pop last state from redo stack
    const next = stack[stack.length - 1];
    redoStackRef.current = stack.slice(0, -1);

    // Apply without adding to undo stack - immediate save (no debounce)
    isUndoRedoRef.current = true;
    saveSettings(next, { immediate: true });
    setCanUndo(true);
    setCanRedo(redoStackRef.current.length > 0);
  }, [saveSettings]);

  // No-op for org context (org doesn't need external apply)
  const applySettingsOptimistically = React.useCallback((newSettings: PersonalizationSettingsType) => {
    setSettings(newSettings);
  }, []);

  // build context value that PersonalizationSettings will consume
  const ctxValue: PersonalizationContextType = React.useMemo(
    () => ({
      settings,
      updateLogo,
      updateBackground,
      updateBranding,
      updateColors,
      updateTheme,
      resetToDefaults,
      undoSettings,
      redoSettings,
      canUndo,
      canRedo,
      applySettingsOptimistically,
      refreshSettings,
      isLoading,
      isSaving,
    }),
    [
      settings,
      updateLogo,
      updateBackground,
      updateBranding,
      updateColors,
      updateTheme,
      resetToDefaults,
      undoSettings,
      redoSettings,
      canUndo,
      canRedo,
      applySettingsOptimistically,
      refreshSettings,
      isLoading,
      isSaving,
    ],
  );

  if (noOrg) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <AlertCircle className="h-5 w-5 text-muted-foreground" />
            No organization
          </CardTitle>
          <CardDescription>
            You are not a member of any organization. Create or join one first to
            manage organization branding.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const selectedOrg = orgs.find((o) => o.id === orgId);
  // An Admin's list is every active organization, so "selected" and "yours" are different
  // questions. is_member is the server's answer to the second one.
  const foreignOrg = selectedOrg !== undefined && !selectedOrg.isMember;

  return (
    <div className="space-y-4">
      {/* Org selector (if multiple orgs) */}
      {orgs.length > 1 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Building2 className="h-4 w-4" />
              Organization
            </CardTitle>
          </CardHeader>
          <CardContent>
            <Select
              value={orgId ?? undefined}
              onValueChange={handleOrgChange}
            >
              <SelectTrigger className="w-[280px]">
                <SelectValue placeholder="Select organization" />
              </SelectTrigger>
              <SelectContent>
                {orgs.map((o) => (
                  <SelectItem key={o.id} value={String(o.id)}>
                    {o.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </CardContent>
        </Card>
      )}

      {/* Which organization every control below writes to.
          Sticky on purpose. This is the only thing standing between an operator and rewriting a
          customer's branding, and the controls that do the writing are a scroll away, which is
          exactly how an operator came to change ALIMRUGI's colours while watching their own.
          Offset by the app header, which is sticky at h-14 on mobile and h-16 from md. */}
      <div
        className={cn(
          "sticky top-14 md:top-16 z-20 flex items-start gap-2 rounded-md border px-4 py-3 text-sm",
          foreignOrg
            ? "border-warning bg-warning/10 text-foreground"
            : "bg-muted/40 text-muted-foreground",
        )}
      >
        {foreignOrg ? (
          <ShieldAlert className="h-4 w-4 shrink-0 mt-0.5 text-warning" aria-hidden="true" />
        ) : (
          <Building2 className="h-4 w-4 shrink-0 mt-0.5" aria-hidden="true" />
        )}
        <span>
          Changes here apply to <strong>all users</strong> in{" "}
          <Badge variant="outline" className="mx-1">
            {selectedOrg?.name ?? "this organization"}
          </Badge>
          {foreignOrg ? (
            <>
              . You are not a member of this organization, so this is someone else&apos;s
              branding and not the one your own sidebar shows.
            </>
          ) : null}
        </span>
      </div>

      {/* Re-use the existing PersonalizationSettings component,
          but with our org-backed context */}
      <PersonalizationContext.Provider value={ctxValue}>
        <PersonalizationSettings />
      </PersonalizationContext.Provider>
    </div>
  );
}
