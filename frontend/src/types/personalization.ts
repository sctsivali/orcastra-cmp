/** Branding and appearance settings, their presets and the product defaults. */

export interface LogoSettings {
  lightModeLogo: string | null; // Base64 or URL
  darkModeLogo: string | null;  // Base64 or URL
  favicon: string | null;
}

/** Background of the landing page. */
export interface BackgroundSettings {
  backgroundImage: string | null;      // Base64 or URL
  backgroundOverlayOpacity: number;    // Percent, 0-100
  backgroundBlur: number;              // Pixels, 0-20
}

export interface BrandingSettings {
  appName: string;
  tagline: string;            // Shown on the landing page
  footerText: string;         // "{YEAR}" is replaced with the current year
}

export interface ColorPreset {
  name: string;
  value: string;
}

export interface ThemePreset {
  name: string;
  accentColor: string;      // Buttons and active states
  sidebarBgColor: string;
  sidebarTextColor: string;
}

export interface ColorSettings {
  accentColor: string;           // Buttons and the active sidebar item
  sidebarBgColor: string;
  sidebarTextColor: string;      // Manual override
  sidebarLogoColor: "auto" | "black" | "white";
  useCustomAccent: boolean;
  useCustomSidebarBg: boolean;
  useCustomSidebarText: boolean;
  autoTextColor: boolean;        // Derive the text colour from the background's brightness
}

export const COLOR_PRESETS: ColorPreset[] = [
  { name: "Violet", value: "#8467e4" },
  { name: "Purple", value: "#a855f7" },
  { name: "Blue", value: "#3b82f6" },
  { name: "Indigo", value: "#6366f1" },
  { name: "Cyan", value: "#06b6d4" },
  { name: "Teal", value: "#14b8a6" },
  { name: "Green", value: "#22c55e" },
  { name: "Emerald", value: "#10b981" },
  { name: "Orange", value: "#f97316" },
  { name: "Red", value: "#ef4444" },
  { name: "Pink", value: "#ec4899" },
  { name: "Rose", value: "#f43f5e" },
];

// Sidebar background and accent combinations. Each sets its text colour explicitly so it stays
// legible in both light and dark mode.
export const THEME_PRESETS: ThemePreset[] = [
  // Identical to DEFAULT_PERSONALIZATION.colors. personalization-settings.tsx marks a preset
  // active only when all three values match, so this entry is how a fresh account sees its
  // preset selected and gets back to the default look.
  { name: "Orcastra", accentColor: "#8467e4", sidebarBgColor: "#321f6f", sidebarTextColor: "#f8fafc" },
  { name: "Classic Light", accentColor: "#8b5cf6", sidebarBgColor: "#ffffff", sidebarTextColor: "#1e293b" },
  { name: "Ocean", accentColor: "#06b6d4", sidebarBgColor: "#f0fdfa", sidebarTextColor: "#134e4a" },
  { name: "Forest", accentColor: "#22c55e", sidebarBgColor: "#f0fdf4", sidebarTextColor: "#14532d" },
  { name: "Sunset", accentColor: "#f97316", sidebarBgColor: "#fff7ed", sidebarTextColor: "#7c2d12" },
  { name: "Berry", accentColor: "#ec4899", sidebarBgColor: "#fdf2f8", sidebarTextColor: "#831843" },
  { name: "Midnight", accentColor: "#6366f1", sidebarBgColor: "#1e1b4b", sidebarTextColor: "#e0e7ff" },
];

// Sidebar background color presets
export const SIDEBAR_BG_PRESETS: ColorPreset[] = [
  { name: "White", value: "#ffffff" },
  { name: "Light Gray", value: "#f8fafc" },
  { name: "Warm White", value: "#fefce8" },
  { name: "Cool Gray", value: "#f1f5f9" },
  { name: "Soft Cyan", value: "#f0fdfa" },
  { name: "Soft Green", value: "#f0fdf4" },
  { name: "Soft Orange", value: "#fff7ed" },
  { name: "Soft Pink", value: "#fdf2f8" },
  // The product default. It has to be listed: the picker marks a swatch selected by exact
  // value match.
  { name: "Orcastra Violet", value: "#321f6f" },
  // The dark theme's own background, for a sidebar that sits flush with the page.
  { name: "Orcastra Dark", value: "#14111c" },
  { name: "Dark Navy", value: "#1e1b4b" },
  { name: "Dark Slate", value: "#1e293b" },
  { name: "Dark Gray", value: "#18181b" },
  { name: "True Black", value: "#000000" },
];

