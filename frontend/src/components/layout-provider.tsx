"use client";

import * as React from "react";

interface LayoutContextType {
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleSidebarCollapsed: () => void;
  sidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;
  commandPaletteOpen: boolean;
  setCommandPaletteOpen: (open: boolean) => void;
}

const LayoutContext = React.createContext<LayoutContextType>({
  sidebarCollapsed: false,
  setSidebarCollapsed: () => {},
  toggleSidebarCollapsed: () => {},
  sidebarOpen: false,
  setSidebarOpen: () => {},
  commandPaletteOpen: false,
  setCommandPaletteOpen: () => {},
});

// Cookie hint for the collapsed state. Read on the server (layout.tsx) so the first
// paint already has the correct sidebar width and content margin - no FOUC/layout shift.
const SIDEBAR_COOKIE = "orcastra-sidebar-collapsed";

function writeSidebarCookie(collapsed: boolean) {
  if (typeof document === "undefined") return;
  try {
    document.cookie = `${SIDEBAR_COOKIE}=${collapsed};path=/;max-age=${365 * 24 * 60 * 60};SameSite=Lax`;
  } catch {
    // degrade gracefully if cookies are unavailable
  }
}

export function LayoutProvider({
  children,
  initialCollapsed = false,
}: {
  children: React.ReactNode;
  initialCollapsed?: boolean;
}) {
  const [sidebarCollapsed, setCollapsedState] = React.useState(initialCollapsed);
  const [sidebarOpen, setSidebarOpen] = React.useState(false);
  const [commandPaletteOpen, setCommandPaletteOpen] = React.useState(false);

  const setSidebarCollapsed = React.useCallback((collapsed: boolean) => {
    setCollapsedState(collapsed);
    writeSidebarCookie(collapsed);
  }, []);

  const toggleSidebarCollapsed = React.useCallback(() => {
    setCollapsedState((prev) => {
      const next = !prev;
      writeSidebarCookie(next);
      return next;
    });
  }, []);

  return (
    <LayoutContext.Provider
      value={{
        sidebarCollapsed,
        setSidebarCollapsed,
        toggleSidebarCollapsed,
        sidebarOpen,
        setSidebarOpen,
        commandPaletteOpen,
        setCommandPaletteOpen,
      }}
    >
      {children}
    </LayoutContext.Provider>
  );
}

export const useLayout = () => {
  const context = React.useContext(LayoutContext);
  if (!context) {
    throw new Error("useLayout must be used within LayoutProvider");
  }
  return context;
};
