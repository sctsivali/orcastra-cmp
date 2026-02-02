"use client";

/**
 * Personalization Provider - Enterprise Grade
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
  DEFAULT_PERSONALIZATION,
} from "@/types/personalization";
import { toast } from "@/hooks/use-toast";

// API configuration
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "";
const API_RETRY_COUNT = 2;
const API_RETRY_DELAY_MS = 500;

// Create context
const PersonalizationContext = React.createContext<PersonalizationContextType | undefined>(
  undefined
);

// Helper to delay execution for retries
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

/**
 * Build headers for API requests
 */
function buildHeaders(userId?: string, userEmail?: string): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
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
  };
}

export function PersonalizationProvider({ children }: { children: React.ReactNode }) {
  const { data: session, status } = useSession();
  const [settings, setSettings] = React.useState<PersonalizationSettings>(DEFAULT_PERSONALIZATION);
  const [isLoading, setIsLoading] = React.useState(true);
  const [isSaving, setIsSaving] = React.useState(false);
  
  // Track current user ID to detect user changes
  const currentUserIdRef = React.useRef<string | null>(null);

  /**
   * Fetch settings from API with retry mechanism
   */
  const fetchFromAPI = React.useCallback(async (
    userId: string, 
    userEmail?: string
  ): Promise<PersonalizationSettings | null> => {
    for (let attempt = 0; attempt <= API_RETRY_COUNT; attempt++) {
      try {
        if (attempt > 0) {
          await delay(API_RETRY_DELAY_MS * attempt);
        }
        
        const response = await fetch(`${API_BASE}/api/v1/user-preferences`, {
          method: "GET",
          headers: buildHeaders(userId, userEmail),
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
    newSettings: PersonalizationSettings
  ): Promise<boolean> => {
    for (let attempt = 0; attempt <= API_RETRY_COUNT; attempt++) {
      try {
        if (attempt > 0) {
          await delay(API_RETRY_DELAY_MS * attempt);
        }
        
        const response = await fetch(`${API_BASE}/api/v1/user-preferences`, {
          method: "PUT",
          headers: buildHeaders(userId, userEmail),
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
    userEmail?: string
  ): Promise<boolean> => {
    try {
      const response = await fetch(`${API_BASE}/api/v1/user-preferences`, {
        method: "DELETE",
        headers: buildHeaders(userId, userEmail),
        credentials: "include",
      });
      return response.ok;
    } catch {
      return false;
    }
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
      setSettings(DEFAULT_PERSONALIZATION);
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
      
      // Authenticated: fetch from API
      const apiSettings = await fetchFromAPI(userId, userEmail);
      
      if (apiSettings) {
        setSettings(apiSettings);
      } else {
        // API failed - use defaults and notify user
        setSettings(DEFAULT_PERSONALIZATION);
        toast({
          title: "Could not load personalization",
          description: "Using default settings. Your custom settings will load when the connection is restored.",
          variant: "destructive",
        });
      }
      
      setIsLoading(false);
    };

    loadSettings();
  }, [status, session?.user?.id, session?.user?.email, fetchFromAPI]);

  /**
   * Save settings - requires authentication
   */
  const saveSettings = React.useCallback(async (newSettings: PersonalizationSettings) => {
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
    
    // Optimistic update for responsive UI
    setSettings(updatedSettings);
    setIsSaving(true);
    
    const success = await saveToAPI(userId, userEmail, updatedSettings);
    setIsSaving(false);
    
    if (!success) {
      // Revert on failure
      toast({
        title: "Failed to save settings",
        description: "Your changes could not be saved. Please try again.",
        variant: "destructive",
      });
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

  const resetToDefaults = React.useCallback(async () => {
    const userId = session?.user?.id;
    const userEmail = session?.user?.email ?? undefined;
    
    // Reset UI immediately
    setSettings(DEFAULT_PERSONALIZATION);
    
    if (userId) {
      const success = await deleteFromAPI(userId, userEmail);
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

  const refreshSettings = React.useCallback(async () => {
    const userId = session?.user?.id;
    const userEmail = session?.user?.email ?? undefined;
    
    if (!userId) {
      setSettings(DEFAULT_PERSONALIZATION);
      return;
    }
    
    setIsLoading(true);
    const apiSettings = await fetchFromAPI(userId, userEmail);
    
    if (apiSettings) {
      setSettings(apiSettings);
      toast({
        title: "Settings refreshed",
        description: "Your personalization settings have been reloaded.",
      });
    } else {
      toast({
        title: "Could not refresh settings",
        description: "Failed to load settings from server.",
        variant: "destructive",
      });
    }
    
    setIsLoading(false);
  }, [session?.user?.id, session?.user?.email, fetchFromAPI]);

  const value: PersonalizationContextType = {
    settings,
    updateLogo,
    updateBackground,
    updateBranding,
    updateColors,
    resetToDefaults,
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

