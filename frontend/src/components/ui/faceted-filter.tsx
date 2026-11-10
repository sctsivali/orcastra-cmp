"use client";

import * as React from "react";
import { Check } from "lucide-react";

import { cn } from "@/lib/utils";
import { FilterTrigger } from "@/components/ui/filter-trigger";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import type { FacetOption } from "@/lib/table-filter";

export interface FacetedFilterProps {
  label: string;
  options: readonly FacetOption[];
  selected: readonly string[];
  onToggle: (value: string) => void;
  onClear: () => void;
  /** The option search row appears past this many options, matching SearchableSelect. */
  searchThreshold?: number;
  disabled?: boolean;
}

/**
 * Multi-select filter popover with a count beside each value.
 *
 * important: selecting an option must not close the popover, because multi-select means
 * several toggles per visit. SearchableSelect closes on select only because it is single-select.
 */
export function FacetedFilter({
  label,
  options,
  selected,
  onToggle,
  onClear,
  searchThreshold = 6,
  disabled = false,
}: FacetedFilterProps) {
  const [open, setOpen] = React.useState(false);
  const listboxId = React.useId();
  const showSearch = options.length > searchThreshold;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <FilterTrigger
          label={label}
          count={selected.length}
          open={open}
          controls={listboxId}
          field={label}
          kind="enum"
          disabled={disabled}
        />
      </PopoverTrigger>
      {/* important: the id goes on the panel. cmdk's List sets its own `id` after spreading
          the caller's props, so an id passed to CommandList is discarded and the trigger's
          aria-controls would reference nothing. */}
      <PopoverContent id={listboxId} align="start" className="w-56 p-0">
        <Command>
          {showSearch && <CommandInput placeholder={`Search ${label.toLowerCase()}`} />}
          <CommandList>
            <CommandEmpty>No matching values.</CommandEmpty>
            <CommandGroup>
              {options.map((option) => {
                const isSelected = selected.includes(option.value);
                return (
                  <CommandItem
                    key={option.value}
                    value={option.label}
                    onSelect={() => onToggle(option.value)}
                  >
                    {/* The check and the weight carry the state. --primary is the
                        user-configurable accent and is not contrast-clamped, so it can
                        never be the only signal. */}
                    <Check
                      aria-hidden="true"
                      className={cn(
                        "mr-2 h-4 w-4 shrink-0",
                        isSelected ? "opacity-100" : "opacity-0",
                      )}
                    />
                    <span className={cn("min-w-0 flex-1 truncate", isSelected && "font-medium")}>
                      {option.label}
                    </span>
                    <span className="ml-2 shrink-0 text-xs tabular-nums text-muted-foreground">
                      {option.count}
                    </span>
                  </CommandItem>
                );
              })}
            </CommandGroup>
          </CommandList>
        </Command>
        {selected.length > 0 && (
          <div className="border-t p-1">
            <button
              type="button"
              onClick={onClear}
              className="w-full rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Clear {label.toLowerCase()}
            </button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
