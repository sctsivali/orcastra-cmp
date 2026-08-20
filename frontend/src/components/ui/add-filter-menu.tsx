"use client";

import * as React from "react";
import { ListFilter } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";

export interface AddFilterOption {
  field: string;
  label: string;
  kind?: string;
}

/** Kind is what an operator is choosing between when scanning ten columns, so group by it. */
const GROUPS: { kind: string; heading: string }[] = [
  { kind: "enum", heading: "Attributes" },
  { kind: "number", heading: "Numbers" },
  { kind: "date", heading: "Dates" },
  { kind: "text", heading: "Text" },
];

export interface AddFilterMenuProps {
  options: readonly AddFilterOption[];
  onAdd: (field: string) => void;
  label?: string;
  searchThreshold?: number;
}

/**
 * Reveals a filter for a column that is not one of the page's primary facets.
 *
 * Renders nothing when there is nothing left to add, the same guard TagFilter uses when a
 * fleet has no tags. An empty dropdown is worse than no control.
 */
export function AddFilterMenu({
  options,
  onAdd,
  label = "Add filter",
  searchThreshold = 5,
}: AddFilterMenuProps) {
  const [open, setOpen] = React.useState(false);
  const listboxId = React.useId();

  if (options.length === 0) return null;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={listboxId}
          className="flex h-9 items-center gap-2 rounded-md border border-dashed border-input bg-background px-3 text-sm ring-offset-background hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <ListFilter aria-hidden="true" className="h-4 w-4 shrink-0 opacity-70" />
          <span>{label}</span>
        </button>
      </PopoverTrigger>
      {/* The id belongs on the panel: cmdk's List overwrites a caller-supplied id with its
          own, so an id passed to CommandList is discarded and the trigger references nothing. */}
      <PopoverContent id={listboxId} align="start" className="w-52 p-0">
        <Command>
          {options.length > searchThreshold && <CommandInput placeholder="Search columns" />}
          <CommandList>
            <CommandEmpty>No columns left to filter.</CommandEmpty>
            {GROUPS.map(({ kind, heading }) => {
              const inGroup = options.filter((o) => (o.kind ?? "enum") === kind);
              if (inGroup.length === 0) return null;
              return (
                <CommandGroup key={kind} heading={heading}>
                  {inGroup.map((option) => (
                    <CommandItem
                      key={option.field}
                      value={option.label}
                      onSelect={() => {
                        onAdd(option.field);
                        setOpen(false);
                      }}
                    >
                      {option.label}
                    </CommandItem>
                  ))}
                </CommandGroup>
              );
            })}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
