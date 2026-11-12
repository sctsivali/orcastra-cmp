"use client";

import * as React from "react";
import { useSession } from "next-auth/react";
import { DEFAULT_PERSONALIZATION, type PersonalizationSettings, type PersonalizationContextType } from "@/types/personalization";
import { hasMappedRole } from "@/types/rbac";
import { hexToHSL } from "@/lib/color";
import { PERSONALIZATION_COOKIE, parseByteUnitsHint, parsePersonalizationHint, toPersonalizationHint } from "@/lib/personalization-cookie";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { ByteUnitsContext } from "@/hooks/use-byte-format";
import type { ByteUnitSystem } from "@/lib/byte-units";
import { setRequestByteUnits } from "@/lib/api";
import { usePersonalizationSync, type ThemeOrigin } from "./personalization/use-personalization-sync";
import { usePersonalizationMutations } from "./personalization/use-personalization-mutations";

// The two sidebar text pairs autoTextColor picks between, and the alpha tiers that go with
// them. Named because app/globals.css seeds the same values for the pre-paint path and
// scripts/checks/check-personalization-defaults.mjs holds the two copies equal.
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
 * Writes the visual hint cookie (365 days) so the next load paints at once. CSS values only:
 * images and logos are too large for a cookie.
 */
function setPersonalizationCookie(settings: PersonalizationSettings, withIllustrations: boolean, byteUnits: ByteUnitSystem) {
  if (typeof document === "undefined") return;
  try {
    const hint = toPersonalizationHint(settings, byteUnits);
    // Signed out, the choice is the deployment's, which the root layout reads on every request;
    // writing the built-in default here would mask it on the next one.
    if (!withIllustrations) delete hint.il;
    const encoded = encodeURIComponent(JSON.stringify(hint));
    document.cookie = `${PERSONALIZATION_COOKIE}=${encoded};path=/;max-age=${365 * 24 * 60 * 60};SameSite=Lax`;
  } catch {
    // A failed write only costs the instant first paint.
  }
}

/** The hint cookie as settings merged with defaults, for the first render. Client side only. */
function readPersonalizationCookie(): PersonalizationSettings | null {
  return parsePersonalizationHint(readHintCookie());
}

function readHintCookie(): string | undefined {
  if (typeof document === "undefined") return undefined;
  return document.cookie.match(new RegExp(`${PERSONALIZATION_COOKIE}=([^;]+)`))?.[1];
}

