import { notFound, redirect } from "next/navigation";
import { SettingsView } from "../settings-view";
import { isSettingsTab, SETTINGS_TABS } from "../settings-tabs";

export const dynamicParams = false;

export function generateStaticParams() {
  return SETTINGS_TABS.map((tab) => ({ tab }));
}

export default async function SettingsTabPage({
  params,
}: {
  params: Promise<{ tab: string }>;
}) {
  const { tab } = await params;
  if (!isSettingsTab(tab)) notFound();
  if (tab === "general") redirect("/settings");
  return <SettingsView tab={tab} />;
}
