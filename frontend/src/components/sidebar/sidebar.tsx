"use client";

import * as React from "react";
import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { useTheme } from "../theme-provider";
import { useLayout } from "../layout-provider";
import { usePersonalization, useCurrentLogo } from "../personalization-provider";
import { usePermissions } from "@/hooks/use-permissions";
import { useRouteIntent, linkIntentProps } from "@/components/navigation/route-intent";
import { NAV_GROUPS, filterGroups, buildPrefetchMap } from "./nav-config";
import { resolveActiveHref } from "./use-active-path";
import { NavSection } from "./nav-group";
import { CollapsedRail } from "./collapsed-flyout";
import { SidebarFooter } from "./sidebar-footer";
import type { NavRenderContext } from "./types";

interface SidebarProps {
  className?: string;
}

export function Sidebar({ className }: SidebarProps) {
  const {
    sidebarCollapsed: isCollapsed,
    toggleSidebarCollapsed,
    sidebarOpen,
    setSidebarOpen,
  } = useLayout();
  const { theme, setTheme } = useTheme();
  const { settings } = usePersonalization();
  const currentLogo = useCurrentLogo(theme);
  const { isAdmin, isPartner, isTenant } = usePermissions();
  const pathname = usePathname();
  const queryClient = useQueryClient();

  const prefetchMap = React.useMemo(() => buildPrefetchMap(queryClient), [queryClient]);
  const groups = React.useMemo(
    () => filterGroups(NAV_GROUPS, { isAdmin, isPartner, isTenant }),
    [isAdmin, isPartner, isTenant]
  );
  const allHrefs = React.useMemo(
    () => groups.flatMap((g) => g.items.map((i) => i.href)),
    [groups]
  );
  const activeHref = resolveActiveHref([...allHrefs, "/settings"], pathname);

  // important: close the mobile drawer when the route commits, not when the link is clicked.
  // Closing on click unmounted the <Link> and with it the pending spinner, so a tap gave no
  // feedback at all for the whole navigation.
  React.useEffect(() => {
    setSidebarOpen(false);
  }, [pathname, setSidebarOpen]);

  const intent = useRouteIntent();
  const warmFor = React.useCallback(
    (key: string) => () => prefetchMap[key]?.(),
    [prefetchMap]
  );

  const ctx: NavRenderContext = React.useMemo(
    () => ({ activeHref, intent, warmFor }),
    [activeHref, intent, warmFor]
  );

  // Icon rail only on desktop-collapsed; the mobile drawer (sidebarOpen) is full width.
  const showRail = isCollapsed && !sidebarOpen;
  const useCustomBg = settings.colors.useCustomSidebarBg;

  return (
    <>
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-[45] bg-black/50 backdrop-blur-sm"
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
      )}

      <aside
        aria-label="Sidebar"
        className={cn(
          // Scope the transition to geometry only (width for desktop collapse, transform for the
          // mobile drawer) so personalization background-color does not lerp/flicker on collapse.
          "fixed left-0 top-0 z-50 border-r transition-[width,transform] duration-200 ease-out motion-reduce:transition-none",
          useCustomBg && "sidebar-custom-bg",
          !useCustomBg && "bg-card",
          "h-[100dvh] overscroll-contain",
          sidebarOpen && "w-64 translate-x-0",
          !sidebarOpen && "-translate-x-full md:translate-x-0",
          !sidebarOpen && isCollapsed && "md:w-16",
          !sidebarOpen && !isCollapsed && "md:w-64",
          className
        )}
        style={useCustomBg ? { backgroundColor: settings.colors.sidebarBgColor || "#ffffff" } : undefined}
      >
        <div className="flex flex-col h-full overflow-hidden overscroll-contain">
          {/* Header: logo + collapse/close */}
          <div className="flex h-16 md:h-20 items-center justify-between border-b px-3 md:px-2 shrink-0">
            <Link
              href="/"
              prefetch={false}
              {...linkIntentProps(intent, "/", warmFor("/"))}
              className={cn(
                "flex items-center justify-center rounded-lg transition-all duration-200 ease-out motion-reduce:transition-none hover:bg-accent",
                sidebarOpen
                  ? "flex-1 px-3 gap-3"
                  : isCollapsed
                    ? "w-10 h-10 md:w-12 md:h-12"
                    : "flex-1 px-3 gap-3"
              )}
            >
              <Image
                src={currentLogo}
                alt={`${settings.branding.appName} Logo`}
                width={120}
                height={120}
                // Eager-load: the logo is always above the fold. Lazy loading lets the
                // initial-render churn (auth/personalization/theme resolving) abort the
                // request with no retry, leaving the logo blank.
                priority
                className={cn(
                  "cursor-pointer transition-all duration-200 ease-out motion-reduce:transition-none object-contain",
                  sidebarOpen || !isCollapsed
                    ? "w-auto h-8 md:h-10 lg:h-12"
                    : "w-8 h-8 md:w-10 md:h-10"
                )}
                unoptimized={currentLogo.startsWith("data:")}
              />
            </Link>

            {sidebarOpen && (
              <Button
                variant="ghost"
                size="icon"
                aria-label="Close sidebar"
                onClick={() => setSidebarOpen(false)}
              >
                <X className="h-5 w-5" />
              </Button>
            )}
            {!sidebarOpen && (
              <Button
                variant="ghost"
                size="icon"
                aria-label={isCollapsed ? "Expand sidebar" : "Collapse sidebar"}
                aria-expanded={!isCollapsed}
                onClick={toggleSidebarCollapsed}
                className={cn(isCollapsed && "ml-1", "hidden md:flex")}
              >
                {isCollapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
              </Button>
            )}
          </div>

          {/* Navigation */}
          <ScrollArea className="flex-1">
            <nav aria-label="Primary">
              {/* key re-mounts on mode change so the incoming rail/tree fades in over the same
                  200ms the width animates, softening the collapse swap. */}
              <div
                key={showRail ? "rail" : "tree"}
                className="animate-in fade-in-0 duration-200 motion-reduce:animate-none"
              >
                {showRail ? (
                  <CollapsedRail groups={groups} ctx={ctx} />
                ) : (
                  <div className="px-3 py-3 space-y-1">
                    {groups.map((group) => (
                      <NavSection key={group.id} group={group} ctx={ctx} />
                    ))}
                  </div>
                )}
              </div>
            </nav>
          </ScrollArea>

          <SidebarFooter
            collapsed={showRail}
            settingsActive={activeHref === "/settings"}
            theme={theme}
            setTheme={setTheme}
            ctx={ctx}
          />
        </div>
      </aside>
    </>
  );
}
