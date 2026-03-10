"use client";

import * as React from "react";
import { usePersonalization } from "./personalization-provider";
import type { ThemePreference } from "@/types/personalization";

type Theme = "dark" | "light" | "system";

type ThemeProviderProps = {
  children: React.ReactNode;
};

type ThemeProviderState = {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  resolvedTheme: "dark" | "light"; // actual applied theme
};

const initialState: ThemeProviderState = {
  theme: "system",
  setTheme: () => null,
  resolvedTheme: typeof window !== "undefined"
    ? window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"
    : "dark",
};

const ThemeProviderContext = React.createContext<ThemeProviderState>(initialState);

// helper to get system theme
function getSystemTheme(): "dark" | "light" {
  if (typeof window === "undefined") return "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// persist theme hint in cookie for SSR FOUC prevention (365 days, SameSite=Lax)
function setThemeCookie(theme: Theme) {
  if (typeof document === "undefined") return;
  document.cookie = `orcastra-theme=${theme};path=/;max-age=${365 * 24 * 60 * 60};SameSite=Lax`;
}

/**
 * ThemeProvider - Reads theme preference from PersonalizationProvider (DB-backed).
 * 
 * Uses a cookie hint (`orcastra-theme`) for instant theme application on page load
 * to prevent FOUC (Flash of Unstyled Content). The cookie is NOT the source of truth —
 * PostgreSQL via PersonalizationProvider remains the single source of truth.
 * The cookie is merely a performance optimization for initial render.
 */
export function ThemeProvider({ children }: ThemeProviderProps) {
  const { settings, updateTheme, isLoading } = usePersonalization();
  const [resolvedTheme, setResolvedTheme] = React.useState<"dark" | "light">(() => getSystemTheme());

  // Derive theme from personalization settings
  const theme: Theme = settings.theme || "system";

  // Apply theme to DOM and listen for system changes
  React.useEffect(() => {
    const root = window.document.documentElement;
    
    const applyTheme = (newTheme: "dark" | "light") => {
      root.classList.remove("light", "dark");
      root.classList.add(newTheme);
      setResolvedTheme(newTheme);
    };

    // sync cookie hint for next page load
    setThemeCookie(theme);

    if (theme === "system") {
      // Apply current system preference
      applyTheme(getSystemTheme());
      
      // Listen for system preference changes
      const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
      const handleChange = (e: MediaQueryListEvent) => {
        applyTheme(e.matches ? "dark" : "light");
      };
      
      mediaQuery.addEventListener("change", handleChange);
      return () => mediaQuery.removeEventListener("change", handleChange);
    } else {
      applyTheme(theme);
    }
  }, [theme]);

  // Migrate: clean up old localStorage key if it exists
  React.useEffect(() => {
    try {
      localStorage.removeItem("orcastra-ui-theme");
    } catch {
      // ignore
    }
  }, []);

  const setTheme = React.useCallback((newTheme: Theme) => {
    // immediately update cookie so next load is instant
    setThemeCookie(newTheme);
    updateTheme(newTheme as ThemePreference);
  }, [updateTheme]);

  const value = React.useMemo(() => ({
    theme,
    setTheme,
    resolvedTheme,
  }), [theme, setTheme, resolvedTheme]);

  return (
    <ThemeProviderContext.Provider value={value}>
      {children}
    </ThemeProviderContext.Provider>
  );
}

export const useTheme = () => {
  const context = React.useContext(ThemeProviderContext);

  if (context === undefined)
    throw new Error("useTheme must be used within a ThemeProvider");

  return context;
};
