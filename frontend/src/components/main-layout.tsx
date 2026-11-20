"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { Header } from "./header";
import Link from "next/link";
import { EDITION_GATE_EXEMPT_PATHS, EditionLayoutExtras } from "./edition-slots";
import { footerVersion } from "@/lib/edition";
import { Sidebar } from "./sidebar/sidebar";
import { useLayout } from "./layout-provider";
import { usePersonalization } from "./personalization-provider";
import { cn } from "@/lib/utils";
import { usePathname } from "next/navigation";
import { BreakGlassModal } from "@/components/break-glass/break-glass-modal";
import { TransferTray } from "@/components/instance-files/transfer-tray";
import { useNavigationProgress } from "./navigation/navigation-progress";
import { TaskPanel } from "@/components/task-panel";
import { AccessBlockedNotice } from "@/components/access-blocked-notice";
import { VaultNotice } from "@/components/vault-notice";
import { TenantTermNotice } from "@/components/tenant-term-notice";
import { DocumentBranding } from "@/components/branding/document-branding";
import { ReleaseNotice } from "@/components/release/release-notice";
import { SecretsNotice } from "@/components/host-health/secrets-notice";
import { FooterReleaseHint } from "@/components/release/footer-release-hint";
import { useAccessGate } from "@/hooks/use-access-gate";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTerminalStore } from "@/lib/terminal-store";
import { useConsoleStore } from "@/lib/console-store";

// Client only: xterm cannot render on the server.
const TerminalManager = dynamic(
  () => import("./terminal/terminal-manager").then((mod) => mod.TerminalManager),
  { ssr: false }
);

// Client only: the SPICE client cannot render on the server.
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
  // important: mount each floating window manager only once its store holds a window. They pull
  // xterm, react-rnd, react-draggable and react-resizable (about 167 KB gzipped), which a cold
  // page load would otherwise download with no window open. Both stores rehydrate from
  // localStorage synchronously in the browser, so windows restored after a reload are counted on
  // the first client render.
  const hasTerminals = useTerminalStore((s) => s.terminals.length > 0);
  const hasConsoles = useConsoleStore((s) => s.consoles.length > 0);
  const [mounted, setMounted] = React.useState(false);
  
  // The footer year comes from Date, so it is filled in only after mount.
  React.useEffect(() => {
    setMounted(true);
  }, []);
  
  // Full-page terminal and console views (opened in a new tab) render their own window, so the
  // shell and the floating managers are skipped there.
  const isFullPageTerminalOrConsole = pathname.startsWith("/terminal/") || pathname.startsWith("/console/");
  
  const footerText = React.useMemo(() => {
    const text = settings?.branding?.footerText || "© {YEAR} ORCASTRA";
    if (mounted) {
      return text.replace("{YEAR}", new Date().getFullYear().toString());
    }
    // Before mount, a fixed year keeps the server and first client render identical.
    return text.replace("{YEAR}", "2025");
  }, [settings?.branding?.footerText, mounted]);
  
  const appName = settings?.branding?.appName || "Orcastra";

  if (isAuthPage) {
    return <>{children}</>;
  }

  // Before any of the shell, so a caller with no role or no cluster access never mounts the
  // sidebar, the task panel or a page whose reads the backend would refuse.
  if (access.state === "checking") {
    return <LoadingLottiePage />;
  }
  if (access.state !== "allowed" && EDITION_GATE_EXEMPT_PATHS.includes(pathname)) {
    return (
      <main className="min-h-[100dvh] bg-background">
        <div className="px-4 pt-4 sm:px-8">
          <Link href="/" className="text-sm font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">
            Back
          </Link>
        </div>
        {children}
      </main>
    );
  }
  if (access.state !== "allowed") {
    return (
      <AccessBlockedNotice
        state={access.state}
        email={access.email}
        endedAt={access.tenantAccess?.ended_at ?? null}
        rechecking={access.rechecking}
        onCheckAgain={access.checkAgain}
      />
    );
  }

  return (
    <div className="relative flex min-h-[100dvh]">
      <DocumentBranding />
      {!isFullPageTerminalOrConsole && <Sidebar />}
      <div
        className={cn(
          // Transition margin only: transition-all would also fade a personalization sidebar
          // background on every collapse. A wide table contributes its min-content width
          // through this flex item; without min-w-0, mobile browsers widen the layout viewport
          // to it, which also centres fixed dialogs outside the visual viewport.
          "flex min-w-0 flex-1 flex-col transition-[margin] duration-200 ease-out motion-reduce:transition-none",
          // Clip, not hidden: `overflow-x-hidden` makes this column a scroll container, and the
          // sticky task panel would then pin to the column instead of the viewport.
          "overflow-x-clip",
          // No margin on full-page views. One class per state avoids tailwind-merge
          // breakpoint conflicts.
          !isFullPageTerminalOrConsole && (sidebarCollapsed ? "md:ml-16" : "md:ml-64")
        )}
      >
        {!isFullPageTerminalOrConsole && <Header />}
        {/* A sealed or unreachable Vault, said on every page instead of read as "no clusters" */}
        {!isFullPageTerminalOrConsole && <VaultNotice />}
        {/* Administrators: the running version is out of date (end of life, security, deprecated, newer) */}
        {!isFullPageTerminalOrConsole && <ReleaseNotice />}
        {!isFullPageTerminalOrConsole && <SecretsNotice />}
        {/* A Tenant's paid term: read-only or ending projects, said on every page */}
        {!isFullPageTerminalOrConsole && <TenantTermNotice summary={access.tenantAccess} />}
        {!isFullPageTerminalOrConsole && <EditionLayoutExtras />}
        <main
          aria-busy={navPending || undefined}
          className={cn(
          "flex-1",
          // Full-page views manage their own layout and need no padding.
          isFullPageTerminalOrConsole ? "" : "container py-4 px-4 md:py-6 md:px-6 lg:py-8 lg:px-8"
        )}>{children}</main>
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
                      {footerVersion(APP_VERSION)}
                    </span>
                  )}
                  {!isLandingPage && <FooterReleaseHint />}
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
      
      {/* Floating terminal and console windows; full-page views render their own. */}
      {!isFullPageTerminalOrConsole && (
        <>
          {hasTerminals && <TerminalManager />}

          {hasConsoles && <ConsoleManager />}
        </>
      )}

      {/* Break-glass prompt - shown when an admin opens a console/terminal they must justify */}
      <BreakGlassModal />

      {/* Uploads outlive the explorer that started them, on full-page views too */}
      <TransferTray />
    </div>
  );
}
