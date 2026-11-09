"use client";

import * as React from "react";
import { toast } from "@/hooks/use-toast";
import { APIError } from "@/types/api";
import {
  DEFAULT_PERSONALIZATION,
  type BackgroundSettings,
  type BrandingEditorContextType,
  type BrandingSettings,
  type ColorSettings,
  type IllustrationPreference,
  type LogoSettings,
  type PersonalizationSettings,
  type ThemePreference,
} from "@/types/personalization";

const MAX_HISTORY = 50;
const SAVE_DEBOUNCE_MS = 800;

interface EditorOptions {
  /** A key per target; a change discards the history and any pending save. */
  targetKey: string | null;
  settings: PersonalizationSettings;
  setSettings: React.Dispatch<React.SetStateAction<PersonalizationSettings>>;
  isLoading: boolean;
  resetLabel: string;
  persist: (settings: PersonalizationSettings) => Promise<void>;
  remove: () => Promise<void>;
  /** Called with every edit, before it is saved (the sidebar preview). */
  onEdit?: (settings: PersonalizationSettings) => void;
}

function failureText(error: unknown): string {
  // A 422 names the field the server refused (backend services/branding/validation.py).
  if (error instanceof APIError && typeof error.detail === "string") return error.detail;
  return "The branding could not be saved. Try again.";
}

/** Debounced saves, undo and redo for whichever branding row the editor targets. */
export function useBrandingEditorState({
  targetKey, settings, setSettings, isLoading, resetLabel, persist, remove, onEdit,
}: EditorOptions): BrandingEditorContextType {
  const [isSaving, setIsSaving] = React.useState(false);
  const [canUndo, setCanUndo] = React.useState(false);
  const [canRedo, setCanRedo] = React.useState(false);
  const settingsRef = React.useRef(settings);
  settingsRef.current = settings;
  const undoStackRef = React.useRef<PersonalizationSettings[]>([]);
  const redoStackRef = React.useRef<PersonalizationSettings[]>([]);
  const isUndoRedoRef = React.useRef(false);
  const saveTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = React.useRef<PersonalizationSettings | null>(null);
  const persistRef = React.useRef(persist);
  persistRef.current = persist;

  React.useEffect(() => {
    undoStackRef.current = [];
    redoStackRef.current = [];
    setCanUndo(false);
    setCanRedo(false);
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
      pendingRef.current = null;
    };
  }, [targetKey]);

  const flush = React.useCallback(async (toSave: PersonalizationSettings) => {
    setIsSaving(true);
    try {
      await persistRef.current(toSave);
    } catch (error) {
      toast({ title: "Could not save branding", description: failureText(error), variant: "destructive" });
    } finally {
      setIsSaving(false);
    }
  }, []);

  const save = React.useCallback(async (next: PersonalizationSettings, { immediate = false } = {}) => {
    if (!targetKey) return;
    const updated = { ...next, lastModified: new Date().toISOString() };
    // Only the start of an edit sequence is an undo step, so a slider drag undoes to where it began.
    if (!saveTimerRef.current && !isUndoRedoRef.current) {
      undoStackRef.current = [...undoStackRef.current, { ...settingsRef.current }].slice(-MAX_HISTORY);
      redoStackRef.current = [];
      setCanUndo(true);
      setCanRedo(false);
    }
    isUndoRedoRef.current = false;
    setSettings(updated);
    onEdit?.(updated);
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = null;
    if (immediate) {
      pendingRef.current = null;
      await flush(updated);
      return;
    }
    pendingRef.current = updated;
    saveTimerRef.current = setTimeout(() => {
      saveTimerRef.current = null;
      const toSave = pendingRef.current;
      pendingRef.current = null;
      if (toSave) void flush(toSave);
    }, SAVE_DEBOUNCE_MS);
  }, [targetKey, setSettings, onEdit, flush]);

  const updateLogo = React.useCallback((logo: Partial<LogoSettings>) =>
    void save({ ...settings, logo: { ...settings.logo, ...logo } }), [settings, save]);
  const updateBackground = React.useCallback((background: Partial<BackgroundSettings>) =>
    void save({ ...settings, background: { ...settings.background, ...background } }), [settings, save]);
  const updateBranding = React.useCallback((branding: Partial<BrandingSettings>) =>
    void save({ ...settings, branding: { ...settings.branding, ...branding } }), [settings, save]);
  const updateColors = React.useCallback((colors: Partial<ColorSettings>) =>
    void save({ ...settings, colors: { ...settings.colors, ...colors } }), [settings, save]);
  const updateDefaultTheme = React.useCallback((theme: ThemePreference) =>
    void save({ ...settings, theme }, { immediate: true }), [settings, save]);
  const updateDefaultIllustrations = React.useCallback((illustrations: IllustrationPreference) =>
    void save({ ...settings, illustrations }, { immediate: true }), [settings, save]);

  const resetToDefaults = React.useCallback(async () => {
    if (!targetKey) return;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = null;
    pendingRef.current = null;
    undoStackRef.current = [];
    redoStackRef.current = [];
    setCanUndo(false);
    setCanRedo(false);
    setIsSaving(true);
    try {
      await remove();
      toast({ title: "Branding removed", description: "What shows now is the branding this one inherits." });
    } catch (error) {
      toast({ title: "Could not reset branding", description: failureText(error), variant: "destructive" });
    } finally {
      setIsSaving(false);
    }
  }, [targetKey, remove]);

  const step = React.useCallback((from: React.MutableRefObject<PersonalizationSettings[]>,
    to: React.MutableRefObject<PersonalizationSettings[]>) => {
    const stack = from.current;
    if (stack.length === 0) return;
    to.current = [...to.current, { ...settingsRef.current }];
    from.current = stack.slice(0, -1);
    isUndoRedoRef.current = true;
    void save(stack[stack.length - 1], { immediate: true });
    setCanUndo(undoStackRef.current.length > 0);
    setCanRedo(redoStackRef.current.length > 0);
  }, [save]);

  return {
    settings: settings ?? DEFAULT_PERSONALIZATION,
    updateLogo, updateBackground, updateBranding, updateColors, updateDefaultTheme, updateDefaultIllustrations,
    resetToDefaults, resetLabel,
    undoSettings: () => step(undoStackRef, redoStackRef),
    redoSettings: () => step(redoStackRef, undoStackRef),
    canUndo, canRedo, isLoading, isSaving,
  };
}
