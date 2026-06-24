"use client";

import { Search } from "lucide-react";
import { useLayout } from "@/components/layout-provider";

// Header entry point for global search. It does not search on its own - it opens
// the shared command palette (Cmd/Ctrl+K), so there is one search engine behind
// both surfaces. Styled like an input so it reads as a search field.
export function GlobalSearchBox() {
  const { setCommandPaletteOpen } = useLayout();

  return (
    <button
      type="button"
      onClick={() => setCommandPaletteOpen(true)}
      aria-label="Search"
      aria-keyshortcuts="Meta+K Control+K"
      className="group flex w-full items-center gap-2 rounded-md border bg-muted/40 px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted"
    >
      <Search className="h-4 w-4 shrink-0" />
      <span className="truncate">Search...</span>
      <kbd className="ml-auto hidden items-center gap-0.5 rounded border bg-background px-1.5 font-mono text-[10px] font-medium text-muted-foreground md:inline-flex">
        ⌘K
      </kbd>
    </button>
  );
}
