"use client";

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { onAuthActorChanged } from "@/lib/api";
import { toast } from "@/hooks/use-toast";
import { captureActorMutationFence, dispatchActorMutation, isActorMutationFenceCurrent, type ActorMutationFence } from "@/lib/actor-mutation-fence";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { personalizationAPI } from "@/lib/personalization-api";
import { type IllustrationPreference, type PersonalizationSettings, type ThemePreference } from "@/types/personalization";
import type { PreferenceOrigin } from "./use-personalization-sync";

interface MutationOptions {
  userId: string | null;
  accessToken?: string;
  scopeKey: string;
  setSettings: React.Dispatch<React.SetStateAction<PersonalizationSettings>>;
  origin: PreferenceOrigin;
  setOrigin: React.Dispatch<React.SetStateAction<PreferenceOrigin>>;
  lastLocalEditRef: React.MutableRefObject<number>;
}

type PersonalWrite = { theme?: ThemePreference | null; illustrations?: IllustrationPreference | null };

/**
 * What an account writes for itself: its theme and whether cards show their illustration. Branding
 * is written by the branding editor to an organization's row or the deployment default
 * (components/org-personalization-settings.tsx), and the server keeps nothing else in a personal
 * row (routers/user_preferences.py).
 */
export function usePersonalizationMutations({
  userId, accessToken, scopeKey, setSettings, origin, setOrigin, lastLocalEditRef,
}: MutationOptions) {
  const queryClient = useQueryClient();
  const [isSaving, setIsSaving] = React.useState(false);
  const originRef = React.useRef(origin);
  originRef.current = origin;
  React.useEffect(() => onAuthActorChanged(() => setIsSaving(false)), []);

  /** Save once. An unknown write outcome is surfaced instead of replayed. */
  const saveToAPI = React.useCallback(async (
    body: PersonalWrite,
    actorFence: ActorMutationFence,
  ): Promise<boolean> => {
    try {
      const outcome = await dispatchActorMutation(
        actorFence,
        () => personalizationAPI.update(body, accessToken),
      );
      if (outcome.retired) return true;
      await reconcileMutationImpact(queryClient, "personalization.write");
      return true;
    } catch {
      if (!isActorMutationFenceCurrent(actorFence)) return true;
      // A write whose outcome is unknown must not be repeated automatically.
      return false;
    }
  }, [accessToken, queryClient]);

  // One path for both choices: show the new value at once, write it once, and say so if the
  // write failed. `apply` derives the optimistic origin and settings from the origin before it.
  const persist = React.useCallback(async (
    body: PersonalWrite,
    apply: (previous: PreferenceOrigin) => { origin: PreferenceOrigin; patch: Partial<PersonalizationSettings> },
    failure: { title: string; signIn: string },
  ) => {
    const actorFence = captureActorMutationFence(scopeKey);
    if (!userId) {
      toast({ title: "Could not save settings", description: failure.signIn, variant: "destructive" });
      return;
    }
    if (!isActorMutationFenceCurrent(actorFence)) return;
    const next = apply(originRef.current);
    lastLocalEditRef.current += 1;
    setOrigin(next.origin);
    setSettings((current) => ({ ...current, ...next.patch }));
    setIsSaving(true);
    const ok = await saveToAPI(body, actorFence);
    if (!isActorMutationFenceCurrent(actorFence)) return;
    setIsSaving(false);
    if (!ok) {
      toast({ title: failure.title, description: "Your choice could not be saved. Try again.", variant: "destructive" });
    }
  }, [userId, scopeKey, saveToAPI, setSettings, setOrigin, lastLocalEditRef]);

  const persistTheme = React.useCallback((theme: ThemePreference | null) => persist(
    { theme },
    (previous) => ({
      origin: { ...previous, personal: theme !== null },
      patch: { theme: theme ?? previous.organizationTheme },
    }),
    { title: "Could not save theme", signIn: "Sign in to save your theme." },
  ), [persist]);

  const persistIllustrations = React.useCallback((choice: IllustrationPreference | null) => persist(
    { illustrations: choice },
    (previous) => ({
      origin: { ...previous, illustrationsPersonal: choice !== null },
      patch: { illustrations: choice ?? previous.organizationIllustrations },
    }),
    { title: "Could not save card illustrations", signIn: "Sign in to save your card illustrations choice." },
  ), [persist]);

  const updateTheme = React.useCallback((theme: ThemePreference) => {
    void persistTheme(theme);
  }, [persistTheme]);

  const clearTheme = React.useCallback(() => {
    void persistTheme(null);
  }, [persistTheme]);

  const updateIllustrations = React.useCallback((choice: IllustrationPreference) => {
    void persistIllustrations(choice);
  }, [persistIllustrations]);

  const clearIllustrations = React.useCallback(() => {
    void persistIllustrations(null);
  }, [persistIllustrations]);

  // The branding editor's live preview of the organization the sidebar renders.
  const applySettingsOptimistically = React.useCallback((newSettings: PersonalizationSettings) => {
    if (!isActorMutationFenceCurrent(captureActorMutationFence(scopeKey))) return;
    lastLocalEditRef.current += 1;
    setSettings(newSettings);
  }, [lastLocalEditRef, setSettings, scopeKey]);

  return { updateTheme, clearTheme, updateIllustrations, clearIllustrations, applySettingsOptimistically, isSaving };
}
