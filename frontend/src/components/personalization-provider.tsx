"use client";

/**
 * Personalization Provider
 * 
 * Manages user-specific dashboard customization settings.
 * 
 * Architecture:
 * - PostgreSQL via API is the SINGLE SOURCE OF TRUTH
 * - No localStorage caching (ensures cross-browser/device consistency)
 * - Settings persist across browsers, devices, and cache clears
 * - Clean error handling with user-facing toast notifications
 * 
 * Security:
 * - Settings are user-specific (keyed by OIDC subject claim)
 * - Unauthenticated users always see system defaults
 * - No sensitive data stored client-side
 */

import * as React from "react";
import { useSession } from "next-auth/react";
import {
  PersonalizationSettings,
  PersonalizationContextType,
  LogoSettings,
  BackgroundSettings,
  BrandingSettings,
  ColorSettings,
  ThemePreference,
  DEFAULT_PERSONALIZATION,
} from "@/types/personalization";
import { Role, resolveRoles } from "@/types/rbac";
import { toast } from "@/hooks/use-toast";

// API configuration - use relative URLs (Next.js rewrites proxy to backend)
const API_BASE = "";
const API_RETRY_COUNT = 2;
const API_RETRY_DELAY_MS = 500;

// Org preference polling interval (ms) for cross-session sync
const ORG_PREF_POLL_INTERVAL_MS = 45_000;

// Cookie name for personalization hint (visual CSS values for FOUC prevention)
const PERSONALIZATION_COOKIE = "orcastra-personalization";

// Create context (exported for OrgPersonalizationSettings wrapper)
export const PersonalizationContext = React.createContext<PersonalizationContextType | undefined>(
  undefined
);

// Helper to delay execution for retries
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

/**
 * Persist visual personalization hint in cookie for instant load (365 days).
 * Only stores CSS-relevant values - NOT images/logos (too large for cookies).
 */
function setPersonalizationCookie(settings: PersonalizationSettings) {
  if (typeof document === "undefined") return;
  try {
    const hint = {
      ac: settings.colors.accentColor,
      sb: settings.colors.sidebarBgColor,
      st: settings.colors.sidebarTextColor,
      uca: settings.colors.useCustomAccent,
      usb: settings.colors.useCustomSidebarBg,
      ust: settings.colors.useCustomSidebarText,
      atc: settings.colors.autoTextColor,
      slc: settings.colors.sidebarLogoColor,
      an: settings.branding.appName,
      th: settings.theme,
    };
    const encoded = encodeURIComponent(JSON.stringify(hint));
    document.cookie = `${PERSONALIZATION_COOKIE}=${encoded};path=/;max-age=${365 * 24 * 60 * 60};SameSite=Lax`;
  } catch {
    // cookie write failed - degrade gracefully
  }
}

/**
 * Read personalization hint from cookie (client-side only).
 * Returns partial settings merged with defaults for instant initial render.
 */
function readPersonalizationCookie(): PersonalizationSettings | null {
  if (typeof document === "undefined") return null;
  try {
    const match = document.cookie.match(new RegExp(`${PERSONALIZATION_COOKIE}=([^;]+)`));
    if (!match) return null;
    const hint = JSON.parse(decodeURIComponent(match[1]));
    return {
      ...DEFAULT_PERSONALIZATION,
      colors: {
        ...DEFAULT_PERSONALIZATION.colors,
        accentColor: hint.ac ?? DEFAULT_PERSONALIZATION.colors.accentColor,
        sidebarBgColor: hint.sb ?? DEFAULT_PERSONALIZATION.colors.sidebarBgColor,
        sidebarTextColor: hint.st ?? DEFAULT_PERSONALIZATION.colors.sidebarTextColor,
        useCustomAccent: hint.uca ?? DEFAULT_PERSONALIZATION.colors.useCustomAccent,
        useCustomSidebarBg: hint.usb ?? DEFAULT_PERSONALIZATION.colors.useCustomSidebarBg,
        useCustomSidebarText: hint.ust ?? DEFAULT_PERSONALIZATION.colors.useCustomSidebarText,
        autoTextColor: hint.atc ?? DEFAULT_PERSONALIZATION.colors.autoTextColor,
        sidebarLogoColor: hint.slc ?? DEFAULT_PERSONALIZATION.colors.sidebarLogoColor,
      },
      branding: {
        ...DEFAULT_PERSONALIZATION.branding,
        appName: hint.an ?? DEFAULT_PERSONALIZATION.branding.appName,
      },
      theme: hint.th ?? DEFAULT_PERSONALIZATION.theme,
    };
  } catch {
    return null;
  }
}

