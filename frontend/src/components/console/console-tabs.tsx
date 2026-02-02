/**
 * Console Tabs - Tab navigation for console window.
 * Switches between text console, graphics console, and ISO management.
 * Note: Boot Order tab is hidden/commented out.
 */

"use client";

import * as React from "react";
import {
  Terminal,
  Monitor,
  // ListOrdered, // Uncomment if re-enabling Boot Order tab
  Disc,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { ConsoleTab, ConsoleCapabilities } from "@/lib/console-store";

interface ConsoleTabsProps {
  activeTab: ConsoleTab;
  onTabChange: (tab: ConsoleTab) => void;
  instanceType: string;
  capabilities: ConsoleCapabilities | null;
  className?: string;
}

interface TabConfig {
  id: ConsoleTab;
  label: string;
  icon: React.ReactNode;
  available: boolean;
  tooltip?: string;
}

export function ConsoleTabs({
  activeTab,
  onTabChange,
  instanceType,
  capabilities,
  className,
}: ConsoleTabsProps) {
  const isVM = instanceType === 'virtual-machine';
  
  const tabs: TabConfig[] = [
    {
      id: 'text',
      label: 'Text',
      icon: <Terminal className="h-4 w-4" />,
      available: true,
      tooltip: 'Text/Shell Console',
    },
    {
      id: 'graphics',
      label: 'Graphics',
      icon: <Monitor className="h-4 w-4" />,
      available: isVM && (capabilities?.graphicsConsole ?? isVM),
      tooltip: isVM ? 'VGA Graphics Console' : 'Graphics console only available for VMs',
    },
    // Boot Order tab is hidden (commented out) as per user request
    // Uncomment below to re-enable the Boot Order tab
    // {
    //   id: 'boot',
    //   label: 'Boot Order',
    //   icon: <ListOrdered className="h-4 w-4" />,
    //   available: true,
    //   tooltip: 'Startup Order Configuration',
    // },
    {
      id: 'iso',
      label: 'ISO',
      icon: <Disc className="h-4 w-4" />,
      available: isVM,
      tooltip: isVM ? 'ISO/Image Management' : 'ISO management only available for VMs',
    },
  ];

  return (
    <div 
      className={cn("flex border-b bg-muted/20 touch-manipulation", className)}
      onMouseDown={(e) => e.stopPropagation()}
      onTouchStart={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
    >
      {tabs.map((tab) => (
        <button
          key={tab.id}
          onClick={() => tab.available && onTabChange(tab.id)}
          onTouchEnd={(e) => { e.preventDefault(); if (tab.available) onTabChange(tab.id); }}
          disabled={!tab.available}
          className={cn(
            "flex items-center gap-1.5 px-3 py-2 min-h-[44px] text-sm font-medium transition-colors relative",
            "border-b-2 -mb-px active:scale-95",
            activeTab === tab.id
              ? "text-primary border-primary bg-background"
              : "text-muted-foreground border-transparent hover:text-foreground hover:bg-muted/50",
            !tab.available && "opacity-50 cursor-not-allowed"
          )}
          title={tab.tooltip}
        >
          {tab.icon}
          <span className="hidden sm:inline">{tab.label}</span>
        </button>
      ))}
    </div>
  );
}
