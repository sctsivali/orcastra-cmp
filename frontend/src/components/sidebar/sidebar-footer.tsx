"use client";
import * as React from "react";
import Link from "next/link";
import {
  Settings,
  Sun,
  Moon,
  Monitor,
  Bug,
  MessagesSquare,
  ExternalLink,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { NavItemSpinner, NavRailIcon } from "./nav-link-status";
import { linkIntentProps } from "@/components/navigation/route-intent";
import { useLayout } from "../layout-provider";
import { COMMUNITY_URL, buildBugReportUrl, describeEnvironment } from "./support-links";
import type { NavRenderContext } from "./types";
import { usePermissions } from "@/hooks/use-permissions";
type Theme = "light" | "dark" | "system";
interface SidebarFooterProps {
  collapsed: boolean;
  settingsActive: boolean;
  theme: Theme;
  setTheme: (theme: Theme) => void;
  ctx: NavRenderContext;
}
interface SupportLink {
  id: string;
  label: string;
  href: string;
  icon: LucideIcon;
}
// Both destinations leave the app, so they say so to a screen reader as well as with the
// glyph. WCAG technique G201: warn before opening a new window.
const NEW_TAB_HINT = "opens in a new tab";
function ThemeIcon({ theme }: { theme: Theme }) {
  if (theme === "light") return <Sun className="h-5 w-5" />;
  if (theme === "dark") return <Moon className="h-5 w-5" />;
  return <Monitor className="h-5 w-5" />;
}
// note: no NavItemSpinner, no NavRailIcon and no linkIntentProps on either of these. The first
// two read useLinkStatus, which only has context inside a next/link, and the third would ask
// the router to prefetch an absolute URL.
function SupportRailLink({ link, onOpen }: { link: SupportLink; onOpen: () => void }) {
  const Icon = link.icon;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button asChild variant="ghost" size="icon">
          <a
            href={link.href}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`${link.label}, ${NEW_TAB_HINT}`}
            onClick={onOpen}
          >
            <Icon className="h-5 w-5" />
          </a>
        </Button>
      </TooltipTrigger>
      <TooltipContent side="right">{link.label}</TooltipContent>
    </Tooltip>
  );
}
function SupportRow({ link, onOpen }: { link: SupportLink; onOpen: () => void }) {
  const Icon = link.icon;
  return (
    <Button asChild variant="ghost" className="w-full justify-start">
      <a
        href={link.href}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={`${link.label}, ${NEW_TAB_HINT}`}
        onClick={onOpen}
      >
        <Icon className="h-5 w-5" />
        <span className="ml-3">{link.label}</span>
        <ExternalLink className="ml-auto h-3.5 w-3.5 shrink-0 opacity-60" aria-hidden="true" />
      </a>
    </Button>
  );
}
export function SidebarFooter({
  collapsed,
  settingsActive,
  theme,
  setTheme,
  ctx,
}: SidebarFooterProps) {
  const { isPartner } = usePermissions();
  const { setSidebarOpen } = useLayout();

  // The href is a real one from the first byte, so middle-click and "copy link address" both
  // work. Template and version are build-time constants and are already in it; the browser's
  // user agent exists only on the client, so it is folded in after mount. The first client
  // render matches the server output, which is what keeps this out of hydration.
  const [bugReportHref, setBugReportHref] = React.useState(() => buildBugReportUrl());
  React.useEffect(() => {
    setBugReportHref(buildBugReportUrl(describeEnvironment()));
  }, []);

  // important: an external link never changes the pathname, so the effect in sidebar.tsx that
  // closes the mobile drawer on route commit never fires for these two. Without this the
  // drawer stays open behind the new tab. On desktop sidebarOpen is already false and React
  // bails out of the update.
  const closeDrawer = React.useCallback(() => setSidebarOpen(false), [setSidebarOpen]);

  const supportLinks: SupportLink[] = React.useMemo(
    () => [
      { id: "bug-report", label: "Bug Report", href: bugReportHref, icon: Bug },
      { id: "discussion", label: "Discussion", href: COMMUNITY_URL, icon: MessagesSquare },
    ],
    [bugReportHref]
  );

  const themeMenu = (
    <DropdownMenuContent align="start" side="top" className="w-48">
      <DropdownMenuItem onClick={() => setTheme("light")}>
        <Sun className="mr-2 h-4 w-4" />
        Light Mode
        {theme === "light" && <span className="ml-auto">✓</span>}
      </DropdownMenuItem>
      <DropdownMenuItem onClick={() => setTheme("dark")}>
        <Moon className="mr-2 h-4 w-4" />
        Dark Mode
        {theme === "dark" && <span className="ml-auto">✓</span>}
      </DropdownMenuItem>
      <DropdownMenuItem onClick={() => setTheme("system")}>
        <Monitor className="mr-2 h-4 w-4" />
        System
        {theme === "system" && <span className="ml-auto">✓</span>}
      </DropdownMenuItem>
    </DropdownMenuContent>
  );
  if (collapsed) {
    return (
      <div className="shrink-0">
        <div className="border-t p-3 flex flex-col items-center gap-2">
          {isPartner && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                asChild
                variant={settingsActive ? "secondary" : "ghost"}
                size="icon"
                className={cn(settingsActive && "sidebar-active")}
              >
                <Link
                  href="/settings"
                  prefetch={false}
                  aria-label="Settings"
                  aria-current={settingsActive ? "page" : undefined}
                  {...linkIntentProps(ctx.intent, "/settings")}
                >
                  <NavRailIcon icon={Settings} />
                </Link>
              </Button>
            </TooltipTrigger>
            <TooltipContent side="right">Settings</TooltipContent>
          </Tooltip>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon" aria-label="Change theme">
                <ThemeIcon theme={theme} />
              </Button>
            </DropdownMenuTrigger>
            {themeMenu}
          </DropdownMenu>
        </div>
        <div className="border-t px-3 py-2 flex flex-col items-center gap-2">
          {supportLinks.map((link) => (
            <SupportRailLink key={link.id} link={link} onOpen={closeDrawer} />
          ))}
        </div>
      </div>
    );
  }
  return (
    <div className="shrink-0">
      <div className="border-t p-3 space-y-2">
        {isPartner && (
        <Button
          asChild
          variant={settingsActive ? "default" : "ghost"}
          className={cn("w-full justify-start", settingsActive && "sidebar-active")}
        >
          <Link
            href="/settings"
            prefetch={false}
            aria-current={settingsActive ? "page" : undefined}
            {...linkIntentProps(ctx.intent, "/settings")}
          >
            <Settings className="h-5 w-5" />
            <span className="ml-3">Settings</span>
            <NavItemSpinner />
          </Link>
        </Button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" className="w-full justify-start">
              <ThemeIcon theme={theme} />
              <span className="ml-3">Theme</span>
            </Button>
          </DropdownMenuTrigger>
          {themeMenu}
        </DropdownMenu>
      </div>
      <div className="border-t px-3 py-2 space-y-1">
        {supportLinks.map((link) => (
          <SupportRow key={link.id} link={link} onOpen={closeDrawer} />
        ))}
      </div>
    </div>
  );
}
