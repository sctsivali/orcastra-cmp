"use client";

import * as React from "react";
import { useSession } from "next-auth/react";
import { DEFAULT_PERSONALIZATION, type PersonalizationSettings, type PersonalizationContextType } from "@/types/personalization";
import { hasMappedRole } from "@/types/rbac";
import { hexToHSL } from "@/lib/color";
import {
  accentForeground,
  accentHueShift,
  hslTriplet,
  isHexColor,
  mutedForText,
  relativeLuminance,
  resolvedSidebarText,
} from "@/lib/personalization-colors";
import { PERSONALIZATION_COOKIE, parseByteUnitsHint, parsePersonalizationHint, toPersonalizationHint } from "@/lib/personalization-cookie";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { ByteUnitsContext } from "@/hooks/use-byte-format";
import type { ByteUnitSystem } from "@/lib/byte-units";
import { setRequestByteUnits } from "@/lib/api";
import { usePersonalizationSync, type ThemeOrigin } from "./personalization/use-personalization-sync";
import { usePersonalizationMutations } from "./personalization/use-personalization-mutations";

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
    // important: only a complete #RRGGBB reaches the variables. The editor's hex field hands over
    // what is typed, and a partial value would paint the sidebar pill transparent while the
    // buttons, which read the triplet, kept the previous accent.
    const accentColor = isHexColor(settings.colors.accentColor)
      ? settings.colors.accentColor
      : null;
    const sidebarBgColor = isHexColor(settings.colors.sidebarBgColor)
      ? settings.colors.sidebarBgColor
      : null;

    // The accent drives buttons and the active menu item, with text that reads on it.
    if (accentColor) {
      root.style.setProperty("--personalization-accent", accentColor);
      const accentHSL = hexToHSL(accentColor);
      if (accentHSL) {
        root.style.setProperty("--personalization-accent-h", String(accentHSL.h));
        root.style.setProperty("--personalization-accent-s", `${accentHSL.s}%`);
        root.style.setProperty("--personalization-accent-l", `${accentHSL.l}%`);
      }
      const foreground = accentForeground(accentColor);
      root.style.setProperty("--personalization-accent-foreground", foreground);
      const foregroundTriplet = hslTriplet(foreground);
      if (foregroundTriplet) root.style.setProperty("--primary-foreground", foregroundTriplet);
      // Brand artwork (loader, card illustrations) turns by this many degrees.
      root.style.setProperty("--personalization-accent-hue-shift", `${accentHueShift(accentColor)}deg`);
    }

    if (sidebarBgColor) {
      root.style.setProperty("--personalization-sidebar-bg", sidebarBgColor);
      const sidebarBgHSL = hexToHSL(sidebarBgColor);
      if (sidebarBgHSL) {
        root.style.setProperty("--personalization-sidebar-bg-h", String(sidebarBgHSL.h));
        root.style.setProperty("--personalization-sidebar-bg-s", `${sidebarBgHSL.s}%`);
        root.style.setProperty("--personalization-sidebar-bg-l", `${sidebarBgHSL.l}%`);
      }
    }

    // The text colour the sidebar renders (auto picks from the ground), and its muted tier, derived
    // from whichever colour won so an unparseable ground cannot leave near-white text over a
    // near-black muted tier.
    const textColor = resolvedSidebarText(settings.colors);
    if (isHexColor(textColor)) {
      root.style.setProperty("--personalization-sidebar-text", textColor);
      root.style.setProperty("--personalization-sidebar-muted", mutedForText(textColor));
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
      const bgLuminance = relativeLuminance(
        settings.colors.sidebarBgColor || DEFAULT_PERSONALIZATION.colors.sidebarBgColor,
      );
      const isDarkBg = (bgLuminance ?? 0.5) < 0.5;
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
