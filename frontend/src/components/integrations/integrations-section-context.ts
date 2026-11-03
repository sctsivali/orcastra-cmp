"use client";

import { createContext, useContext } from "react";

export const INTEGRATIONS_SECTIONS = ["keys", "organizations", "desktop-control"] as const;
export type IntegrationsSection = (typeof INTEGRATIONS_SECTIONS)[number];

/** Its own module so a key card can reach the tab shell without importing it (a cycle). */
export const RevealIntegrationsSectionContext = createContext<((section: IntegrationsSection) => void) | null>(null);

/**
 * Switch the Integrations tab to a section and put focus on it, ex; from a key card's "Open
 * Synced Organizations". Null outside the tab, where the caller falls back to scrolling.
 */
export function useRevealIntegrationsSection() {
  return useContext(RevealIntegrationsSectionContext);
}
