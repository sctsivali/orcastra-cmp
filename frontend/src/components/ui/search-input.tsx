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
   * Names the clear button. Set it wherever more than one of these renders on a screen: the
   * default is a fixed string, so several searches on one page announce identical buttons.
   * check-icon-button-names.mjs catches only buttons with no name.
   */
  clearLabel?: string;
  disabled?: boolean;
  className?: string;
}

/**
 * The one search control for every list view.
 *
 * Debouncing lives in useTableFilters. The control stays fully controlled and instant, which
 * keeps typing responsive.
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
        // Chrome paints its own clear button for type="search", which would sit next to the
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
