"use client";

import * as React from "react";

// Collapse state lives in a cookie rather than localStorage so app/layout.tsx can read it
// on the server and the very first paint already has the right sidebar width and content
// margin. localStorage is unreadable during SSR, which is why the previous implementation
// always rendered collapsed and then jumped once hydration ran the effect.
const SIDEBAR_COOKIE = "orcastra-sidebar-collapsed";
const LEGACY_STORAGE_KEY = "sidebar-collapsed";
const ONE_YEAR_SECONDS = 60 * 60 * 24 * 365;

function writeSidebarCookie(collapsed: boolean): void {
  try {
    document.cookie = `${SIDEBAR_COOKIE}=${collapsed}; path=/; max-age=${ONE_YEAR_SECONDS}; SameSite=Lax`;
  } catch {
    // A blocked cookie only costs the first-paint optimisation, never the interaction.
  }
}

interface LayoutContextType {
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleSidebarCollapsed: () => void;
  sidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;
}

const LayoutContext = React.createContext<LayoutContextType>({
  sidebarCollapsed: false,
  setSidebarCollapsed: () => {},
  toggleSidebarCollapsed: () => {},
  sidebarOpen: false,
  setSidebarOpen: () => {},
});

export function LayoutProvider({
  children,
  initialCollapsed = false,
}: {
  children: React.ReactNode;
  initialCollapsed?: boolean;
}) {
  const [sidebarCollapsed, setCollapsedState] = React.useState(initialCollapsed);
  const [sidebarOpen, setSidebarOpen] = React.useState(false);

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

  // One-shot migration from the localStorage key this used to persist to. A user who had
  // explicitly collapsed or expanded keeps that choice; a user who never touched the toggle
  // falls through to the new expanded default. Costs one post-hydration flip, once, and
  // only for profiles that predate the cookie.
  React.useEffect(() => {
    if (document.cookie.includes(`${SIDEBAR_COOKIE}=`)) return;
    let legacy: string | null = null;
    try {
      legacy = localStorage.getItem(LEGACY_STORAGE_KEY);
    } catch {
      return;
    }
    if (legacy === null) return;
    const collapsed = legacy === "true";
    writeSidebarCookie(collapsed);
    setCollapsedState(collapsed);
  }, []);

  const value = React.useMemo(
    () => ({
      sidebarCollapsed,
      setSidebarCollapsed,
      toggleSidebarCollapsed,
      sidebarOpen,
      setSidebarOpen,
    }),
    [sidebarCollapsed, setSidebarCollapsed, toggleSidebarCollapsed, sidebarOpen]
  );

  return <LayoutContext.Provider value={value}>{children}</LayoutContext.Provider>;
}

export const useLayout = () => {
  const context = React.useContext(LayoutContext);
  if (!context) {
    throw new Error("useLayout must be used within LayoutProvider");
  }
  return context;
};
