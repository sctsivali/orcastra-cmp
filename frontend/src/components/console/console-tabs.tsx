/**
 * Console Tabs: Graphics (a VM's display), Text (a shell, needs the VM agent), Serial (the serial
 * console, needs neither the agent nor networking), ISO (a VM's boot media), Files (the guest's
 * filesystem, needs the VM agent) and Credentials (the guest's logins, needs the VM agent).
 */

"use client";

import * as React from "react";
import { Disc, FolderOpen, KeyRound, Monitor, Terminal, TerminalSquare } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ConsoleCapabilities, ConsoleTab } from "@/lib/console-store";

interface ConsoleTabsProps {
  activeTab: ConsoleTab;
  onTabChange: (tab: ConsoleTab) => void;
  instanceType: string;
  capabilities: ConsoleCapabilities | null;
  /** Why files cannot be used here, or undefined when they can. */
  filesUnavailableReason?: string;
  /** Why the Credentials panel cannot be used here, or undefined when it can. */
  credentialsUnavailableReason?: string;
  className?: string;
}

interface TabConfig {
  id: ConsoleTab;
  label: string;
  icon: React.ReactNode;
  available: boolean;
  description: string;
}

export function ConsoleTabs({
  activeTab, onTabChange, instanceType, capabilities, filesUnavailableReason, credentialsUnavailableReason, className,
}: ConsoleTabsProps) {
  const isVM = instanceType === "virtual-machine";
  const tabs: TabConfig[] = [
    {
      id: "graphics",
      label: "Graphics",
      icon: <Monitor className="h-4 w-4" aria-hidden="true" />,
      available: isVM && (capabilities?.graphicsConsole ?? isVM),
      description: isVM ? "The VM's display" : "Only virtual machines have a display",
    },
    {
      id: "text",
      label: "Text",
      icon: <Terminal className="h-4 w-4" aria-hidden="true" />,
      available: true,
      description: isVM ? "A shell on the VM (needs its agent)" : "A shell in the container",
    },
    {
      id: "serial",
      label: "Serial",
      icon: <TerminalSquare className="h-4 w-4" aria-hidden="true" />,
      available: true,
      description: "The serial console: boot output and a login prompt, no agent needed",
    },
    {
      id: "iso",
      label: "ISO",
      icon: <Disc className="h-4 w-4" aria-hidden="true" />,
      available: isVM,
      description: isVM ? "Boot media" : "Only virtual machines take boot media",
    },
    {
      id: "files",
      label: "Files",
      icon: <FolderOpen className="h-4 w-4" aria-hidden="true" />,
      available: !filesUnavailableReason,
      description: filesUnavailableReason ?? (isVM ? "Files in the VM (needs its agent)" : "Files in the container"),
    },
    {
      id: "credentials",
      label: "Credentials",
      icon: <KeyRound className="h-4 w-4" aria-hidden="true" />,
      available: !credentialsUnavailableReason,
      description: credentialsUnavailableReason ?? "Logins of the guest: users, passwords and SSH keys",
    },
  ];
  // A container has no display and no boot media: those tabs are left out rather than greyed.
  const visible = tabs.filter((tab) => tab.available || isVM);

  return (
    <div
      role="tablist"
      aria-label="Console views"
      className={cn("flex border-b bg-muted/20 touch-manipulation", className)}
      onMouseDown={(e) => e.stopPropagation()}
      onTouchStart={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        const enabled = visible.filter((t) => t.available);
        const index = enabled.findIndex((t) => t.id === activeTab);
        const next = enabled[(index + (e.key === "ArrowRight" ? 1 : enabled.length - 1)) % enabled.length];
        if (next) {
          e.preventDefault();
          onTabChange(next.id);
        }
      }}
    >
      {visible.map((tab) => {
        const selected = activeTab === tab.id;
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-label={tab.label}
            tabIndex={selected ? 0 : -1}
            onClick={() => tab.available && onTabChange(tab.id)}
            disabled={!tab.available}
            title={tab.description}
            className={cn(
              "relative -mb-px flex min-h-[40px] items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
              selected
                ? "border-primary bg-background text-primary"
                : "border-transparent text-muted-foreground hover:bg-muted/50 hover:text-foreground",
              !tab.available && "cursor-not-allowed opacity-50",
            )}
          >
            {tab.icon}
            <span className="hidden sm:inline">{tab.label}</span>
          </button>
        );
      })}
    </div>
  );
}
