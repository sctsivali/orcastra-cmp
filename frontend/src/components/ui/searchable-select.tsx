"use client";

import * as React from "react";
import { Check, ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";

export interface SearchableSelectOption {
  value: string;
  label: string;
  // secondary line under the label (ex; cluster endpoint), also matched while searching
  sublabel?: string;
  // trailing hint shown on the right (ex; hypervisor type), also matched while searching
  badge?: string;
  disabled?: boolean;
}

interface SearchableSelectProps {
  value: string;
  onValueChange: (value: string) => void;
  options: SearchableSelectOption[];
  placeholder?: string;
  searchPlaceholder?: string;
  emptyText?: string;
  disabled?: boolean;
  // note: the search row only renders once the option count passes this, so short lists stay uncluttered
  searchThreshold?: number;
  // forwarded to the trigger so callers can keep their existing styling (ex; error border)
  className?: string;
  contentClassName?: string;
}

// Single-select combobox with optional in-list search. Drop-in replacement for a shadcn Select
// where the option list can grow large (cluster/node pickers). cmdk handles the filtering.
export function SearchableSelect({
  value,
  onValueChange,
  options,
  placeholder = "Select...",
  searchPlaceholder = "Search...",
  emptyText = "No results found.",
  disabled = false,
  searchThreshold = 6,
  className,
  contentClassName,
}: SearchableSelectProps) {
  const [open, setOpen] = React.useState(false);
  const selected = options.find((o) => o.value === value);
  const showSearch = options.length > searchThreshold;
  // A combobox must name the listbox it controls, otherwise a screen reader announces the
  // trigger with no relationship to the options. Radix renders the list in a portal, so the two
  // are not adjacent in the DOM and only an explicit id ties them together.
  const listboxId = React.useId();

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          role="combobox"
          aria-expanded={open}
          aria-controls={listboxId}
          disabled={disabled}
          className={cn(
            "flex h-10 w-full items-center justify-between rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
            className
          )}
        >
          <span
            className={cn(
              "line-clamp-1 text-left",
              !selected && "text-muted-foreground"
            )}
          >
            {selected ? selected.label : placeholder}
          </span>
          <ChevronDown className="h-4 w-4 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className={cn(
          "w-[var(--radix-popover-trigger-width)] p-0",
          contentClassName
        )}
      >
        <Command>
          {showSearch && <CommandInput placeholder={searchPlaceholder} />}
          <CommandList id={listboxId}>
            <CommandEmpty>{emptyText}</CommandEmpty>
            <CommandGroup>
              {options.map((opt) => (
                <CommandItem
                  key={opt.value}
                  // include value/sublabel/badge so search matches more than the visible label
                  value={`${opt.value} ${opt.label} ${opt.sublabel ?? ""} ${opt.badge ?? ""}`}
                  disabled={opt.disabled}
                  onSelect={() => {
                    onValueChange(opt.value);
                    setOpen(false);
                  }}
                >
                  <Check
                    className={cn(
                      "mr-2 h-4 w-4 shrink-0",
                      value === opt.value ? "opacity-100" : "opacity-0"
                    )}
                  />
                  <div className="flex min-w-0 flex-1 items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate">{opt.label}</p>
                      {opt.sublabel && (
                        <p className="truncate font-mono text-xs text-muted-foreground">
                          {opt.sublabel}
                        </p>
                      )}
                    </div>
                    {opt.badge && (
                      <span className="shrink-0 text-xs text-muted-foreground">
                        {opt.badge}
                      </span>
                    )}
                  </div>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
