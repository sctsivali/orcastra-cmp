/**
 * Auth Layout
 *
 * The sign-in and error pages. The global app shell suppresses sidebar/header/footer for /auth/*.
 * Branded here, on the server, with the deployment default an Administrator set, so the first
 * byte already carries the name, logo and accent; nobody is signed in yet to have anything else.
 */

import type { CSSProperties, ReactNode } from "react";
import { PublicBrandingProvider } from "@/components/branding/public-branding-context";
import { hexToHSL } from "@/lib/color";
import { getPublicBranding } from "@/lib/server/public-branding";

function accentStyle(accent: string | null | undefined): CSSProperties | undefined {
  const hsl = accent ? hexToHSL(accent) : null;
  if (!hsl) return undefined;
  const triplet = `${hsl.h} ${hsl.s}% ${hsl.l}%`;
  // --primary and --ring are bound on :root, so they are set here directly rather than through
  // the --personalization-accent-* variables, which a descendant cannot re-resolve.
  return { "--primary": triplet, "--ring": triplet } as CSSProperties;
}

export default async function AuthLayout({ children }: { children: ReactNode }) {
  const branding = await getPublicBranding();
  return (
    <PublicBrandingProvider value={branding}>
      <div style={accentStyle(branding?.accent_color)}>{children}</div>
    </PublicBrandingProvider>
  );
}
