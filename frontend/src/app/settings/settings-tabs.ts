export const SETTINGS_TABS = [
  "general",
  "integrations",
  "user-groups",
  "personalization",
  "dashboard-host",
] as const;

export type SettingsTab = (typeof SETTINGS_TABS)[number];

export function isSettingsTab(value: string): value is SettingsTab {
  return SETTINGS_TABS.some((tab) => tab === value);
}

export function settingsTabPath(tab: SettingsTab): string {
  return tab === "general" ? "/settings" : `/settings/${tab}`;
}
