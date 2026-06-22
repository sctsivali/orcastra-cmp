"use client";

import * as React from "react";
import { useLayout } from "../layout-provider";

function isTypingTarget(el: EventTarget | null): boolean {
  const node = el as HTMLElement | null;
  if (!node) return false;
  const tag = node.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    node.isContentEditable
  );
}

// Global keyboard shortcuts: Cmd/Ctrl+B toggles the sidebar, Cmd/Ctrl+K opens the
// command palette. Registered once with a single listener that is cleaned up on unmount.
export function useSidebarShortcuts() {
  const { toggleSidebarCollapsed, setCommandPaletteOpen } = useLayout();

  React.useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
      const key = e.key.toLowerCase();
      if (key === "k") {
        e.preventDefault();
        setCommandPaletteOpen(true);
      } else if (key === "b") {
        // do not hijack while typing (some editors map Cmd+B to bold)
        if (isTypingTarget(e.target)) return;
        e.preventDefault();
        toggleSidebarCollapsed();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [toggleSidebarCollapsed, setCommandPaletteOpen]);
}
