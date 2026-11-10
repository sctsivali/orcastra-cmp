"use client";

import * as React from "react";
import { Check, ChevronDown, type LucideIcon } from "lucide-react";

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

// A structural type rather than the ClusterStatus enum, so this primitive imports no domain type.
// ClusterStatusMeta from lib/cluster-status.ts is assignable to it with no adapter.
export interface SearchableSelectStatus {
  // Rendered as plain text beside the option. A chip per row reads louder than the option's
  // own name in a py-1.5 row.
  label: string;
  // The caller's ordering weight. This primitive renders options in the order given.
  tone: "positive" | "caution" | "neutral" | "negative";
}

export interface SearchableSelectOption {
  value: string;
  label: string;
  // Secondary line under the label (ex; cluster endpoint), also matched while searching.
  sublabel?: string;
  // Trailing hint shown on the right (ex; hypervisor type), also matched while searching.
  //
  // important: keep this a string. The cmdk match key below is a template literal, so a
  // ReactNode here interpolates to "[object Object]" and every option matches "object".
  // Anything richer belongs in `status`.
  badge?: string;
  disabled?: boolean;
  // Extra terms matched while searching but never shown, ex; a regency's seat town.
  keywords?: string[];
  // Reachability of the thing this option names.
  status?: SearchableSelectStatus;
  // Why it is in that state, ex; "connection refused". Rendered inline, never as a tooltip:
  // command.tsx sets pointer-events-none on a disabled item, so a disabled row cannot be
  // hovered and a tooltip on it is unreachable.
  statusReason?: string;
}

interface SearchableSelectProps {
  value: string;
  onValueChange: (value: string) => void;
  options: SearchableSelectOption[];
  placeholder?: string;
  searchPlaceholder?: string;
  emptyText?: string;
  disabled?: boolean;
  // The search row renders only once the option count passes this, so short lists stay uncluttered.
  searchThreshold?: number;
  // Forwarded to the trigger so callers can keep their existing styling (ex; error border).
  className?: string;
  contentClassName?: string;
  // Optional leading glyph on the trigger, for a control that sits in a toolbar rather than
  // under a label and needs its own anchor. Decorative: the label still carries the meaning.
  icon?: LucideIcon;
  // Accessible name for a trigger with no visible <Label> beside it, ex; a toolbar filter.
  // Without one a screen reader announces the current value and nothing about what it selects.
  ariaLabel?: string;
  // Lands on the trigger, so a sibling <Label htmlFor> points at it, as with shadcn's
  // SelectTrigger.
  id?: string;
  // The notice under the field that says why the list is empty or the read failed.
  ariaDescribedBy?: string;
  // True when the read behind this list failed, so assistive tech announces it as invalid.
  ariaInvalid?: boolean;
  // Lets intent-gated callers start a read when the picker opens.
  onOpenChange?: (open: boolean) => void;
  // Async catalogues need newly-arrived options to appear without forcing a close/reopen cycle.
  // Infrastructure target pickers keep the safer frozen default.
  freezeOptionsWhileOpen?: boolean;
}

// Single-select combobox with optional in-list search. Drop-in replacement for a shadcn Select
// where the option list can grow large (cluster/node pickers). cmdk handles the filtering.
export function SearchableSelect({
  value,
  onValueChange,
  options,
  placeholder = "Select…",
  searchPlaceholder = "Search…",
  emptyText = "No results found.",
  disabled = false,
  searchThreshold = 6,
  className,
  contentClassName,
  icon: Icon,
  ariaLabel,
  id,
  ariaDescribedBy,
  ariaInvalid,
  onOpenChange,
  freezeOptionsWhileOpen = true,
}: SearchableSelectProps) {
  const [open, setOpen] = React.useState(false);
  const selected = options.find((o) => o.value === value);
  const commandRef = React.useRef<HTMLDivElement>(null);

  // important: freeze the rendered order while the popover is open. Status arrives on a poll,
  // and re-sorting a live list moves a row out from under the cursor; on a control that
  // creates infrastructure, the click then lands on the wrong cluster.
  const frozen = React.useRef(options);
  if (!open || !freezeOptionsWhileOpen) frozen.current = options;
  const rendered = open && freezeOptionsWhileOpen ? frozen.current : options;

  const showSearch = rendered.length > searchThreshold;
  // A combobox must name the listbox it controls. Radix renders the list in a portal, so only
  // an explicit id ties the two together.
  //
  // important: the id goes on PopoverContent, never on CommandList. cmdk sets its own `id` after
  // spreading the caller's props, so an id passed to the list is silently dropped.
  const listboxId = React.useId();

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        onOpenChange?.(next);
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          id={id}
          role="combobox"
          aria-label={ariaLabel}
          aria-expanded={open}
          aria-controls={listboxId}
          aria-describedby={ariaDescribedBy}
          aria-invalid={ariaInvalid || undefined}
          disabled={disabled}
          className={cn(
            // gap-2 as well as justify-between: on a w-auto trigger the two groups fill the
            // width, and without a gap the value and its status label run together
            // ("nodeOnline").
            "flex h-10 w-full items-center justify-between gap-2 rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
            className
          )}
        >
          <span className="flex min-w-0 items-center gap-2">
            {Icon && (
              <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
            )}
            <span
              className={cn(
                "line-clamp-1 text-left",
                !selected && "text-muted-foreground"
              )}
            >
              {selected ? selected.label : placeholder}
            </span>
          </span>
          <span className="flex shrink-0 items-center gap-2">
            {/* The trigger shows the status too, so it stays visible after the popover
                closes, when the operator is about to submit. */}
            {selected?.status && (
              <span className="text-xs text-muted-foreground">
                {selected.status.label}
              </span>
            )}
            <ChevronDown className="h-4 w-4 shrink-0 opacity-50" />
          </span>
        </button>
      </PopoverTrigger>
      <PopoverContent id={listboxId}
        align="start"
        // important: with no search row nothing inside cmdk holds focus, so Radix parks it on
        // this panel and Enter never reaches cmdk's handler: a keyboard user could highlight an
        // option but not choose it. This applies to every list at or under searchThreshold.
        onOpenAutoFocus={(event) => {
          if (showSearch) return;
          event.preventDefault();
          commandRef.current?.focus();
        }}
        className={cn(
          "w-[var(--radix-popover-trigger-width)] p-0",
          contentClassName
        )}
      >
        <Command ref={commandRef} tabIndex={-1} className="outline-none">
          {showSearch && <CommandInput placeholder={searchPlaceholder} />}
          <CommandList>
            <CommandEmpty>{emptyText}</CommandEmpty>
            <CommandGroup>
              {rendered.map((opt) => (
                <CommandItem
                  key={opt.value}
                  // Value, sublabel, badge, status and keywords all feed the match, so typing
                  // "offline" narrows the list to the clusters in that state.
                  value={`${opt.value} ${opt.label} ${opt.sublabel ?? ""} ${opt.badge ?? ""} ${opt.status?.label ?? ""} ${opt.keywords?.join(" ") ?? ""}`}
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
                        <p className="break-all font-mono text-xs text-muted-foreground">
                          {opt.sublabel}
                        </p>
                      )}
                      {opt.disabled && opt.statusReason && (
                        <p className="break-words text-xs text-muted-foreground">
                          {opt.statusReason}
                        </p>
                      )}
                    </div>
                    <span className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                      {opt.badge && <span>{opt.badge}</span>}
                      {opt.status && (
                        <span>
                          <span className="sr-only">Status: </span>
                          {opt.status.label}
                        </span>
                      )}
                    </span>
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
