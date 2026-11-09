"use client";

import * as React from "react";
import { useSession } from "next-auth/react";
import { DEFAULT_PERSONALIZATION, type PersonalizationSettings, type PersonalizationContextType } from "@/types/personalization";
import { hasMappedRole } from "@/types/rbac";
import { hexToHSL } from "@/lib/color";
import { PERSONALIZATION_COOKIE, parsePersonalizationHint, toPersonalizationHint } from "@/lib/personalization-cookie";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { usePersonalizationSync, type PreferenceOrigin } from "./personalization/use-personalization-sync";
import { usePersonalizationMutations } from "./personalization/use-personalization-mutations";

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

export const PersonalizationContext = React.createContext<PersonalizationContextType | undefined>(undefined);

/**
 * Persist visual personalization hint in cookie for instant load (365 days).
 * Only stores CSS-relevant values - NOT images/logos (too large for cookies).
 */
function setPersonalizationCookie(settings: PersonalizationSettings, withIllustrations: boolean) {
  if (typeof document === "undefined") return;
  try {
    const hint = toPersonalizationHint(settings);
    // Signed out, the choice is the deployment's, which the root layout reads on every request;
    // writing the built-in default here would mask it on the next one.
    if (!withIllustrations) delete hint.il;
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
  const match = document.cookie.match(new RegExp(`${PERSONALIZATION_COOKIE}=([^;]+)`));
  return parsePersonalizationHint(match?.[1]);
}

export function PersonalizationProvider({ children, initialSettings }: {
  children: React.ReactNode;
  initialSettings?: PersonalizationSettings | null;
}) {
  const { data: session, status } = useSession();
  const { scopeKey, accessToken, isAuthenticated } = useActorQueryScope();
  const seededRef = React.useRef<PersonalizationSettings | null>(null);
  if (seededRef.current === null) {
    // The server prop precedes the cookie fallback so SSR and hydration agree on first paint.
    seededRef.current = initialSettings ?? readPersonalizationCookie() ?? DEFAULT_PERSONALIZATION;
  }
  const [settings, setSettings] = React.useState<PersonalizationSettings>(seededRef.current);
  const [isLoading, setIsLoading] = React.useState(true);
  const personalRowRef = React.useRef<PersonalizationSettings | null>(null);
  const lastLocalEditRef = React.useRef(0);
  const [origin, setOrigin] = React.useState<PreferenceOrigin>({
    personal: false,
    organizationTheme: DEFAULT_PERSONALIZATION.theme,
    illustrationsPersonal: false,
    organizationIllustrations: DEFAULT_PERSONALIZATION.illustrations,
  });
  const actor = {
    userId: session?.user?.id ?? null,
    accessToken: accessToken ?? undefined,
    scopeKey,
  };
  // A caller with no role sees only the access notice and the backend refuses its reads, so
  // there is nothing to fetch and every attempt would be a 403 (issue #17).
  const mayRead = !Array.isArray(session?.user?.groups) || hasMappedRole(session.user.groups);
  // Only an account whose preferences are read owns the illustrations choice on screen. Signed
  // out, or without a role, the server already rendered the deployment's choice (app/layout.tsx)
  // and the defaults held here would overwrite it.
  const ownsIllustrations = isAuthenticated && mayRead;
  const { refreshSettings } = usePersonalizationSync({
    ...actor,
    authenticated: isAuthenticated && mayRead,
    sessionLoading: status === "loading" && !session,
    personalRowRef, lastLocalEditRef, setSettings, setIsLoading, setOrigin,
  });
  const mutations = usePersonalizationMutations({
    ...actor, setSettings, origin, setOrigin, lastLocalEditRef,
  });
  const value: PersonalizationContextType = {
    settings,
    ...mutations,
    themeIsPersonal: origin.personal,
    organizationTheme: origin.organizationTheme,
    illustrationsArePersonal: origin.illustrationsPersonal,
    organizationIllustrations: origin.organizationIllustrations,
    refreshSettings,
    isLoading,
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

  // The root layout renders data-illustrations from the hint cookie, so the first byte of HTML is
  // already right; this keeps the attribute in step with a choice made or loaded since.
  React.useEffect(() => {
    if (typeof document === "undefined" || !ownsIllustrations) return;
    document.documentElement.dataset.illustrations = settings.illustrations;
  }, [settings.illustrations, ownsIllustrations]);

  // Persist visual hint cookie whenever visual settings change (for FOUC prevention on next load)
  React.useEffect(() => {
    setPersonalizationCookie(settings, ownsIllustrations);
  }, [settings.colors, settings.branding.appName, settings.theme, settings.illustrations, ownsIllustrations]);

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
