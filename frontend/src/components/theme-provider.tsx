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
  resolvedTheme: "dark" | "light"; // the applied theme, "system" resolved
};

const initialState: ThemeProviderState = {
  theme: "system",
  setTheme: () => null,
  resolvedTheme: typeof window !== "undefined"
    ? window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"
    : "dark",
};

const ThemeProviderContext = React.createContext<ThemeProviderState>(initialState);

function getSystemTheme(): "dark" | "light" {
  if (typeof window === "undefined") return "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// A hint the root layout reads before first paint, so the wrong theme never flashes.
function setThemeCookie(theme: Theme) {
  if (typeof document === "undefined") return;
  document.cookie = `orcastra-theme=${theme};path=/;max-age=${365 * 24 * 60 * 60};SameSite=Lax`;
}

/**
 * Applies the theme preference from PersonalizationProvider, stored in the database.
 *
 * The `orcastra-theme` cookie only lets the first paint use the right theme; the
 * database stays the source of truth.
 */
export function ThemeProvider({ children }: ThemeProviderProps) {
  const { settings, updateTheme, isLoading } = usePersonalization();
  const [resolvedTheme, setResolvedTheme] = React.useState<"dark" | "light">(() => getSystemTheme());

  const theme: Theme = settings.theme || "system";

  // Apply the theme to <html>; while it is "system", follow the OS preference.
  React.useEffect(() => {
    const root = window.document.documentElement;
    
    const applyTheme = (newTheme: "dark" | "light") => {
      root.classList.remove("light", "dark");
      root.classList.add(newTheme);
      setResolvedTheme(newTheme);
    };

    // Keep the cookie hint in step for the next page load.
    setThemeCookie(theme);

    if (theme === "system") {
      applyTheme(getSystemTheme());
      
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

  // Drop the stale `orcastra-ui-theme` localStorage key; the database holds the theme.
  React.useEffect(() => {
    try {
      localStorage.removeItem("orcastra-ui-theme");
    } catch {
      // Storage unavailable: nothing to remove.
    }
  }, []);

  const setTheme = React.useCallback((newTheme: Theme) => {
    // Set the cookie at once so the next page load already uses the new theme.
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
