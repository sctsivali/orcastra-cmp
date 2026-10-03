"use client";

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { onAuthActorChanged } from "@/lib/api";
import { toast } from "@/hooks/use-toast";
import { captureActorMutationFence, dispatchActorMutation, isActorMutationFenceCurrent, type ActorMutationFence } from "@/lib/actor-mutation-fence";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { personalizationAPI } from "@/lib/personalization-api";
import { DEFAULT_PERSONALIZATION, type PersonalizationSettings, type LogoSettings, type BackgroundSettings, type BrandingSettings, type ColorSettings, type ThemePreference } from "@/types/personalization";

interface MutationOptions {
  userId: string | null;
  userEmail?: string;
  accessToken?: string;
  scopeKey: string;
  settings: PersonalizationSettings;
  setSettings: React.Dispatch<React.SetStateAction<PersonalizationSettings>>;
  personalRowRef: React.MutableRefObject<PersonalizationSettings | null>;
  orgBackedRef: React.MutableRefObject<boolean>;
  lastLocalEditRef: React.MutableRefObject<number>;
}

export function usePersonalizationMutations({
  userId, userEmail, accessToken, scopeKey, settings, setSettings,
  personalRowRef, orgBackedRef, lastLocalEditRef,
}: MutationOptions) {
  const queryClient = useQueryClient();
  const [isSaving, setIsSaving] = React.useState(false);
  const saveTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSettingsRef = React.useRef<PersonalizationSettings | null>(null);
  const settingsRef = React.useRef(settings);
  const undoStackRef = React.useRef<PersonalizationSettings[]>([]);
  const redoStackRef = React.useRef<PersonalizationSettings[]>([]);
  const isUndoRedoRef = React.useRef(false);
  const MAX_HISTORY = 50;
  const [canUndo, setCanUndo] = React.useState(false);
  const [canRedo, setCanRedo] = React.useState(false);
  React.useEffect(() => { settingsRef.current = settings; }, [settings]);
  const clearPendingSave = React.useCallback(() => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = null;
    pendingSettingsRef.current = null;
  }, []);
  React.useEffect(() => {
    const unsubscribe = onAuthActorChanged(() => {
      clearPendingSave();
      undoStackRef.current = [];
      redoStackRef.current = [];
      setCanUndo(false);
      setCanRedo(false);
      setIsSaving(false);
    });
    return () => { unsubscribe(); clearPendingSave(); };
  }, [clearPendingSave]);
  /** Save settings once. An unknown write outcome is surfaced instead of replayed. */
  const saveToAPI = React.useCallback(async (
    _userId: string,
    _userEmail: string | undefined,
    newSettings: PersonalizationSettings,
    accessToken: string | undefined,
    actorFence: ActorMutationFence,
  ): Promise<boolean> => {
    try {
      const outcome = await dispatchActorMutation(
        actorFence,
        () => personalizationAPI.update(newSettings, accessToken),
      );
      if (outcome.retired) return true;
      await reconcileMutationImpact(queryClient, "personalization.write");
      return true;
    } catch {
      if (!isActorMutationFenceCurrent(actorFence)) return true;
      // A write whose outcome is unknown must not be repeated automatically.
      return false;
    }
  }, [queryClient]);

  /**
   * Delete settings from API (reset to defaults)
   */
  const deleteFromAPI = React.useCallback(async (
    _userId: string,
    _userEmail: string | undefined,
    accessToken: string | undefined,
    actorFence: ActorMutationFence,
  ): Promise<boolean> => {
    try {
      const outcome = await dispatchActorMutation(
        actorFence,
        () => personalizationAPI.delete(accessToken),
      );
      if (outcome.retired) return true;
      await reconcileMutationImpact(queryClient, "personalization.write");
      return true;
    } catch {
      if (!isActorMutationFenceCurrent(actorFence)) return true;
      return false;
    }
  }, [queryClient]);

  /**
   * Save settings - debounced to prevent API spam on rapid edits.
   * Pass immediate=true for discrete actions (undo/redo) to skip debounce.
   */
  const saveSettings = React.useCallback(async (
    newSettings: PersonalizationSettings,
    { immediate = false }: { immediate?: boolean } = {},
  ) => {
    const actorId = userId;
    const actorEmail = userEmail ?? undefined;
    const actorFence = captureActorMutationFence(scopeKey);

    if (!actorId) {
      toast({
        title: "Cannot save settings",
        description: "Please sign in to save your personalization settings.",
        variant: "destructive",
      });
      return;
    }
    if (!isActorMutationFenceCurrent(actorFence)) return;

    const updatedSettings: PersonalizationSettings = {
      ...newSettings,
      lastModified: new Date().toISOString(),
    };

    // Optimistic update - instant UI feedback
    // Only push to undo stack at the START of a new edit sequence
    // (when no debounce timer is pending). This prevents slider drags
    // from overwriting the original value on every intermediate tick.
    // ex; dragging 50% → 60% should undo to 50%, not 59%.
    if (!saveTimerRef.current && !isUndoRedoRef.current) {
      // Push current state onto undo stack
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
    lastLocalEditRef.current += 1;
    setSettings(updatedSettings);

    // Clear any pending save timer
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }

    // flush function that persists to API
    const flush = async (toSave: PersonalizationSettings) => {
      if (!isActorMutationFenceCurrent(actorFence)) return;
      setIsSaving(true);
      const success = await saveToAPI(
        actorId,
        actorEmail,
        toSave,
        accessToken,
        actorFence,
      );
      if (!isActorMutationFenceCurrent(actorFence)) return;
      setIsSaving(false);
      if (!success) {
        toast({
          title: "Failed to save settings",
          description: "Your changes could not be saved. Please try again.",
          variant: "destructive",
        });
      }
    };

    if (immediate) {
      // undo/redo: save right away (no debounce for discrete actions)
      pendingSettingsRef.current = null;
      await flush(updatedSettings);
    } else {
      // normal edit: debounce 800ms for continuous interactions (sliders, color pickers)
      pendingSettingsRef.current = updatedSettings;
      saveTimerRef.current = setTimeout(async () => {
        saveTimerRef.current = null;
        const toSave = pendingSettingsRef.current;
        if (!toSave) return;
        pendingSettingsRef.current = null;
        await flush(toSave);
      }, 800);
    }
  }, [userId, userEmail, accessToken, scopeKey, saveToAPI]);

  const updateLogo = React.useCallback((logo: Partial<LogoSettings>) => {
    saveSettings({
      ...settings,
      logo: { ...settings.logo, ...logo },
    });
  }, [settings, saveSettings]);

  const updateBackground = React.useCallback((background: Partial<BackgroundSettings>) => {
    saveSettings({
      ...settings,
      background: { ...settings.background, ...background },
    });
  }, [settings, saveSettings]);

  const updateBranding = React.useCallback((branding: Partial<BrandingSettings>) => {
    saveSettings({
      ...settings,
      branding: { ...settings.branding, ...branding },
    });
  }, [settings, saveSettings]);

  const updateColors = React.useCallback((colors: Partial<ColorSettings>) => {
    saveSettings({
      ...settings,
      colors: { ...settings.colors, ...colors },
    });
  }, [settings, saveSettings]);

  /**
   * Persist a theme choice into the personal row and nothing else.
   *
   * For a Partner or Tenant `settings` is the ORGANIZATION's branding with a personal theme laid
   * over it. Rendering that is correct; saving it is not. updateTheme used to hand the whole
   * merged object to saveSettings, so picking Dark stamped the organization's colours into a row
   * that is only ever read for `theme`. It never showed on screen, and it made the table say the
   * operator had chosen colours they had never seen a picker for, which is exactly the evidence
   * somebody debugging a colour problem reaches for first.
   */
  const persistPersonalTheme = React.useCallback(async (theme: ThemePreference) => {
    const actorId = userId;
    const actorFence = captureActorMutationFence(scopeKey);
    if (!actorId) {
      toast({
        title: "Cannot save settings",
        description: "Please sign in to save your personalization settings.",
        variant: "destructive",
      });
      return;
    }
    if (!isActorMutationFenceCurrent(actorFence)) return;
    const personal: PersonalizationSettings = {
      ...(personalRowRef.current ?? DEFAULT_PERSONALIZATION),
      theme,
      // important: the loader honours a personal theme only when the row carries a lastModified,
      // which is how "never chose a theme" is told apart from "chose System". Dropping it here
      // would make the choice survive the session and vanish on the next load.
      lastModified: new Date().toISOString(),
    };
    personalRowRef.current = personal;
    lastLocalEditRef.current += 1;
    if (!isActorMutationFenceCurrent(actorFence)) return;
    setIsSaving(true);
    const ok = await saveToAPI(
      actorId,
      userEmail ?? undefined,
      personal,
      accessToken,
      actorFence,
    );
    if (!isActorMutationFenceCurrent(actorFence)) return;
    setIsSaving(false);
    if (!ok) {
      toast({
        title: "Failed to save settings",
        description: "Your changes could not be saved. Please try again.",
        variant: "destructive",
      });
    }
  }, [userId, userEmail, accessToken, scopeKey, saveToAPI]);

  const updateTheme = React.useCallback((theme: ThemePreference) => {
    if (!isActorMutationFenceCurrent(captureActorMutationFence(scopeKey))) return;
    if (!orgBackedRef.current) {
      // Admin without Partner: `settings` IS the personal row, so saving all of it is right, and
      // the theme belongs on the branding undo stack with everything else on that path.
      saveSettings({
        ...settings,
        theme,
      });
      return;
    }
    // Render the organization's branding under the new theme, persist only the theme. Not pushed
    // onto the undo stack on purpose: that stack is the branding editor's, and undoing a colour
    // edit should not flip the operator's theme back.
    setSettings((prev) => ({ ...prev, theme }));
    void persistPersonalTheme(theme);
  }, [settings, saveSettings, persistPersonalTheme, scopeKey]);

  const resetToDefaults = React.useCallback(async () => {
    const actorId = userId;
    const actorEmail = userEmail ?? undefined;
    const actorFence = captureActorMutationFence(scopeKey);
    if (!isActorMutationFenceCurrent(actorFence)) return;

    // Push current settings onto undo stack so user can undo the reset
    undoStackRef.current = [...undoStackRef.current, { ...settingsRef.current }];
    if (undoStackRef.current.length > MAX_HISTORY) {
      undoStackRef.current = undoStackRef.current.slice(-MAX_HISTORY);
    }
    redoStackRef.current = [];
    setCanUndo(true);
    setCanRedo(false);

    // Reset UI immediately
    lastLocalEditRef.current += 1;
    setSettings(DEFAULT_PERSONALIZATION);
    // The row is about to be deleted, so the next theme save must not rebuild it from a stale
    // copy of what it used to hold.
    personalRowRef.current = null;

    if (actorId) {
      const success = await deleteFromAPI(
        actorId,
        actorEmail,
        accessToken,
        actorFence,
      );
      if (!isActorMutationFenceCurrent(actorFence)) return;
      if (success) {
        toast({
          title: "Settings reset",
          description: "Your personalization has been reset to defaults.",
        });
      } else {
        toast({
          title: "Failed to reset settings",
          description: "Could not reset settings on server. Please try again.",
          variant: "destructive",
        });
      }
    }
  }, [userId, userEmail, accessToken, scopeKey, deleteFromAPI]);

  // Undo: pop from undo stack, push current onto redo stack
  const undoSettings = React.useCallback(() => {
    const stack = undoStackRef.current;
    if (stack.length === 0) return;

    // Push current state onto redo stack
    redoStackRef.current = [...redoStackRef.current, { ...settingsRef.current }];

    // Pop last state from undo stack
    const prev = stack[stack.length - 1];
    undoStackRef.current = stack.slice(0, -1);

    // Apply the undo without adding to undo stack - immediate save
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

    // Apply the redo without adding to undo stack - immediate save
    isUndoRedoRef.current = true;
    saveSettings(next, { immediate: true });
    setCanUndo(true);
    setCanRedo(redoStackRef.current.length > 0);
  }, [saveSettings]);

  // Force-apply settings without API save (for org-level instant preview in sidebar/header)
  const applySettingsOptimistically = React.useCallback((newSettings: PersonalizationSettings) => {
    if (!isActorMutationFenceCurrent(captureActorMutationFence(scopeKey))) return;
    lastLocalEditRef.current += 1;
    setSettings(newSettings);
  }, [lastLocalEditRef, setSettings, scopeKey]);

  return {
    updateLogo, updateBackground, updateBranding, updateColors, updateTheme,
    resetToDefaults, undoSettings, redoSettings, canUndo, canRedo,
    applySettingsOptimistically, isSaving,
  };
}
