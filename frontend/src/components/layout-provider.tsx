"use client";

import * as React from "react";

interface LayoutContextType {
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
}

const LayoutContext = React.createContext<LayoutContextType>({
  sidebarCollapsed: true,
  setSidebarCollapsed: () => {},
});

export function LayoutProvider({ children }: { children: React.ReactNode }) {
  const [sidebarCollapsed, setSidebarCollapsed] = React.useState(true);

  React.useEffect(() => {
    const saved = localStorage.getItem("sidebar-collapsed");
    if (saved !== null) {
      setSidebarCollapsed(saved === "true");
    }
  }, []);

  return (
    <LayoutContext.Provider value={{ sidebarCollapsed, setSidebarCollapsed }}>
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
