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
import { useQueryClient } from "@tanstack/react-query";
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
import {
  PERSONALIZATION_COOKIE,
  parsePersonalizationHint,
  toPersonalizationHint,
} from "@/lib/personalization-cookie";
import { toast } from "@/hooks/use-toast";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { onAuthActorChanged } from "@/lib/api";
import {
  ActorMutationFence,
  captureActorMutationFence,
  dispatchActorMutation,
  isActorMutationFenceCurrent,
} from "@/lib/actor-mutation-fence";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { personalizationAPI } from "@/lib/personalization-api";

// API configuration - use relative URLs (Next.js rewrites proxy to backend)
const API_BASE = "";
const API_RETRY_COUNT = 2;
const API_RETRY_DELAY_MS = 500;

// Org preference polling interval (ms) for cross-session sync
const ORG_PREF_POLL_INTERVAL_MS = 45_000;

// The two sidebar text pairs autoTextColor picks between, and the alpha tiers that go with
// them. Named because app/globals.css seeds the same values for the pre-paint path and
// scripts/checks/check-personalization-defaults.mjs holds the two copies equal; a literal
// buried in a branch is how they drifted apart before.
const SIDEBAR_TEXT_ON_DARK = "#f8fafc";
const SIDEBAR_TEXT_ON_LIGHT = "#1e293b";
const SIDEBAR_MUTED_ON_DARK = "rgba(248,250,252,0.6)";
const SIDEBAR_MUTED_ON_LIGHT = "rgba(30,41,59,0.5)";

/** The muted tier that belongs with a given sidebar text colour. */
function mutedForText(textColor: string): string {
  const hsl = hexToHSL(textColor);
  if (!hsl) return SIDEBAR_MUTED_ON_DARK;
  return hsl.l < 50 ? SIDEBAR_MUTED_ON_LIGHT : SIDEBAR_MUTED_ON_DARK;
}

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
    const encoded = encodeURIComponent(JSON.stringify(toPersonalizationHint(settings)));
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
  const match = document.cookie.match(new RegExp(`${PERSONALIZATION_COOKIE}=([^;]+)`));
  return parsePersonalizationHint(match?.[1]);
}

/**
 * Build headers for API requests
 *
 * important: the user identity is NOT sent here. The backend derives it from the
 * validated bearer token. It used to trust X-User-ID, which let any authenticated
 * caller read or overwrite another user's preferences by changing one header.
 * The parameters are kept so call sites stay unchanged and are deliberately unused.
 */
