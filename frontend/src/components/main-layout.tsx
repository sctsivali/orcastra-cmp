"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { Header } from "./header";
import { Sidebar } from "./sidebar/sidebar";
import { useLayout } from "./layout-provider";
import { usePersonalization } from "./personalization-provider";
import { cn } from "@/lib/utils";
import { usePathname } from "next/navigation";
import { BreakGlassModal } from "@/components/break-glass/break-glass-modal";
import { useNavigationProgress } from "./navigation/navigation-progress";
import { TaskPanel } from "@/components/task-panel";
import { AccessBlockedNotice } from "@/components/access-blocked-notice";
import { VaultNotice } from "@/components/vault-notice";
import { useAccessGate } from "@/hooks/use-access-gate";
import { LoadingLottiePage } from "@/components/loading-lottie";

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

export function MainLayout({ children }: { children: React.ReactNode }) {
  const { sidebarCollapsed } = useLayout();
  const { settings } = usePersonalization();
  const pathname = usePathname();
  const { pending: navPending } = useNavigationProgress();
  const isAuthPage = pathname.startsWith("/auth");
  const isLandingPage = pathname === "/";
  const access = useAccessGate({ enabled: !isAuthPage });
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

  // Issue #17: before any of the shell, so a caller with no role or no cluster access never
  // mounts the sidebar, the task panel or a page whose reads the backend would refuse.
  if (access.state === "checking") {
    return <LoadingLottiePage />;
  }
  if (access.state !== "allowed") {
    return (
      <AccessBlockedNotice
        state={access.state}
        email={access.email}
        rechecking={access.rechecking}
        onCheckAgain={access.checkAgain}
      />
    );
  }

  return (
    <div className="relative flex min-h-[100dvh]">
      {/* Hide sidebar/header on full-page terminal/console views */}
      {!isFullPageTerminalOrConsole && <Sidebar />}
      <div
        className={cn(
          // Scope the transition to margin. transition-all lerps every animatable property,
          // which makes a personalization sidebar background fade on every collapse.
          // A wide table contributes its min-content width through this flex item. Without
          // min-w-0, mobile browsers expand the layout viewport to that desktop-sized width,
          // which also centres fixed dialogs outside the visual viewport.
          "flex min-w-0 flex-1 flex-col transition-[margin] duration-200 ease-out motion-reduce:transition-none",
          // Prevent content from affecting sidebar positioning.
          // note: clip, not hidden. `overflow-x-hidden` makes this column a scroll container,
          // and the sticky task panel would then pin to the column instead of the viewport.
          "overflow-x-clip",
          // On full-page views, no margin. Otherwise apply sidebar margins
          // Use clean conditional to avoid tailwind-merge breakpoint conflicts
          !isFullPageTerminalOrConsole && (sidebarCollapsed ? "md:ml-16" : "md:ml-64")
        )}
      >
        {/* Hide header on full-page terminal/console views */}
        {!isFullPageTerminalOrConsole && <Header />}
        {/* Issue #15: a sealed or unreachable Vault, said on every page rather than read as "no clusters" */}
        {!isFullPageTerminalOrConsole && <VaultNotice />}
        <main
          aria-busy={navPending || undefined}
          className={cn(
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
                  <p className="text-center sm:text-left">{appName}</p>
                  {!isLandingPage && (
                    <span className="text-xs opacity-70">
                      v{APP_VERSION}
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
        {/* Bottom task dock: sticky in flow after the footer so the footer stays reachable */}
        {!isFullPageTerminalOrConsole && <TaskPanel />}
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

      {/* Break-glass prompt - shown when an admin opens a console/terminal they must justify */}
      <BreakGlassModal />
    </div>
  );
}
