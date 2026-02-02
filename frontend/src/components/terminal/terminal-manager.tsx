/**
 * Terminal Manager - Global container for terminal windows.
 * Manages all terminal windows and the tray.
 */

"use client";

import * as React from "react";
import { useTerminalStore } from "@/lib/terminal-store";
import { TerminalWindow } from "./terminal-window";
import { TerminalTray } from "./terminal-tray";

export function TerminalManager() {
  // Subscribe to terminals array
  const terminals = useTerminalStore((s) => s.terminals);
  const ensureWithinViewport = useTerminalStore((s) => s.ensureWithinViewport);
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

  // SSR safety - don't render terminals until client-side
  if (!mounted) {
    return <TerminalTray />;
  }

  // Render terminals directly in a fixed container (no portal)
  // This avoids portal timing issues with zustand state updates
  return (
    <>
      {/* Fixed container for all terminals */}
      <div 
        id="terminal-layer"
        style={{
          position: 'fixed',
          top: 0,
          left: 0,
          width: 0,
          height: 0,
          overflow: 'visible',
          zIndex: 99999,
          pointerEvents: 'none',
        }}
      >
        {terminals.map((terminal) => (
          <div key={terminal.id} style={{ pointerEvents: 'auto' }}>
            <TerminalWindow terminal={terminal} />
          </div>
        ))}
      </div>

      {/* Tray stays in normal layout */}
      <TerminalTray />
    </>
  );
}

export default TerminalManager;
