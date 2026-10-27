"use client";

import * as React from "react";
import type { BrandingEditorContextType } from "@/types/personalization";

/** The row the branding editor is writing: one organization's, or the deployment default. */
export const BrandingEditorContext = React.createContext<BrandingEditorContextType | undefined>(undefined);

export function useBrandingEditor(): BrandingEditorContextType {
  const context = React.useContext(BrandingEditorContext);
  if (context === undefined) {
    throw new Error("useBrandingEditor must be used inside a BrandingEditorContext provider");
  }
  return context;
}
