"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { Header } from "./header";
import { Sidebar } from "./sidebar";
import { useLayout } from "./layout-provider";
import { usePersonalization } from "./personalization-provider";
import { cn } from "@/lib/utils";
import { usePathname } from "next/navigation";
import { CommandPalette } from "./sidebar/command-palette";
import { TopProgressBar } from "./top-progress-bar";
import { useSidebarShortcuts } from "./sidebar/use-sidebar-shortcuts";

// Dynamically import TerminalManager to avoid SSR issues with xterm
const TerminalManager = dynamic(
  () => import("./terminal/terminal-manager").then((mod) => mod.TerminalManager),
  { ssr: false }
);

// Dynamically import ConsoleManager to avoid SSR issues with SPICE
const ConsoleManager = dynamic(
  () => import("./console/console-manager").then((mod) => mod.ConsoleManager),
  { ssr: false }
);

// Version embedded at build time from APP_VERSION env or package.json via next.config.js
const APP_VERSION = process.env.NEXT_PUBLIC_APP_VERSION || "dev";
// Edition label is derived from the build-time auth mode (baked via Dockerfile ARG, so it is
// not affected by the runtime placeholder swap). Tagged onto the version - not the app name,
// which users can override via branding - so the edition is always visible for support.
const IS_MINI = process.env.NEXT_PUBLIC_AUTH_MODE === "client-cert";

export function MainLayout({ children }: { children: React.ReactNode }) {
  const { sidebarCollapsed } = useLayout();
  const { settings } = usePersonalization();
  const pathname = usePathname();
  // Global keyboard shortcuts (Cmd/Ctrl+B toggle, Cmd/Ctrl+K palette)
  useSidebarShortcuts();
  const isAuthPage = pathname.startsWith("/auth");
  const isLandingPage = pathname === "/";
  const [mounted, setMounted] = React.useState(false);
  
  // Client-side only mounting to avoid hydration issues with Date
  React.useEffect(() => {
    setMounted(true);
  }, []);
  
  // Detect if we're on a full-page terminal or console view (opened in new tab)
  // These pages have their own terminal/console rendering, so we skip the managers
  const isFullPageTerminalOrConsole = pathname.startsWith("/terminal/") || pathname.startsWith("/console/");
  
  // Safe footer text with year replacement - only on client
  const footerText = React.useMemo(() => {
    const text = settings?.branding?.footerText || "© {YEAR} ORCASTRA";
    if (mounted) {
      return text.replace("{YEAR}", new Date().getFullYear().toString());
    }
    // During SSR, return text without year replacement
    return text.replace("{YEAR}", "2025");
  }, [settings?.branding?.footerText, mounted]);
  
  const appName = settings?.branding?.appName || "Orcastra";

  if (isAuthPage) {
    return <>{children}</>;
  }

  return (
    <div className="relative flex min-h-[100dvh]">
      {/* Thin top bar for route-navigation feedback (instant; the whale covers data fetch) */}
      <TopProgressBar />
      {/* Hide sidebar/header on full-page terminal/console views */}
      {!isFullPageTerminalOrConsole && <Sidebar />}
      <div
        className={cn(
          "flex flex-1 flex-col transition-[margin] duration-200 ease-out motion-reduce:transition-none",
          // Prevent content from affecting sidebar positioning
          "overflow-x-hidden",
          // On full-page views, no margin. Otherwise apply sidebar margins
          // Use clean conditional to avoid tailwind-merge breakpoint conflicts
          !isFullPageTerminalOrConsole && (sidebarCollapsed ? "md:ml-16" : "md:ml-64")
        )}
      >
        {/* Hide header on full-page terminal/console views */}
        {!isFullPageTerminalOrConsole && <Header />}
        <main className={cn(
          "flex-1",
          // Full-page terminal/console views need no padding - they manage their own layout
          isFullPageTerminalOrConsole ? "" : "container py-4 px-4 md:py-6 md:px-6 lg:py-8 lg:px-8"
        )}>{children}</main>
        {/* Hide footer on full-page terminal/console views */}
        {!isFullPageTerminalOrConsole && (
          <footer className={cn(
            "border-t py-4 md:py-6",
            isLandingPage && "border-transparent" // Subtle footer on landing
          )}>
            <div className="container px-4 md:px-6">
              <div className="flex flex-col sm:flex-row items-center justify-between gap-2 text-sm text-muted-foreground">
                <div className="flex flex-col sm:flex-row items-center gap-2 sm:gap-4">
                  <p className="text-center sm:text-left">{appName} Dashboard</p>
                  {!isLandingPage && (
                    <span className="text-xs opacity-70">
                      v{APP_VERSION}{IS_MINI ? " - Mini" : ""}
                    </span>
                  )}
                </div>
                <p className="text-center sm:text-right" suppressHydrationWarning>
                  {footerText}
                </p>
              </div>
            </div>
          </footer>
        )}
      </div>
      
      {/* Terminal/Console Managers - only render when NOT on full-page views */}
      {/* Full-page terminal/console views have their own rendering */}
      {!isFullPageTerminalOrConsole && (
        <>
          {/* Terminal Manager - renders floating terminal windows */}
          <TerminalManager />

          {/* Console Manager - renders floating console windows (SPICE for VMs) */}
          <ConsoleManager />
        </>
      )}

      {/* Command palette (Cmd/Ctrl+K) - mounted globally so it works even on
          full-page terminal/console views where the sidebar is hidden */}
      <CommandPalette />
    </div>
  );
}
