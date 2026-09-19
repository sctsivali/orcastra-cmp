"use client";

import * as React from "react";
import {
  DOCK_COOKIE,
  DOCK_DEFAULT_STATE,
  clampDockHeight,
  serialiseDockCookie,
  type DockState,
  type DockTab,
} from "@/lib/dock-cookie";

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

// The task panel's open/height/tab state persists the same way and for the same reason: the
// server reads the cookie so the strip is open at the right height before hydration.
function persistDock(next: DockState): void {
  try {
    document.cookie = `${DOCK_COOKIE}=${serialiseDockCookie(next)}; path=/; max-age=${ONE_YEAR_SECONDS}; SameSite=Lax`;
  } catch {
    // Same as the sidebar: a blocked cookie only loses the first-paint state.
  }
}

interface LayoutContextType {
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleSidebarCollapsed: () => void;
  sidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;
  dockOpen: boolean;
  dockHeight: number;
  dockTab: DockTab;
  setDockOpen: (open: boolean) => void;
  toggleDock: () => void;
  setDockHeight: (px: number) => void;
  setDockTab: (tab: DockTab) => void;
}

const LayoutContext = React.createContext<LayoutContextType>({
  sidebarCollapsed: false,
  setSidebarCollapsed: () => {},
  toggleSidebarCollapsed: () => {},
  sidebarOpen: false,
  setSidebarOpen: () => {},
  dockOpen: DOCK_DEFAULT_STATE.open,
  dockHeight: DOCK_DEFAULT_STATE.height,
  dockTab: DOCK_DEFAULT_STATE.tab,
  setDockOpen: () => {},
  toggleDock: () => {},
  setDockHeight: () => {},
  setDockTab: () => {},
});

export function LayoutProvider({
  children,
  initialCollapsed = false,
  initialDock = DOCK_DEFAULT_STATE,
}: {
  children: React.ReactNode;
  initialCollapsed?: boolean;
  initialDock?: DockState;
}) {
  const [sidebarCollapsed, setCollapsedState] = React.useState(initialCollapsed);
  const [sidebarOpen, setSidebarOpen] = React.useState(false);
  const [dock, setDock] = React.useState<DockState>(initialDock);

  // One updater shape for every dock write: compute the next state, persist it, return it.
  const updateDock = React.useCallback((patch: (prev: DockState) => DockState) => {
    setDock((prev) => {
      const next = patch(prev);
      if (
        next.open === prev.open &&
        next.height === prev.height &&
        next.tab === prev.tab
      ) {
        return prev;
      }
      persistDock(next);
      return next;
    });
  }, []);

  const setDockOpen = React.useCallback(
    (open: boolean) => updateDock((prev) => ({ ...prev, open })),
    [updateDock],
  );
  const toggleDock = React.useCallback(
    () => updateDock((prev) => ({ ...prev, open: !prev.open })),
    [updateDock],
  );
  const setDockHeight = React.useCallback(
    (px: number) => updateDock((prev) => ({ ...prev, height: clampDockHeight(px) })),
    [updateDock],
  );
  const setDockTab = React.useCallback(
    (tab: DockTab) => updateDock((prev) => ({ ...prev, tab })),
    [updateDock],
  );

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
      dockOpen: dock.open,
      dockHeight: dock.height,
      dockTab: dock.tab,
      setDockOpen,
      toggleDock,
      setDockHeight,
      setDockTab,
    }),
    [
      sidebarCollapsed,
      setSidebarCollapsed,
      toggleSidebarCollapsed,
      sidebarOpen,
      dock,
      setDockOpen,
      toggleDock,
      setDockHeight,
      setDockTab,
    ]
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
