"use client";

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { onAuthActorChanged } from "@/lib/api";
import { toast } from "@/hooks/use-toast";
import { captureActorMutationFence, dispatchActorMutation, isActorMutationFenceCurrent, type ActorMutationFence } from "@/lib/actor-mutation-fence";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { personalizationAPI } from "@/lib/personalization-api";
import type { ByteUnitSystem } from "@/lib/byte-units";
import { type PersonalizationSettings, type ThemePreference } from "@/types/personalization";
import { announcePersonalWrite } from "./preferences";
import type { ThemeOrigin } from "./use-personalization-sync";

interface MutationOptions {
  userId: string | null;
  scopeKey: string;
  setSettings: React.Dispatch<React.SetStateAction<PersonalizationSettings>>;
  themeOrigin: ThemeOrigin;
  setThemeOrigin: React.Dispatch<React.SetStateAction<ThemeOrigin>>;
  byteUnits: ByteUnitSystem;
  setByteUnits: React.Dispatch<React.SetStateAction<ByteUnitSystem>>;
  lastLocalEditRef: React.MutableRefObject<number>;
}

/**
 * What an account writes for itself: its theme and its byte units. Branding is written by the
 * branding editor to an organization's row or the deployment default
 * (components/org-personalization-settings.tsx), and the server keeps nothing but these two
 * choices in a personal row (routers/user_preferences.py).
 */
export function usePersonalizationMutations({
  userId, scopeKey, setSettings, themeOrigin, setThemeOrigin, byteUnits, setByteUnits, lastLocalEditRef,
}: MutationOptions) {
  const queryClient = useQueryClient();
  const [isSaving, setIsSaving] = React.useState(false);
  const themeOriginRef = React.useRef(themeOrigin);
  themeOriginRef.current = themeOrigin;
  const byteUnitsRef = React.useRef(byteUnits);
  byteUnitsRef.current = byteUnits;
  React.useEffect(() => onAuthActorChanged(() => setIsSaving(false)), []);

  /** Save once. An unknown write outcome is surfaced instead of replayed. */
  const saveToAPI = React.useCallback(async (
    body: Parameters<typeof personalizationAPI.update>[0],
    actorFence: ActorMutationFence,
  ): Promise<boolean> => {
    try {
      const outcome = await dispatchActorMutation(
        actorFence,
        () => personalizationAPI.update(body),
      );
      if (outcome.retired) return true;
      await reconcileMutationImpact(queryClient, "personalization.write");
      announcePersonalWrite(scopeKey);
      return true;
    } catch {
      if (!isActorMutationFenceCurrent(actorFence)) return true;
      // A write whose outcome is unknown must not be repeated automatically.
      return false;
    }
  }, [queryClient, scopeKey]);

  const persistTheme = React.useCallback(async (theme: ThemePreference | null) => {
    const actorFence = captureActorMutationFence(scopeKey);
    if (!userId) {
      toast({
        title: "Could not save settings",
        description: "Sign in to save your theme.",
        variant: "destructive",
      });
      return;
    }
    if (!isActorMutationFenceCurrent(actorFence)) return;
    const previous = themeOriginRef.current;
    lastLocalEditRef.current += 1;
    setThemeOrigin({ ...previous, personal: theme !== null });
    setSettings((current) => ({ ...current, theme: theme ?? previous.organizationTheme }));
    setIsSaving(true);
    const ok = await saveToAPI({ theme }, actorFence);
    if (!isActorMutationFenceCurrent(actorFence)) return;
    setIsSaving(false);
    if (!ok) {
      toast({
        title: "Could not save theme",
        description: "Your choice could not be saved. Try again.",
        variant: "destructive",
      });
    }
  }, [userId, scopeKey, saveToAPI, setSettings, setThemeOrigin, lastLocalEditRef]);

  const updateTheme = React.useCallback((theme: ThemePreference) => {
    void persistTheme(theme);
  }, [persistTheme]);

  const clearTheme = React.useCallback(() => {
    void persistTheme(null);
  }, [persistTheme]);

  const updateByteUnits = React.useCallback(async (units: ByteUnitSystem) => {
    const actorFence = captureActorMutationFence(scopeKey);
    if (!userId || !isActorMutationFenceCurrent(actorFence)) return;
    const previous = byteUnitsRef.current;
    if (previous === units) return;
    lastLocalEditRef.current += 1;
    setByteUnits(units);
    setIsSaving(true);
    const ok = await saveToAPI({ byteUnits: units }, actorFence);
    if (!isActorMutationFenceCurrent(actorFence)) return;
    setIsSaving(false);
    if (!ok) {
      // The screen goes back to what is stored, so it never shows a choice that was not saved.
      lastLocalEditRef.current += 1;
      setByteUnits(previous);
      toast({
        title: "Could not save units",
        description: "Sizes stay in the units you had. Try again.",
        variant: "destructive",
      });
    }
  }, [userId, scopeKey, saveToAPI, setByteUnits, lastLocalEditRef]);

  // The branding editor's live preview of the organization the sidebar renders.
  const applySettingsOptimistically = React.useCallback((newSettings: PersonalizationSettings) => {
    if (!isActorMutationFenceCurrent(captureActorMutationFence(scopeKey))) return;
    lastLocalEditRef.current += 1;
    setSettings(newSettings);
  }, [lastLocalEditRef, setSettings, scopeKey]);

  return {
    updateTheme,
    clearTheme,
    updateByteUnits: React.useCallback((units: ByteUnitSystem) => void updateByteUnits(units), [updateByteUnits]),
    applySettingsOptimistically,
    isSaving,
  };
}
