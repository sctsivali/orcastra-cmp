import { SettingsNoticeProvider } from "./components/settings-notice";

export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  return <SettingsNoticeProvider>{children}</SettingsNoticeProvider>;
}