export function PersonalizationProvider({ children, initialSettings, initialByteUnits }: {
  children: React.ReactNode;
  initialSettings?: PersonalizationSettings | null;
  initialByteUnits?: ByteUnitSystem;
}) {
  const { data: session, status } = useSession();
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const seededRef = React.useRef<PersonalizationSettings | null>(null);
  if (seededRef.current === null) {
    // The server prop precedes the cookie fallback so SSR and hydration agree on first paint.
    seededRef.current = initialSettings ?? readPersonalizationCookie() ?? DEFAULT_PERSONALIZATION;
  }
  const [settings, setSettings] = React.useState<PersonalizationSettings>(seededRef.current);
  // Same order as the settings: the server's reading of the hint first, so both renders agree.
  const [byteUnits, setByteUnits] = React.useState<ByteUnitSystem>(
    () => initialByteUnits ?? parseByteUnitsHint(readHintCookie()),
  );
  // Before children render, so the first request any of them sends already carries the units.
  setRequestByteUnits(byteUnits);
  const [isLoading, setIsLoading] = React.useState(true);
  const personalRowRef = React.useRef<PersonalizationSettings | null>(null);
  const lastLocalEditRef = React.useRef(0);
  const [themeOrigin, setThemeOrigin] = React.useState<ThemeOrigin>({
    personal: false,
    organizationTheme: DEFAULT_PERSONALIZATION.theme,
  });
  const actor = {
    userId: session?.user?.id ?? null,
    scopeKey,
  };
  // A caller with no role sees only the access notice and the backend refuses its reads, so
  // there is nothing to fetch and every attempt would be a 403.
  const mayRead = !Array.isArray(session?.user?.groups) || hasMappedRole(session.user.groups);
  // The illustrations choice on screen is the organization's, and only an account whose branding is
  // read carries it. Signed out, or without a role, the server already rendered the deployment's
  // choice (app/layout.tsx) and the defaults held here would overwrite it.
  const ownsIllustrations = isAuthenticated && mayRead;
  const { refreshSettings } = usePersonalizationSync({
    ...actor,
    authenticated: isAuthenticated && mayRead,
    sessionLoading: status === "loading" && !session,
    personalRowRef, lastLocalEditRef, setSettings, setIsLoading, setThemeOrigin, setByteUnits,
  });
  const mutations = usePersonalizationMutations({
    ...actor, setSettings, themeOrigin, setThemeOrigin, byteUnits, setByteUnits, lastLocalEditRef,
  });
  const value: PersonalizationContextType = {
    settings,
    ...mutations,
    themeIsPersonal: themeOrigin.personal,
    organizationTheme: themeOrigin.organizationTheme,
    byteUnits,
    refreshSettings,
    isLoading,
  };

  React.useEffect(() => {
    if (typeof document === "undefined") return;
    
    const root = document.documentElement;
    const accentColor = settings.colors.accentColor || DEFAULT_PERSONALIZATION.colors.accentColor;
    const sidebarBgColor =
      settings.colors.sidebarBgColor || DEFAULT_PERSONALIZATION.colors.sidebarBgColor;
    
    // The accent drives buttons and the sidebar active state.
    root.style.setProperty("--personalization-accent", accentColor);
    root.style.setProperty("--personalization-sidebar-bg", sidebarBgColor);
    
    // shadcn/ui tokens take HSL components.
    const accentHSL = hexToHSL(accentColor);
    const sidebarBgHSL = hexToHSL(sidebarBgColor);
    
    if (accentHSL) {
      root.style.setProperty("--personalization-accent-h", String(accentHSL.h));
      root.style.setProperty("--personalization-accent-s", `${accentHSL.s}%`);
      root.style.setProperty("--personalization-accent-l", `${accentHSL.l}%`);
    }
    
    // Pick the text colour from the background brightness.
    let textColor =
      settings.colors.sidebarTextColor || DEFAULT_PERSONALIZATION.colors.sidebarTextColor;

    if (settings.colors.autoTextColor !== false && !settings.colors.useCustomSidebarText) {
      if (sidebarBgHSL) {
        const isDarkBg = calculateRelativeLuminance(sidebarBgColor) < 0.5;
        textColor = isDarkBg ? SIDEBAR_TEXT_ON_DARK : SIDEBAR_TEXT_ON_LIGHT;
      }
    }
    // Derived from whichever text colour won above, in every branch, so an unparseable sidebar
    // colour cannot leave near-white text over a near-black muted tier.
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
  // already right; this keeps the attribute in step when the organization's choice loads or changes.
  React.useEffect(() => {
    if (typeof document === "undefined" || !ownsIllustrations) return;
    document.documentElement.dataset.illustrations = settings.illustrations;
  }, [settings.illustrations, ownsIllustrations]);

  // Rewrite the hint cookie on every visual change so the next load does not flash.
  React.useEffect(() => {
    setPersonalizationCookie(settings, ownsIllustrations, byteUnits);
  }, [settings.colors, settings.branding.appName, settings.theme, settings.illustrations, ownsIllustrations, byteUnits]);

  return (
    <PersonalizationContext.Provider value={value}>
      <ByteUnitsContext.Provider value={byteUnits}>
        {children}
      </ByteUnitsContext.Provider>
    </PersonalizationContext.Provider>
  );
}

/** WCAG relative luminance of a hex colour; 0.5 when it does not parse. */
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

/** The personalization context. Throws outside a PersonalizationProvider. */
export function usePersonalization() {
  const context = React.useContext(PersonalizationContext);
  if (context === undefined) {
    throw new Error("usePersonalization must be used within a PersonalizationProvider");
  }
  return context;
}

/** The logo for the current theme and sidebar colours. */
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
    
    // Auto: a custom sidebar background decides first.
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
    
    // Otherwise follow the effective theme.
    if (effectiveTheme === "light") {
      return settings.logo.lightModeLogo || "/orca-logo-black.png";
    }
    return settings.logo.darkModeLogo || "/orca-logo-white.png";
  }, [effectiveTheme, settings.logo, settings.colors.sidebarLogoColor, settings.colors.useCustomSidebarBg, settings.colors.sidebarBgColor]);
}
