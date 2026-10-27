import {
  DEFAULT_PERSONALIZATION,
  type PersonalizationSettings,
} from "@/types/personalization";

// The hint cookie carries only the values a stylesheet needs, never an image: a logo is a
// base64 data URI in the settings blob and would blow past the 4KB cookie budget on its own.
export const PERSONALIZATION_COOKIE = "orcastra-personalization";

// Deliberately two-letter keys. Every one of these rides on every request to this origin, so
// the difference between `sidebarBgColor` and `sb` is real bytes on every navigation.
export interface PersonalizationHint {
  ac?: string;
  sb?: string;
  st?: string;
  uca?: boolean;
  usb?: boolean;
  ust?: boolean;
  atc?: boolean;
  slc?: string;
  an?: string;
  th?: string;
}

export function toPersonalizationHint(settings: PersonalizationSettings): PersonalizationHint {
  return {
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
}

// important: this module carries no "use client" directive, and that is the point. The root
// layout is a server component and reads this cookie to seed the provider's first render, so
// the parser has to be importable from both sides. Importing it out of the provider instead
// would hand the server a client reference rather than a function.
//
// Takes the raw cookie VALUE, not the document.cookie string, so the server (which has a
// parsed cookie store) and the browser (which does not) share one implementation.
export function parsePersonalizationHint(
  raw: string | null | undefined,
): PersonalizationSettings | null {
  if (!raw) return null;
  try {
    const hint = JSON.parse(decodeURIComponent(raw)) as PersonalizationHint;
    if (!hint || typeof hint !== "object") return null;
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
        sidebarLogoColor:
          (hint.slc as PersonalizationSettings["colors"]["sidebarLogoColor"]) ??
          DEFAULT_PERSONALIZATION.colors.sidebarLogoColor,
      },
      branding: {
        ...DEFAULT_PERSONALIZATION.branding,
        appName: hint.an ?? DEFAULT_PERSONALIZATION.branding.appName,
      },
      theme: (hint.th as PersonalizationSettings["theme"]) ?? DEFAULT_PERSONALIZATION.theme,
    };
  } catch {
    return null;
  }
}

/**
 * Forget the hint on sign-out. It paints before any script reads the session, so on a shared
 * browser it would show the last account's organization colours on the sign-in page and on the
 * next account's first paint.
 */
export function clearPersonalizationHint(): void {
  if (typeof document === "undefined") return;
  document.cookie = `${PERSONALIZATION_COOKIE}=;path=/;max-age=0;SameSite=Lax`;
}
