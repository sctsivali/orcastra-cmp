/**
 * Personalization Types for Orcastra CMP
 * Allows users to customize branding and appearance
 */

// Logo settings for different themes
export interface LogoSettings {
  lightModeLogo: string | null; // Base64 or URL for light mode logo
  darkModeLogo: string | null;  // Base64 or URL for dark mode logo
  favicon: string | null;       // Custom favicon
}

// Background settings for landing page
export interface BackgroundSettings {
  backgroundImage: string | null;      // Base64 or URL for background image
  backgroundOverlayOpacity: number;    // 0-100 opacity for overlay
  backgroundBlur: number;              // 0-20 blur level
}

// Branding text settings
export interface BrandingSettings {
  appName: string;            // Application name (default: "Orcastra")
  tagline: string;            // Tagline on landing page
  footerText: string;         // Footer copyright text
}

// Color preset type for accent colors
export interface ColorPreset {
  name: string;
  value: string;
}

// Visual theme preset with sidebar background
export interface ThemePreset {
  name: string;
  accentColor: string;      // Unified accent for buttons and active states
  sidebarBgColor: string;   // Sidebar background color
  sidebarTextColor: string; // Sidebar text color for this preset
}

// Accent/Theme color settings
export interface ColorSettings {
  accentColor: string;           // Unified accent color (buttons, active sidebar)
  sidebarBgColor: string;        // Sidebar background color
  sidebarTextColor: string;      // Sidebar text color (manual override)
  sidebarLogoColor: "auto" | "black" | "white"; // Logo color preference for sidebar
  useCustomAccent: boolean;      // Whether using custom accent color
  useCustomSidebarBg: boolean;   // Whether using custom sidebar bg
  useCustomSidebarText: boolean; // Whether using custom text color
  autoTextColor: boolean;        // Auto-calculate text color based on bg brightness
}

// Available accent color presets
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

// Visual theme presets (like image 5) - sidebar bg + accent combinations
// Text colors are explicitly set to ensure readability in both light/dark modes
export const THEME_PRESETS: ThemePreset[] = [
  // First, and identical to DEFAULT_PERSONALIZATION.colors. personalization-settings.tsx marks
  // a preset active by matching all three values, so without this entry a fresh account shows
  // no preset selected at all and there is no way back to the shipped look short of typing
  // three hexes.
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
  // The shipped default. First of the dark grounds because it is the one an operator returns
  // to, and present at all because the picker marks a swatch selected by exact value match.
  { name: "Orcastra Violet", value: "#321f6f" },
  // Matches the dark theme's own ground, so an operator who wants the sidebar to sit flush
  // with the page has a preset for it rather than having to type the value.
  { name: "Orcastra Dark", value: "#14111c" },
  { name: "Dark Navy", value: "#1e1b4b" },
  { name: "Dark Slate", value: "#1e293b" },
  { name: "Dark Gray", value: "#18181b" },
  { name: "True Black", value: "#000000" },
];

// Theme preference type
export type ThemePreference = "dark" | "light" | "system";

// Complete personalization settings
export interface PersonalizationSettings {
  logo: LogoSettings;
  background: BackgroundSettings;
  branding: BrandingSettings;
  colors: ColorSettings;
  theme: ThemePreference;          // User's theme preference (dark/light/system)
  lastModified: string | null;   // ISO date string
}

// Default personalization settings
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
  // important: these five colour values are one of five copies of the same defaults. The others
  // are the CSS seeds in app/globals.css, the runtime fallbacks in
  // components/personalization-provider.tsx, the picker fallbacks in
  // components/personalization-settings.tsx, and get_default_settings() in the backend's
  // routers/user_preferences.py, which is what a user with no stored row receives.
  // scripts/checks/check-personalization-defaults.mjs fails the build when they disagree,
  // because the copies drifting is not hypothetical: the CSS accent hex and its own HSL
  // triplet described two different purples for as long as personalization has shipped.
  colors: {
    accentColor: "#8467e4",       // 254 70% 65%, the sidebar ground's hue
    sidebarBgColor: "#321f6f",    // 254 56% 28%
    sidebarTextColor: "#f8fafc",  // what autoTextColor derives for a ground this dark
    sidebarLogoColor: "auto",     // Auto = follows sidebar background brightness
    useCustomAccent: false,
    useCustomSidebarBg: true,     // The product ships a coloured sidebar; false falls back to bg-card
    useCustomSidebarText: false,
    autoTextColor: true,          // Auto-calculate text color by default
  },
  theme: "system",               // Default: follow system preference
  lastModified: null,
};

// Context type for the provider
export interface PersonalizationContextType {
  settings: PersonalizationSettings;
  updateLogo: (logo: Partial<LogoSettings>) => void;
  updateBackground: (background: Partial<BackgroundSettings>) => void;
  updateBranding: (branding: Partial<BrandingSettings>) => void;
  updateColors: (colors: Partial<ColorSettings>) => void;
  updateTheme: (theme: ThemePreference) => void;
  resetToDefaults: () => void;
  undoSettings: () => void;
  redoSettings: () => void;
  canUndo: boolean;
  canRedo: boolean;
  /** Force-apply settings without API save (for org-level instant preview) */
  applySettingsOptimistically: (settings: PersonalizationSettings) => void;
  refreshSettings: () => Promise<void>;  // Force refresh from API
  isLoading: boolean;
  isSaving: boolean;
}