function buildHeaders(_userId?: string, _userEmail?: string, accessToken?: string): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (accessToken) {
    headers["Authorization"] = `Bearer ${accessToken}`;
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

export function PersonalizationProvider({
  children,
  initialSettings,
}: {
  children: React.ReactNode;
  // Parsed from the hint cookie by the root layout, which is a server component and can read
  // it. Without this the server always renders DEFAULT_PERSONALIZATION while the browser's
  // first pass renders the operator's stored colours, so the aside's className and inline
  // style disagree between the two and React reconciles them one tick after paint. That is
  // visible as the sidebar changing colour on every navigation for anyone whose settings are
  // not the shipped default. suppressHydrationWarning on <html> does not reach it.
  initialSettings?: PersonalizationSettings | null;
}) {
  const { data: session, status } = useSession();
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  // note: next-auth builds a fresh session object on every refresh, so depending on
  // `session` itself tears down and rebuilds the org-pref timer and listener below
  // on each one. The token is the only part those effects actually read.
  const sessionAccessToken = (session as any)?.accessToken as string | undefined;
  // note: the server-provided value is read FIRST and the document.cookie read is the
  // fallback, not the other way round. Both resolve to the same thing on the client, but only
  // the prop exists during the server render, and the two initialisers below must agree.
  // Lazily, because a useRef argument is evaluated on every render and this provider wraps the
  // whole app: the eager form ran a cookie regex and a JSON.parse on each one.
  const seededRef = React.useRef<PersonalizationSettings | null>(null);
  if (seededRef.current === null) {
    seededRef.current = initialSettings ?? readPersonalizationCookie() ?? DEFAULT_PERSONALIZATION;
  }
  // The value, not the ref: useRef<T>(T | null) resolves to React's readonly RefObject overload,
  // and settingsRef below has to stay writable.
  const seeded: PersonalizationSettings = seededRef.current;
  // Initialize from cookie hint for instant render (prevents FOUC for colors/branding)
  const [settings, setSettings] = React.useState<PersonalizationSettings>(() => seeded);
  const [isLoading, setIsLoading] = React.useState(true);
  const [isSaving, setIsSaving] = React.useState(false);
  
  // Track current user ID to detect user changes
  const currentUserIdRef = React.useRef<string | null>(null);
  // Debounce ref for save operations
  const saveTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSettingsRef = React.useRef<PersonalizationSettings | null>(null);
  // What the personal user_preferences row actually holds, and whether this session's colours come
  // from an organization. updateTheme needs both: for a Partner or Tenant the personal row is a
  // theme carrier, not a copy of what is on screen.
  const personalRowRef = React.useRef<PersonalizationSettings | null>(null);
  const orgBackedRef = React.useRef(false);
  // When the operator last changed something locally.
  //
  // important: loadSettings below is async, and it used to setSettings unconditionally when its
  // response arrived. A click landing between the fetch starting and finishing was therefore
  // overwritten by the value the server still held: the change painted, stuck for a moment, and
  // reverted. That is the same symptom as the organization-targeting defect and a completely
  // different cause, which is why it survived that fix and showed up as an intermittent theme
  // failure. Compared against the load's own start time rather than a boolean, so a slow response
  // from an EARLIER load cannot suppress a later one.
  const lastLocalEditRef = React.useRef(0);
  // Always-current settings ref (avoids stale closure in saveSettings)
  const settingsRef = React.useRef<PersonalizationSettings>(seeded);
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

  const clearPendingSave = React.useCallback(() => {
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    pendingSettingsRef.current = null;
  }, []);

  // Retire debounced work synchronously at the auth boundary. The fence checked immediately
  // before dispatch is still required: a timer can already be runnable, and A -> B -> A must
  // not make an old A callback current again merely because its actor label matches.
  React.useEffect(() => {
    const unsubscribe = onAuthActorChanged(() => {
      clearPendingSave();
      setIsSaving(false);
    });
    return () => {
      unsubscribe();
      clearPendingSave();
    };
  }, [clearPendingSave]);

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

      // Everything this load writes goes through here. A response is only authoritative if the
      // operator has not changed anything since the request went out; otherwise it is a stale
      // answer to a question that has moved on, and applying it reads as the edit reverting.
      const startedAt = Date.now();
      const applyLoaded = (next: PersonalizationSettings) => {
        if (lastLocalEditRef.current > startedAt) return;
        setSettings(next);
      };
      
      const token = (session as any)?.accessToken;

      // Detect role from session groups
      const groups: string[] = (session?.user as any)?.groups ?? [];
      const roles = resolveRoles(groups);
      const isAdminOnly = roles.includes(Role.ADMIN) && !roles.includes(Role.PARTNER);

      orgBackedRef.current = !isAdminOnly;

      if (isAdminOnly) {
        // Admin (system-level): personal user preferences
        const apiSettings = await fetchFromAPI(userId, userEmail, token);
        applyLoaded(apiSettings ?? DEFAULT_PERSONALIZATION);
        personalRowRef.current = apiSettings ?? null;
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
        applyLoaded({
          ...base,
          theme: personalTheme ?? base.theme,
        });
        // important: kept separately from `settings`. For these roles `settings` is the
        // ORGANIZATION's branding with a personal theme laid over it, and it is the right thing
        // to render. It is the wrong thing to save: updateTheme used to write all of it to the
        // personal row, so changing the theme stamped the organization's colours into a row that
        // is only ever read for `theme`. Harmless on screen, and it made the table lie about what
        // an operator had chosen, which is the evidence anybody debugging this reaches for first.
        personalRowRef.current = personalSettings;
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
    const actorFence = captureActorMutationFence(scopeKey);
    
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
    lastLocalEditRef.current = Date.now();
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
        userId,
        userEmail,
        toSave,
        sessionAccessToken,
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
  }, [session?.user?.id, session?.user?.email, sessionAccessToken, scopeKey, saveToAPI]);

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
    const userId = session?.user?.id;
    const actorFence = captureActorMutationFence(scopeKey);
    if (!userId) {
      toast({
        title: "Cannot save settings",
        description: "Please sign in to save your personalization settings.",
        variant: "destructive",
      });
      return;
    }
    const personal: PersonalizationSettings = {
      ...(personalRowRef.current ?? DEFAULT_PERSONALIZATION),
      theme,
      // important: the loader honours a personal theme only when the row carries a lastModified,
      // which is how "never chose a theme" is told apart from "chose System". Dropping it here
      // would make the choice survive the session and vanish on the next load.
      lastModified: new Date().toISOString(),
    };
    personalRowRef.current = personal;
    lastLocalEditRef.current = Date.now();
    if (!isActorMutationFenceCurrent(actorFence)) return;
    setIsSaving(true);
    const ok = await saveToAPI(
      userId,
      session?.user?.email ?? undefined,
      personal,
      sessionAccessToken,
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
  }, [session?.user?.id, session?.user?.email, sessionAccessToken, scopeKey, saveToAPI]);

  const updateTheme = React.useCallback((theme: ThemePreference) => {
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
  }, [settings, saveSettings, persistPersonalTheme]);

  const resetToDefaults = React.useCallback(async () => {
    const userId = session?.user?.id;
    const userEmail = session?.user?.email ?? undefined;
    const actorFence = captureActorMutationFence(scopeKey);
    
    // Push current settings onto undo stack so user can undo the reset
    undoStackRef.current = [...undoStackRef.current, { ...settingsRef.current }];
    if (undoStackRef.current.length > MAX_HISTORY) {
      undoStackRef.current = undoStackRef.current.slice(-MAX_HISTORY);
    }
    redoStackRef.current = [];
    setCanUndo(true);
    setCanRedo(false);

    // Reset UI immediately
    lastLocalEditRef.current = Date.now();
    setSettings(DEFAULT_PERSONALIZATION);
    // The row is about to be deleted, so the next theme save must not rebuild it from a stale
    // copy of what it used to hold.
    personalRowRef.current = null;
    
    if (userId) {
      const success = await deleteFromAPI(
        userId,
        userEmail,
        sessionAccessToken,
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
  }, [session?.user?.id, session?.user?.email, sessionAccessToken, scopeKey, deleteFromAPI]);

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
    // Fire-and-forget: just records the current version
    checkOrgPrefsVersion(sessionAccessToken);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shouldPollOrgPrefs]);

  // Periodic polling: check version every N seconds, full refresh only on change
  React.useEffect(() => {
    if (!shouldPollOrgPrefs) return;

    const intervalId = setInterval(async () => {
      const changed = await checkOrgPrefsVersion(sessionAccessToken);
      if (changed) {
        await refreshSettings();
      }
    }, ORG_PREF_POLL_INTERVAL_MS);

    return () => clearInterval(intervalId);
  }, [shouldPollOrgPrefs, checkOrgPrefsVersion, refreshSettings, sessionAccessToken]);

  // Visibility-based refresh: when user switches back to the tab, check immediately
  React.useEffect(() => {
    if (!shouldPollOrgPrefs) return;

    const handleVisibilityChange = async () => {
      if (document.visibilityState !== "visible") return;
      const changed = await checkOrgPrefsVersion(sessionAccessToken);
      if (changed) {
        await refreshSettings();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, [shouldPollOrgPrefs, checkOrgPrefsVersion, refreshSettings, sessionAccessToken]);

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
    const accentColor = settings.colors.accentColor || DEFAULT_PERSONALIZATION.colors.accentColor;
    const sidebarBgColor =
      settings.colors.sidebarBgColor || DEFAULT_PERSONALIZATION.colors.sidebarBgColor;
    
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
    let textColor =
      settings.colors.sidebarTextColor || DEFAULT_PERSONALIZATION.colors.sidebarTextColor;

    if (settings.colors.autoTextColor !== false && !settings.colors.useCustomSidebarText) {
      if (sidebarBgHSL) {
        const isDarkBg = calculateRelativeLuminance(sidebarBgColor) < 0.5;
        textColor = isDarkBg ? SIDEBAR_TEXT_ON_DARK : SIDEBAR_TEXT_ON_LIGHT;
      }
    }
    // note: derived from whichever text colour won above, in every branch. It used to be
    // initialised to the light-ground alpha and only corrected inside the two branches, so an
    // unparseable sidebar colour left near-white text sitting under a near-black muted tier.
    const mutedTextColor = mutedForText(textColor);
    
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
      const bgLuminance = calculateRelativeLuminance(
        settings.colors.sidebarBgColor || DEFAULT_PERSONALIZATION.colors.sidebarBgColor,
      );
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