export type ThemePreference = "dark" | "light" | "system";

/** Whether cards show their illustration (components/illustrations). */
export type IllustrationPreference = "show" | "hide";

export function isIllustrationPreference(value: unknown): value is IllustrationPreference {
  return value === "show" || value === "hide";
}

export interface PersonalizationSettings {
  logo: LogoSettings;
  background: BackgroundSettings;
  branding: BrandingSettings;
  colors: ColorSettings;
  theme: ThemePreference;
  illustrations: IllustrationPreference;
  lastModified: string | null;   // ISO date string
}

export const DEFAULT_PERSONALIZATION: PersonalizationSettings = {
  logo: {
    lightModeLogo: null,
    darkModeLogo: null,
    favicon: null,
  },
  background: {
    backgroundImage: null,
    backgroundOverlayOpacity: 85,
    backgroundBlur: 0,
  },
  branding: {
    appName: "Orcastra",
    tagline: "Operations center for managing virtualized infrastructure and containers across distributed clusters. Streamline resource management with real-time monitoring and automation.",
    footerText: "© {YEAR} ORCASTRA",
  },
  // important: the CSS seeds in app/globals.css, SIDEBAR_TEXT_ON_DARK in the provider and the
  // backend's routers/user_preferences.py (what a user with no stored row receives) repeat these
  // colours; the provider and the picker fall back to this object.
  // scripts/checks/check-personalization-defaults.mjs fails the build when they disagree.
  colors: {
    accentColor: "#8467e4",       // 254 70% 65%, the sidebar ground's hue
    sidebarBgColor: "#321f6f",    // 254 56% 28%
    sidebarTextColor: "#f8fafc",  // What autoTextColor derives for a ground this dark
    sidebarLogoColor: "auto",     // Follows the sidebar background's brightness
    useCustomAccent: false,
    useCustomSidebarBg: true,     // The product ships a coloured sidebar; false falls back to bg-card
    useCustomSidebarText: false,
    autoTextColor: true,
  },
  theme: "system",
  // Off until an organization or the deployment turns them on in Personalization; this is also
  // what the first render shows when nothing is set.
  illustrations: "hide",
  lastModified: null,
};

/**
 * The app-wide context: the branding this account sees (resolved on the server from its
 * organization, that organization's provider and the deployment default) with the account's own
 * theme choice over it. Branding is edited through BrandingEditorContextType, never here.
 */
export interface PersonalizationContextType {
  settings: PersonalizationSettings;
  /** Persist this account's own theme choice. */
  updateTheme: (theme: ThemePreference) => void;
  /** Drop the account's own choice so the organization's default theme applies. */
  clearTheme: () => void;
  /** Whether the theme in `settings` is the account's own choice rather than its organization's. */
  themeIsPersonal: boolean;
  /** The theme the organization (or deployment) sets, which applies when the account chose none. */
  organizationTheme: ThemePreference;
  /** Show branding at once (the editor's preview of the organization the sidebar renders). */
  applySettingsOptimistically: (settings: PersonalizationSettings) => void;
  refreshSettings: () => Promise<void>;
  isLoading: boolean;
  isSaving: boolean;
}

/** What the branding editor (components/personalization-settings.tsx) edits: one organization's or the deployment's row. */
export interface BrandingEditorContextType {
  settings: PersonalizationSettings;
  updateLogo: (logo: Partial<LogoSettings>) => void;
  updateBackground: (background: Partial<BackgroundSettings>) => void;
  updateBranding: (branding: Partial<BrandingSettings>) => void;
  updateColors: (colors: Partial<ColorSettings>) => void;
  /** The default theme for everyone this branding applies to; each account can still choose its own. */
  updateDefaultTheme: (theme: ThemePreference) => void;
  /** Whether cards show their illustration for everyone this branding applies to (no per-account choice). */
  updateDefaultIllustrations: (choice: IllustrationPreference) => void;
  resetToDefaults: () => void;
  /** What the reset button says: it removes this row, so it names what shows instead. */
  resetLabel: string;
  undoSettings: () => void;
  redoSettings: () => void;
  canUndo: boolean;
  canRedo: boolean;
  isLoading: boolean;
  isSaving: boolean;
}
