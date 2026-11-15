import { hexToHSL } from "@/lib/color";
import { DEFAULT_PERSONALIZATION, type ColorSettings } from "@/types/personalization";

// The two sidebar text pairs autoTextColor picks between, and the alpha tiers that go with them.
// app/globals.css seeds the same values for the pre-paint path, app/layout.tsx spells them in its
// inline script, and scripts/checks/check-personalization-defaults.mjs holds the copies equal.
export const SIDEBAR_TEXT_ON_DARK = "#f8fafc";
export const SIDEBAR_TEXT_ON_LIGHT = "#1e293b";
export const SIDEBAR_MUTED_ON_DARK = "rgba(248,250,252,0.6)";
export const SIDEBAR_MUTED_ON_LIGHT = "rgba(30,41,59,0.5)";

// Text on an accent surface (buttons, the active menu item): white unless the accent is light
// enough that the dark text reads better. The shipped accent keeps white.
export const ACCENT_TEXT_LIGHT = "#ffffff";
export const ACCENT_TEXT_DARK = "#1e293b";

const HEX = /^#[0-9a-fA-F]{6}$/;

/** A complete #RRGGBB value, the only shape the backend stores and the CSS variables take. */
export function isHexColor(value: unknown): value is string {
  return typeof value === "string" && HEX.test(value);
}

/** Two colours are the same when their hexes match, whatever the letter case. */
export function sameColor(a: string | null | undefined, b: string | null | undefined): boolean {
  return isHexColor(a) && isHexColor(b) && a.toLowerCase() === b.toLowerCase();
}

/** WCAG relative luminance of a #RRGGBB colour, or null when it is not one. */
export function relativeLuminance(hex: string): number | null {
  if (!isHexColor(hex)) return null;
  const channel = (offset: number) => {
    const v = parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

export function contrastRatio(a: string, b: string): number | null {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  if (la === null || lb === null) return null;
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** The text colour on an accent surface: whichever of the two pairs reads better on it. */
export function accentForeground(accent: string): string {
  const light = contrastRatio(ACCENT_TEXT_LIGHT, accent);
  const dark = contrastRatio(ACCENT_TEXT_DARK, accent);
  if (light === null || dark === null) return ACCENT_TEXT_LIGHT;
  return dark > light ? ACCENT_TEXT_DARK : ACCENT_TEXT_LIGHT;
}

/** The sidebar text colour the sidebar renders: auto picks from the ground's brightness. */
export function resolvedSidebarText(colors: ColorSettings): string {
  const stored = colors.sidebarTextColor || DEFAULT_PERSONALIZATION.colors.sidebarTextColor;
  if (colors.autoTextColor === false || colors.useCustomSidebarText) return stored;
  const ground = colors.sidebarBgColor || DEFAULT_PERSONALIZATION.colors.sidebarBgColor;
  const luminance = relativeLuminance(ground);
  if (luminance === null) return stored;
  return luminance < 0.5 ? SIDEBAR_TEXT_ON_DARK : SIDEBAR_TEXT_ON_LIGHT;
}

/** The muted tier that belongs with a sidebar text colour. */
export function mutedForText(textColor: string): string {
  const hsl = hexToHSL(textColor);
  if (!hsl) return SIDEBAR_MUTED_ON_DARK;
  return hsl.l < 50 ? SIDEBAR_MUTED_ON_LIGHT : SIDEBAR_MUTED_ON_DARK;
}

/**
 * How far the accent's hue sits from the shipped accent's, in degrees within (-180, 180].
 * Brand artwork (the loader, the card illustrations) rotates by this, so the default accent
 * leaves it exactly as drawn and any other accent turns it to the same hue family.
 */
export function accentHueShift(accent: string): number {
  const current = hexToHSL(accent);
  const shipped = hexToHSL(DEFAULT_PERSONALIZATION.colors.accentColor);
  if (!current || !shipped) return 0;
  let shift = current.h - shipped.h;
  if (shift > 180) shift -= 360;
  if (shift <= -180) shift += 360;
  return shift;
}

/** "h s% l%", the triplet shadcn tokens such as --primary-foreground take. */
export function hslTriplet(hex: string): string | null {
  const hsl = hexToHSL(hex);
  return hsl ? `${hsl.h} ${hsl.s}% ${hsl.l}%` : null;
}

/** An [r, g, b] colour in 0..1 with its hue turned by `degrees`, saturation and lightness kept. */
export function rotateRgbHue(rgb: readonly number[], degrees: number): number[] {
  const [r, g, b] = rgb;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min || !degrees) return [r, g, b];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  h = (((h / 6) * 360 + degrees) % 360 + 360) % 360 / 360;
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const channel = (t: number) => {
    const u = t < 0 ? t + 1 : t > 1 ? t - 1 : t;
    if (u < 1 / 6) return p + (q - p) * 6 * u;
    if (u < 1 / 2) return q;
    if (u < 2 / 3) return p + (q - p) * (2 / 3 - u) * 6;
    return p;
  };
  return [channel(h + 1 / 3), channel(h), channel(h - 1 / 3)];
}

/** The accent hue shift the root carries now (set by the provider and the pre-paint script). */
export function currentAccentHueShift(): number {
  if (typeof document === "undefined") return 0;
  const raw = getComputedStyle(document.documentElement).getPropertyValue("--personalization-accent-hue-shift");
  const value = parseFloat(raw);
  return Number.isFinite(value) ? value : 0;
}