/**
 * Build headers for API requests
 */
function buildHeaders(userId?: string, userEmail?: string, accessToken?: string): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (accessToken) {
    headers["Authorization"] = `Bearer ${accessToken}`;
  }
  if (userId) {
    headers["X-User-ID"] = userId;
  }
  if (userEmail) {
    headers["X-User-Email"] = userEmail;
  }
  return headers;
}

/**
 * Merge API response with defaults to ensure all fields exist
 */
function mergeWithDefaults(data: Partial<PersonalizationSettings>): PersonalizationSettings {
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

export function PersonalizationProvider({ children }: { children: React.ReactNode }) {
  const { data: session, status } = useSession();
  // Initialize from cookie hint for instant render (prevents FOUC for colors/branding)
  const [settings, setSettings] = React.useState<PersonalizationSettings>(
    () => readPersonalizationCookie() ?? DEFAULT_PERSONALIZATION
  );
  const [isLoading, setIsLoading] = React.useState(true);
  const [isSaving, setIsSaving] = React.useState(false);
  
  // Track current user ID to detect user changes
  const currentUserIdRef = React.useRef<string | null>(null);
  // Debounce ref for save operations
  const saveTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSettingsRef = React.useRef<PersonalizationSettings | null>(null);
  // Always-current settings ref (avoids stale closure in saveSettings)
  const settingsRef = React.useRef<PersonalizationSettings>(
    readPersonalizationCookie() ?? DEFAULT_PERSONALIZATION
  );
  // Undo/Redo stacks
  const undoStackRef = React.useRef<PersonalizationSettings[]>([]);
  const redoStackRef = React.useRef<PersonalizationSettings[]>([]);
  const isUndoRedoRef = React.useRef(false);
  const MAX_HISTORY = 50;
  const [canUndo, setCanUndo] = React.useState(false);
  const [canRedo, setCanRedo] = React.useState(false);

  // Keep settingsRef always in sync with latest settings state
  React.useEffect(() => {
    settingsRef.current = settings;
  }, [settings]);

  /**
   * Fetch settings from API with retry mechanism
   */
  const fetchFromAPI = React.useCallback(async (
    userId: string, 
    userEmail?: string,
    accessToken?: string
  ): Promise<PersonalizationSettings | null> => {
    for (let attempt = 0; attempt <= API_RETRY_COUNT; attempt++) {
      try {
        if (attempt > 0) {
          await delay(API_RETRY_DELAY_MS * attempt);
        }
        
        const response = await fetch(`${API_BASE}/api/v1/user-preferences`, {
          method: "GET",
          headers: buildHeaders(userId, userEmail, accessToken),
          credentials: "include",
          cache: "no-store",
        });
        
        if (response.ok) {
          const data = await response.json();
          return mergeWithDefaults(data);
        }
        
        // Don't retry on client errors (4xx)
        if (response.status >= 400 && response.status < 500) {
          return null;
        }
      } catch {
        // Network error - will retry
      }
    }
    
    return null;
  }, []);

  /**
   * Save settings to API with retry mechanism
   */
  const saveToAPI = React.useCallback(async (
    userId: string, 
    userEmail: string | undefined,
    newSettings: PersonalizationSettings,
    accessToken?: string
  ): Promise<boolean> => {
    for (let attempt = 0; attempt <= API_RETRY_COUNT; attempt++) {
      try {
        if (attempt > 0) {
          await delay(API_RETRY_DELAY_MS * attempt);
        }
        
        const response = await fetch(`${API_BASE}/api/v1/user-preferences`, {
          method: "PUT",
          headers: buildHeaders(userId, userEmail, accessToken),
          credentials: "include",
          body: JSON.stringify(newSettings),
        });
        
        if (response.ok) {
          return true;
        }
        
        // Don't retry on client errors (4xx)
        if (response.status >= 400 && response.status < 500) {
          return false;
        }
      } catch {
        // Network error - will retry
      }
    }
    
    return false;
  }, []);

  /**
   * Delete settings from API (reset to defaults)
   */
  const deleteFromAPI = React.useCallback(async (
    userId: string, 
    userEmail?: string,
    accessToken?: string
  ): Promise<boolean> => {
    try {
      const response = await fetch(`${API_BASE}/api/v1/user-preferences`, {
        method: "DELETE",
        headers: buildHeaders(userId, userEmail, accessToken),
        credentials: "include",
      });
      return response.ok;
    } catch {
      return false;
    }
  }, []);

  /**
   * Fetch org-level preferences for tenant auto-inherit
   */
  const fetchOrgPreferences = React.useCallback(async (
    accessToken?: string,
  ): Promise<PersonalizationSettings | null> => {
    for (let attempt = 0; attempt <= API_RETRY_COUNT; attempt++) {
      try {
        if (attempt > 0) {
          await delay(API_RETRY_DELAY_MS * attempt);
        }

        const headers: Record<string, string> = { "Content-Type": "application/json" };
        if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;

        const response = await fetch(`${API_BASE}/api/v1/organizations/my-preferences`, {
          method: "GET",
          headers,
          credentials: "include",
          cache: "no-store",
        });

        if (response.ok) {
          const data = await response.json();
          // settings field contains the PersonalizationSettings structure
          if (data.settings && Object.keys(data.settings).length > 0) {
            return mergeWithDefaults(data.settings);
          }
          return null; // org has no prefs configured yet
        }

        if (response.status >= 400 && response.status < 500) {
          return null;
        }
      } catch {
        // Network error - will retry
      }
    }
    return null;
  }, []);

  // Load settings when session changes
  React.useEffect(() => {
    if (status === "loading") {
      return;
    }

    const userId = session?.user?.id || null;
    const userEmail = session?.user?.email || undefined;
    
    // Detect user change (including logout)
    const isUserChanged = currentUserIdRef.current !== userId;
    if (isUserChanged) {
      // On logout: clear cookie and reset to defaults
      // On first load or login: preserve cookie hint to avoid flash (FOUC)
      // Only reset to defaults if actually logging out (userId is null)
      // or switching to a different user (prev was non-null and different)
      const isPreviouslyLoggedIn = currentUserIdRef.current !== null;
      if (!userId) {
        // Logout: reset everything
        setSettings(DEFAULT_PERSONALIZATION);
        setPersonalizationCookie(DEFAULT_PERSONALIZATION);
      } else if (isPreviouslyLoggedIn) {
        // User switch (was user A, now user B): reset to defaults,
        // the API call below will fetch the new user's settings
        setSettings(DEFAULT_PERSONALIZATION);
      }
      // else: first load with userId - keep cookie-based hint active
      currentUserIdRef.current = userId;
    }

    const loadSettings = async () => {
      setIsLoading(true);
      
      // Unauthenticated: show defaults
      if (!userId) {
        setSettings(DEFAULT_PERSONALIZATION);
        setIsLoading(false);
        return;
      }
      
      const token = (session as any)?.accessToken;

      // Detect role from session groups
      const groups: string[] = (session?.user as any)?.groups ?? [];
      const roles = resolveRoles(groups);
      const isAdminOnly = roles.includes(Role.ADMIN) && !roles.includes(Role.PARTNER);

      if (isAdminOnly) {
        // Admin (system-level): personal user preferences
        const apiSettings = await fetchFromAPI(userId, userEmail, token);
        setSettings(apiSettings ?? DEFAULT_PERSONALIZATION);
      } else {
        // Partner / Tenant: org branding + personal theme
        // Org prefs drive logo, colors, background, branding
        // Personal prefs drive theme only after the user saves one.
        const [orgSettings, personalSettings] = await Promise.all([
          fetchOrgPreferences(token),
          fetchFromAPI(userId, userEmail, token),
        ]);
        const base = orgSettings ?? DEFAULT_PERSONALIZATION;
        const personalTheme = personalSettings?.lastModified ? personalSettings.theme : undefined;
        setSettings({
          ...base,
          theme: personalTheme ?? base.theme,
        });
      }
      
      setIsLoading(false);
    };

    loadSettings();
  }, [status, session?.user?.id, session?.user?.email, fetchFromAPI, fetchOrgPreferences]);

  /**
   * Save settings - debounced to prevent API spam on rapid edits.
   * Pass immediate=true for discrete actions (undo/redo) to skip debounce.
   */
  const saveSettings = React.useCallback(async (
    newSettings: PersonalizationSettings,
    { immediate = false }: { immediate?: boolean } = {},
  ) => {
    const userId = session?.user?.id;
    const userEmail = session?.user?.email ?? undefined;
    
    if (!userId) {
      toast({
        title: "Cannot save settings",
        description: "Please sign in to save your personalization settings.",
        variant: "destructive",
      });
      return;
    }
    
    const updatedSettings: PersonalizationSettings = {
      ...newSettings,
      lastModified: new Date().toISOString(),
    };
    
    // Optimistic update - instant UI feedback
    // Only push to undo stack at the START of a new edit sequence
    // (when no debounce timer is pending). This prevents slider drags
    // from overwriting the original value on every intermediate tick.
    // e.g., dragging 50% → 60% should undo to 50%, not 59%.
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
    setSettings(updatedSettings);

    // Clear any pending save timer
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }

    // flush function that persists to API
    const flush = async (toSave: PersonalizationSettings) => {
      setIsSaving(true);
      const success = await saveToAPI(userId, userEmail, toSave, (session as any)?.accessToken);
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
  }, [session?.user?.id, session?.user?.email, saveToAPI]);

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

  const updateTheme = React.useCallback((theme: ThemePreference) => {
    saveSettings({
      ...settings,
      theme,
    });
  }, [settings, saveSettings]);

  const resetToDefaults = React.useCallback(async () => {
    const userId = session?.user?.id;
    const userEmail = session?.user?.email ?? undefined;
    
    // Push current settings onto undo stack so user can undo the reset
    undoStackRef.current = [...undoStackRef.current, { ...settingsRef.current }];
    if (undoStackRef.current.length > MAX_HISTORY) {
      undoStackRef.current = undoStackRef.current.slice(-MAX_HISTORY);
    }
    redoStackRef.current = [];
    setCanUndo(true);
    setCanRedo(false);

    // Reset UI immediately
    setSettings(DEFAULT_PERSONALIZATION);
    
    if (userId) {
      const success = await deleteFromAPI(userId, userEmail, (session as any)?.accessToken);
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
  }, [session?.user?.id, session?.user?.email, deleteFromAPI]);

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
    setSettings(newSettings);
  }, []);

  const refreshSettings = React.useCallback(async () => {
    const userId = session?.user?.id;
    const userEmail = session?.user?.email ?? undefined;
    
    if (!userId) {
      setSettings(DEFAULT_PERSONALIZATION);
      return;
    }
    
    // Silent refresh - no isLoading flash to prevent visible re-render
    const token = (session as any)?.accessToken;

    // detect role to decide which source to load
    const groups: string[] = (session?.user as any)?.groups ?? [];
    const roles = resolveRoles(groups);
    const isAdminOnly = roles.includes(Role.ADMIN) && !roles.includes(Role.PARTNER);

    if (isAdminOnly) {
      const apiSettings = await fetchFromAPI(userId, userEmail, token);
      setSettings(apiSettings ?? DEFAULT_PERSONALIZATION);
    } else {
      // Partner / Tenant: org branding + saved personal theme
      const [orgSettings, personalSettings] = await Promise.all([
        fetchOrgPreferences(token),
        fetchFromAPI(userId, userEmail, token),
      ]);
      const base = orgSettings ?? DEFAULT_PERSONALIZATION;
      const personalTheme = personalSettings?.lastModified ? personalSettings.theme : undefined;
      setSettings({
        ...base,
        theme: personalTheme ?? base.theme,
      });
    }
    
    // No isLoading = false needed (silent refresh has no loading state)
  }, [session, fetchFromAPI, fetchOrgPreferences]);

  // ────────────────────────────────────────────────────────────────────────
  // Cross-session org-preference sync (polling + visibility)
  //
  // When a Partner saves org branding, other sessions (Tenant / Partner in
  // another browser) must pick up the change without a hard refresh.
  // Strategy:
  //   1. Poll a lightweight /version endpoint every ORG_PREF_POLL_INTERVAL_MS
  //      (returns only updated_at - avoids transferring base64 images).
  //   2. On document visibilitychange (tab becomes visible), do a single
  //      version check and full refresh if changed.
  //   3. Skip entirely for Admin (personal prefs only) and unauthenticated.
  // ────────────────────────────────────────────────────────────────────────

  // Track the last-known org preferences version to avoid unnecessary full fetches
  const orgPrefsVersionRef = React.useRef<string | null>(null);

  /**
   * Lightweight version check: fetch only updated_at from backend.
   * Returns true if org preferences have changed since last check.
   */
  const checkOrgPrefsVersion = React.useCallback(async (
    accessToken?: string,
  ): Promise<boolean> => {
    try {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;

      const response = await fetch(`${API_BASE}/api/v1/organizations/my-preferences/version`, {
        method: "GET",
        headers,
        credentials: "include",
        cache: "no-store",
      });

      if (!response.ok) return false;

      const data = await response.json();
      const serverVersion = data.updated_at ?? null;
      const localVersion = orgPrefsVersionRef.current;

      if (serverVersion !== localVersion) {
        orgPrefsVersionRef.current = serverVersion;
        // On first fetch (localVersion === null), record version but don't flag as changed
        // because the initial load already fetched settings.
        return localVersion !== null;
      }

      return false; // no change
    } catch {
      return false; // network error - skip this cycle
    }
  }, []);

  // Determine if current user should use org-pref sync
  const shouldPollOrgPrefs = React.useMemo(() => {
    if (!session?.user?.id) return false;
    const groups: string[] = (session?.user as any)?.groups ?? [];
    const roles = resolveRoles(groups);
    return !(roles.includes(Role.ADMIN) && !roles.includes(Role.PARTNER)); // Partner + Tenant inherit org prefs
  }, [session?.user?.id, (session?.user as any)?.groups]);

  // Seed the version ref on initial load so subsequent polls have a baseline
  React.useEffect(() => {
    if (!shouldPollOrgPrefs) return;
    const token = (session as any)?.accessToken;
    // Fire-and-forget: just records the current version
    checkOrgPrefsVersion(token);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shouldPollOrgPrefs]);

  // Periodic polling: check version every N seconds, full refresh only on change
  React.useEffect(() => {
    if (!shouldPollOrgPrefs) return;

    const intervalId = setInterval(async () => {
      const token = (session as any)?.accessToken;
      const changed = await checkOrgPrefsVersion(token);
      if (changed) {
        await refreshSettings();
      }
    }, ORG_PREF_POLL_INTERVAL_MS);

    return () => clearInterval(intervalId);
  }, [shouldPollOrgPrefs, checkOrgPrefsVersion, refreshSettings, session]);

  // Visibility-based refresh: when user switches back to the tab, check immediately
  React.useEffect(() => {
    if (!shouldPollOrgPrefs) return;

    const handleVisibilityChange = async () => {
      if (document.visibilityState !== "visible") return;
      const token = (session as any)?.accessToken;
      const changed = await checkOrgPrefsVersion(token);
      if (changed) {
        await refreshSettings();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, [shouldPollOrgPrefs, checkOrgPrefsVersion, refreshSettings, session]);

  const value: PersonalizationContextType = {
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
  };

  // Apply CSS custom properties for colors
  React.useEffect(() => {
    if (typeof document === "undefined") return;
    
    const root = document.documentElement;
    const accentColor = settings.colors.accentColor || "#8b5cf6";
    const sidebarBgColor = settings.colors.sidebarBgColor || "#ffffff";
    
    // Set accent color (used for buttons and sidebar active state)
    root.style.setProperty("--personalization-accent", accentColor);
    root.style.setProperty("--personalization-sidebar-bg", sidebarBgColor);
    
    // Convert hex to HSL for better integration with shadcn/ui
    const accentHSL = hexToHSL(accentColor);
    const sidebarBgHSL = hexToHSL(sidebarBgColor);
    
    if (accentHSL) {
      root.style.setProperty("--personalization-accent-h", String(accentHSL.h));
      root.style.setProperty("--personalization-accent-s", `${accentHSL.s}%`);
      root.style.setProperty("--personalization-accent-l", `${accentHSL.l}%`);
    }
    
    // Determine text color based on background brightness
    let textColor = settings.colors.sidebarTextColor || "#1e293b";
    let mutedTextColor = "rgba(15,23,42,0.5)";
    
    if (settings.colors.autoTextColor !== false && !settings.colors.useCustomSidebarText) {
      if (sidebarBgHSL) {
        const bgLuminance = calculateRelativeLuminance(sidebarBgColor);
        const isDarkBg = bgLuminance < 0.5;
        textColor = isDarkBg ? "#f8fafc" : "#1e293b";
        mutedTextColor = isDarkBg ? "rgba(248,250,252,0.6)" : "rgba(30,41,59,0.5)";
      }
    } else {
      const textHSL = hexToHSL(textColor);
      if (textHSL) {
        const isDarkText = textHSL.l < 50;
        mutedTextColor = isDarkText ? "rgba(30,41,59,0.5)" : "rgba(248,250,252,0.6)";
      }
    }
    
    root.style.setProperty("--personalization-sidebar-text", textColor);
    root.style.setProperty("--personalization-sidebar-muted", mutedTextColor);
    
    if (sidebarBgHSL) {
      root.style.setProperty("--personalization-sidebar-bg-h", String(sidebarBgHSL.h));
      root.style.setProperty("--personalization-sidebar-bg-s", `${sidebarBgHSL.s}%`);
      root.style.setProperty("--personalization-sidebar-bg-l", `${sidebarBgHSL.l}%`);
    }
  }, [settings.colors]);

  // Persist visual hint cookie whenever visual settings change (for FOUC prevention on next load)
  React.useEffect(() => {
    setPersonalizationCookie(settings);
  }, [settings.colors, settings.branding.appName, settings.theme]);

  return (
    <PersonalizationContext.Provider value={value}>
      {children}
    </PersonalizationContext.Provider>
  );
}

// ============================================
// Helper Functions
// ============================================

/**
 * Convert hex color to HSL
 */
function hexToHSL(hex: string): { h: number; s: number; l: number } | null {
  const result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if (!result) return null;
  
  const r = parseInt(result[1], 16) / 255;
  const g = parseInt(result[2], 16) / 255;
  const b = parseInt(result[3], 16) / 255;
  
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  let h = 0;
  let s = 0;
  const l = (max + min) / 2;
  
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    switch (max) {
      case r: h = ((g - b) / d + (g < b ? 6 : 0)) / 6; break;
      case g: h = ((b - r) / d + 2) / 6; break;
      case b: h = ((r - g) / d + 4) / 6; break;
    }
  }
  
  return { h: Math.round(h * 360), s: Math.round(s * 100), l: Math.round(l * 100) };
}

/**
 * Calculate relative luminance for WCAG contrast
 */
function calculateRelativeLuminance(hex: string): number {
  const result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if (!result) return 0.5;
  
  const sRGB = [
    parseInt(result[1], 16) / 255,
    parseInt(result[2], 16) / 255,
    parseInt(result[3], 16) / 255,
  ].map((c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)));
  
  return 0.2126 * sRGB[0] + 0.7152 * sRGB[1] + 0.0722 * sRGB[2];
}

// ============================================
// Hooks
// ============================================

/**
 * Hook to access personalization context
 */
export function usePersonalization() {
  const context = React.useContext(PersonalizationContext);
  if (context === undefined) {
    throw new Error("usePersonalization must be used within a PersonalizationProvider");
  }
  return context;
}

/**
 * Hook to get the appropriate logo based on theme and sidebar settings
 */
export function useCurrentLogo(theme: "light" | "dark" | "system") {
  const { settings } = usePersonalization();
  
  const effectiveTheme = React.useMemo(() => {
    if (theme === "system") {
      return typeof window !== "undefined" &&
        window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
    }
    return theme;
  }, [theme]);

  return React.useMemo(() => {
    const logoColorPref = settings.colors.sidebarLogoColor || "auto";
    
    if (logoColorPref === "black") {
      return settings.logo.lightModeLogo || "/orca-logo-black.png";
    }
    if (logoColorPref === "white") {
      return settings.logo.darkModeLogo || "/orca-logo-white.png";
    }
    
    // Auto mode
    if (settings.colors.useCustomSidebarBg) {
      const bgLuminance = calculateRelativeLuminance(settings.colors.sidebarBgColor || "#ffffff");
      const isDarkBg = bgLuminance < 0.5;
      if (isDarkBg) {
        return settings.logo.darkModeLogo || "/orca-logo-white.png";
      }
      return settings.logo.lightModeLogo || "/orca-logo-black.png";
    }
    
    // Follow system theme
    if (effectiveTheme === "light") {
      return settings.logo.lightModeLogo || "/orca-logo-black.png";
    }
    return settings.logo.darkModeLogo || "/orca-logo-white.png";
  }, [effectiveTheme, settings.logo, settings.colors.sidebarLogoColor, settings.colors.useCustomSidebarBg, settings.colors.sidebarBgColor]);
}

