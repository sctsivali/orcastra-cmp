"use client";

import Image from "next/image";
import { ShieldCheck } from "lucide-react";
import { usePublicBranding } from "./public-branding-context";

export const BUILT_IN_NAME = "Orcastra CMP";

/** The deployment's name and logo on the sign-in and error pages, or the built-in mark. */
export function AuthBrandMark({ withoutLogo = "icon" }: { withoutLogo?: "icon" | "nothing" }) {
  const branding = usePublicBranding();
  const light = branding?.logo_light ?? branding?.logo_dark;
  const dark = branding?.logo_dark ?? branding?.logo_light;
  const name = branding?.app_name ?? BUILT_IN_NAME;
  if (!light || !dark) {
    if (withoutLogo === "nothing") return null;
    return (
      <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10">
        <ShieldCheck className="h-8 w-8 text-primary" aria-hidden="true" />
      </div>
    );
  }
  // Both logos render, switched by the `dark` class the pre-paint script puts on <html>. Choosing
  // one in JavaScript would render the light logo on the server and swap it after hydration.
  return (
    <div className="mx-auto flex h-16 items-center justify-center">
      {[{ src: light, className: "dark:hidden" }, { src: dark, className: "hidden dark:block" }].map((logo) => (
        <Image
          key={logo.className}
          src={logo.src}
          alt={`${name} logo`}
          width={192}
          height={64}
          priority
          unoptimized
          className={`h-16 w-auto max-w-[240px] object-contain ${logo.className}`}
        />
      ))}
    </div>
  );
}

/** The deployment's footer line, with {YEAR} filled in. Null when none is set. */
export function useAuthFooterText(): string | null {
  const branding = usePublicBranding();
  return branding?.footer_text ? branding.footer_text.replace("{YEAR}", String(new Date().getFullYear())) : null;
}
