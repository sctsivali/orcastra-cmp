import { editionShows } from "@/lib/edition";

export const SETTINGS_TABS = [
  "general",
  "integrations",
  "user-groups",
  "personalization",
  "dashboard-host",
] as const;

export type SettingsTab = (typeof SETTINGS_TABS)[number];

// A tab the edition leaves out is not a tab at all: its deep link is a 404 like any unknown tab.
export function isSettingsTab(value: string): value is SettingsTab {
  if (value === "user-groups" && !editionShows("settings.user-groups")) return false;
  return SETTINGS_TABS.some((tab) => tab === value);
}

export function settingsTabPath(tab: SettingsTab): string {
  return tab === "general" ? "/settings" : `/settings/${tab}`;
}
