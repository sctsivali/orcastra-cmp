/**
 * Console Manager - Orchestrates multiple console windows.
 * Renders floating console windows and minimized tray.
 * 
 * This component should be placed in the main layout to enable
 * console windows across the entire application.
 */

"use client";

import * as React from "react";
import { useConsoleStore } from "@/lib/console-store";
import { ConsoleWindow } from "./console-window";
import { ConsoleTray } from "./console-tray";
import { cn } from "@/lib/utils";

interface ConsoleManagerProps {
  className?: string;
}

export function ConsoleManager({ className }: ConsoleManagerProps) {
  const {
    consoles,
    focusedConsoleId,
    closeConsole,
    minimizeConsole,
    restoreConsole,
    toggleFullPage,
    maximizeConsole,
    restoreFromMaximize,
    zIndexOrder,
    ensureWithinViewport,
  } = useConsoleStore();

  const [mounted, setMounted] = React.useState(false);

  // Client-side mount detection
  React.useEffect(() => {
    setMounted(true);
  }, []);

  // Handle viewport resize (orientation change, window resize)
  React.useEffect(() => {
    if (!mounted) return;

    // Debounce resize handler
    let resizeTimer: NodeJS.Timeout;
    const handleResize = () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        ensureWithinViewport();
      }, 150);
    };

    // Listen for resize and orientation change
    window.addEventListener('resize', handleResize);
    window.addEventListener('orientationchange', handleResize);
    
    // Also check on initial mount
    ensureWithinViewport();

    return () => {
      clearTimeout(resizeTimer);
      window.removeEventListener('resize', handleResize);
      window.removeEventListener('orientationchange', handleResize);
    };
  }, [mounted, ensureWithinViewport]);

  // Filter visible (non-minimized) consoles
  const visibleConsoles = consoles.filter((c) => !c.minimized);
  const minimizedConsoles = consoles.filter((c) => c.minimized);

  // Get z-index for each console
  const getZIndex = (consoleId: string) => {
    const index = zIndexOrder.indexOf(consoleId);
    return index >= 0 ? 1000 + index : 1000;
  };

  // Check if any console is in full page mode
  const fullPageConsole = consoles.find((c) => c.fullPage && !c.minimized);

  return (
    <>
      {/* Floating windows container */}
      <div
        className={cn(
          "fixed inset-0 pointer-events-none z-[999]",
          fullPageConsole && "pointer-events-auto",
          className
        )}
      >
        {/* Full page overlay */}
        {fullPageConsole && (
          <div className="absolute inset-0 bg-background z-[2000]">
            <ConsoleWindow
              consoleId={fullPageConsole.id}
              isFullPage={true}
              onClose={() => closeConsole(fullPageConsole.id)}
              onMinimize={() => {
                toggleFullPage(fullPageConsole.id);
                minimizeConsole(fullPageConsole.id);
              }}
              onMaximize={() => toggleFullPage(fullPageConsole.id)}
            />
          </div>
        )}

        {/* Floating windows (when not in full page mode) */}
        {!fullPageConsole && (
          <div className="absolute inset-0 overflow-hidden">
            {visibleConsoles.map((console) => (
              <div
                key={console.id}
                className="pointer-events-auto"
                style={{ zIndex: getZIndex(console.id) }}
              >
                <ConsoleWindow
                  consoleId={console.id}
                  isFullPage={false}
                  onClose={() => closeConsole(console.id)}
                  onMinimize={() => minimizeConsole(console.id)}
                  onMaximize={() => {
                    if (console.maximized) {
                      restoreFromMaximize(console.id);
                    } else {
                      toggleFullPage(console.id);
                    }
                  }}
                />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Minimized consoles tray */}
      {minimizedConsoles.length > 0 && !fullPageConsole && (
        <ConsoleTray
          consoles={minimizedConsoles}
          onRestore={(id) => restoreConsole(id)}
          onClose={(id) => closeConsole(id)}
        />
      )}
    </>
  );
}

export default ConsoleManager;
