"use client";

import * as React from "react";
import { Search, X } from "lucide-react";

import { cn } from "@/lib/utils";
import { Input } from "@/components/ui/input";

export interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  /** Falls back to the placeholder. Use for a fuller sentence than the visible hint. */
  label?: string;
  /**
   * Names the clear button. Set it wherever more than one of these renders on a screen.
   *
   * important: the default is a fixed string, so N searches on one page give a screen reader N
   * buttons that announce identically. check-icon-button-names.mjs counts buttons with no name,
   * not buttons whose name is ambiguous, so nothing catches that for you.
   */
  clearLabel?: string;
  disabled?: boolean;
  className?: string;
}

/**
 * The one search control for every list view.
 *
 * note: this replaces four hand-rolled shapes that had drifted apart on icon offset, input
 * padding, width and whether a clear button existed at all. One of them bypassed the house
 * Input entirely and used a raw element.
 *
 * Debouncing deliberately lives in useTableFilters, not here. The control stays fully
 * controlled and instant, which is what keeps typing responsive.
 */
export function SearchInput({
  value,
  onChange,
  placeholder,
  label,
  clearLabel = "Clear search",
  disabled = false,
  className,
}: SearchInputProps) {
  const inputRef = React.useRef<HTMLInputElement>(null);

  return (
    <div className={cn("relative", className)}>
      <Search
        aria-hidden="true"
        className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
      />
      <Input
        ref={inputRef}
        type="search"
        value={value}
        disabled={disabled}
        aria-label={label ?? placeholder}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        // important: Chrome paints its own clear button for type="search", so without this reset
        // every one of these renders TWO crosses side by side, the native one and the real
        // button below. Scoped here rather than in globals.css because this component is the
        // only place the app uses type="search".
        className="pl-9 pr-9 [&::-webkit-search-cancel-button]:appearance-none"
      />
      {value && (
        <button
          type="button"
          aria-label={clearLabel}
          onClick={() => {
            onChange("");
            // Returning focus keeps a keyboard user in the field they were typing in.
            inputRef.current?.focus();
          }}
          className="absolute right-2 top-1/2 -translate-y-1/2 rounded-sm p-1 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}
