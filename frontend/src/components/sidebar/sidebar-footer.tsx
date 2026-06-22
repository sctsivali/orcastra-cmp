"use client";

import Link from "next/link";
import { Settings, Sun, Moon, Monitor } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

type Theme = "light" | "dark" | "system";

interface SidebarFooterProps {
  collapsed: boolean;
  settingsActive: boolean;
  theme: Theme;
  setTheme: (theme: Theme) => void;
  onLinkClick: () => void;
  prefetch: (key: string) => void;
}

function ThemeIcon({ theme }: { theme: Theme }) {
  if (theme === "light") return <Sun className="h-5 w-5" />;
  if (theme === "dark") return <Moon className="h-5 w-5" />;
  return <Monitor className="h-5 w-5" />;
}

export function SidebarFooter({
  collapsed,
  settingsActive,
  theme,
  setTheme,
  onLinkClick,
}: SidebarFooterProps) {
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
      <div className="border-t p-3 flex flex-col items-center gap-2 shrink-0">
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
                aria-label="Settings"
                aria-current={settingsActive ? "page" : undefined}
                onClick={onLinkClick}
              >
                <Settings className="h-5 w-5" />
              </Link>
            </Button>
          </TooltipTrigger>
          <TooltipContent side="right">Settings</TooltipContent>
        </Tooltip>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" aria-label="Change theme">
              <ThemeIcon theme={theme} />
            </Button>
          </DropdownMenuTrigger>
          {themeMenu}
        </DropdownMenu>
      </div>
    );
  }

  return (
    <div className="border-t p-3 space-y-2 shrink-0">
      <Button
        asChild
        variant={settingsActive ? "default" : "ghost"}
        className={cn("w-full justify-start", settingsActive && "sidebar-active")}
      >
        <Link
          href="/settings"
          onClick={onLinkClick}
          aria-current={settingsActive ? "page" : undefined}
        >
          <Settings className="h-5 w-5" />
          <span className="ml-3">Settings</span>
        </Link>
      </Button>

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
  );
}
