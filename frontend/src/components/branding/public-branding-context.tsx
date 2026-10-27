"use client";

import * as React from "react";
import type { PublicBranding } from "@/types/branding";

const PublicBrandingContext = React.createContext<PublicBranding | null>(null);

/** Handed down by the server-rendered auth layout, so the sign-in page is branded in its first byte. */
export function PublicBrandingProvider({ value, children }: { value: PublicBranding | null; children: React.ReactNode }) {
  return <PublicBrandingContext.Provider value={value}>{children}</PublicBrandingContext.Provider>;
}

/** The deployment default branding, or null when none is set (the built-in look applies). */
export function usePublicBranding(): PublicBranding | null {
  return React.useContext(PublicBrandingContext);
}
