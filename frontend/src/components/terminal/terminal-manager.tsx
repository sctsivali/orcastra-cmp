/** Global container for the floating terminal windows and the tray. */

"use client";

import * as React from "react";
import { useTerminalStore } from "@/lib/terminal-store";
import { TerminalWindow } from "./terminal-window";
import { TerminalTray } from "./terminal-tray";

export function TerminalManager() {
  const terminals = useTerminalStore((s) => s.terminals);
  const ensureWithinViewport = useTerminalStore((s) => s.ensureWithinViewport);
  const [mounted, setMounted] = React.useState(false);
  
  React.useEffect(() => {
    setMounted(true);
  }, []);

  // Keep every window inside the viewport after a resize or an orientation change.
  React.useEffect(() => {
    if (!mounted) return;

    let resizeTimer: NodeJS.Timeout;
    const handleResize = () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        ensureWithinViewport();
      }, 150);
    };

    window.addEventListener('resize', handleResize);
    window.addEventListener('orientationchange', handleResize);
    
    ensureWithinViewport();

    return () => {
      clearTimeout(resizeTimer);
      window.removeEventListener('resize', handleResize);
      window.removeEventListener('orientationchange', handleResize);
    };
  }, [mounted, ensureWithinViewport]);

  // Windows render only after mount, so the server render holds the tray alone.
  if (!mounted) {
    return <TerminalTray />;
  }

  // A fixed container instead of a portal, which avoids portal timing issues with zustand
  // state updates.
  return (
    <>
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
