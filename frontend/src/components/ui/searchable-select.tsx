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

// A structural slice, deliberately not the ClusterStatus enum: a components/ui primitive that
// imports a domain type stops being reusable. ClusterStatusMeta from lib/cluster-status.ts is
// assignable to this with no adapter, so there is still exactly one mapping in the codebase.
export interface SearchableSelectStatus {
  // Always rendered next to the dot. Colour is never the only carrier of the state.
  label: string;
  // Background utility from the --status-* tokens, ex; "bg-status-offline".
  dotClassName: string;
  // Ordering weight owned by the caller's vocabulary, not by this primitive.
  tone: "positive" | "caution" | "neutral" | "negative";
}

export interface SearchableSelectOption {
  value: string;
  label: string;
  // secondary line under the label (ex; cluster endpoint), also matched while searching
  sublabel?: string;
  // trailing hint shown on the right (ex; hypervisor type), also matched while searching
  //
  // important: keep this a string. The cmdk match key below is a template literal, so a
  // ReactNode here interpolates to "[object Object]": every option would match the query
  // "object" and contribute nothing else to search. Anything richer belongs in `status`.
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
  // note: the search row only renders once the option count passes this, so short lists stay uncluttered
  searchThreshold?: number;
  // forwarded to the trigger so callers can keep their existing styling (ex; error border)
  className?: string;
  contentClassName?: string;
  // Optional leading glyph on the trigger, for a control that sits in a toolbar rather than
  // under a label and needs its own anchor. Decorative: the label still carries the meaning.
  icon?: LucideIcon;
  // Accessible name for a trigger with no visible <Label> beside it, ex; a toolbar filter.
  // Without one a screen reader announces the current value and nothing about what it selects.
  ariaLabel?: string;
  // Lands on the trigger, so a sibling <Label htmlFor> actually points at something. shadcn's
  // SelectTrigger took an id and several call sites still pass one to a label that would
  // otherwise dangle.
  id?: string;
  // The notice under the field that says why the list is empty or the read failed.
  ariaDescribedBy?: string;
  // True when the read behind this list failed, so assistive tech announces it as invalid.
  ariaInvalid?: boolean;
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
  icon: Icon,
  ariaLabel,
  id,
  ariaDescribedBy,
  ariaInvalid,
}: SearchableSelectProps) {
  const [open, setOpen] = React.useState(false);
  const selected = options.find((o) => o.value === value);
  const commandRef = React.useRef<HTMLDivElement>(null);

  // important: freeze the rendered order while the popover is open. Status arrives on a poll,
  // and re-sorting a live list moves a row out from under the cursor between the eye and the
  // click. On a control that creates real infrastructure, that is a wrong cluster, not a
  // cosmetic glitch.
  const frozen = React.useRef(options);
  if (!open) frozen.current = options;
  const rendered = open ? frozen.current : options;

  const showSearch = rendered.length > searchThreshold;
  const anyStatus = rendered.some((o) => o.status);
  // A combobox must name the listbox it controls, otherwise a screen reader announces the
  // trigger with no relationship to the options. Radix renders the list in a portal, so the two
  // are not adjacent in the DOM and only an explicit id ties them together.
  //
  // important: the id goes on PopoverContent, never on CommandList. cmdk sets its own `id` after
  // spreading the caller's props, so an id passed to the list is silently dropped and this
  // reference resolves to nothing. It sat that way here until an audit rendered the two
  // libraries and checked which id actually reached the DOM.
  const listboxId = React.useId();

  return (
    <Popover open={open} onOpenChange={setOpen}>
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
            // gap-2, not only justify-between: on a w-auto trigger the two groups fill the
            // width and justify-between then leaves no space at all, so the value and its
            // status label rendered as one word ("nodeOnline").
            "flex h-10 w-full items-center justify-between gap-2 rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
            className
          )}
        >
          <span className="flex min-w-0 items-center gap-2">
            {Icon && (
              <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
            )}
            {selected?.status && (
              <span
                aria-hidden
                className={cn(
                  "h-1.5 w-1.5 shrink-0 rounded-full",
                  selected.status.dotClassName
                )}
              />
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
            {/* The trigger has to carry the state too. Without it the whole signal vanishes
                the moment the popover closes, which is exactly when the operator is about to
                submit against the cluster they picked. */}
            {selected?.status && (
              <span className="text-xs text-muted-foreground">
                {selected.status.label}
              </span>
            )}
            <ChevronDown className="h-4 w-4 shrink-0 opacity-50" />
          </span>
        </button>
      </PopoverTrigger>
      {/* The id belongs on the panel. cmdk's List sets its own id after spreading the
          caller's props, so an id passed there is dropped and this combobox's aria-controls
          resolved to nothing. A combobox is required to reference its popup. */}
      <PopoverContent id={listboxId}
        align="start"
        // important: with no search row there is nothing inside cmdk holding focus, so Radix
        // parks it on this panel instead. Arrow keys still moved the highlight, but Enter never
        // reached cmdk's handler and a keyboard user could highlight a cluster and not choose
        // it. That is every list at or under searchThreshold, which is the monitoring filter and
        // most of what a partner or tenant sees.
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
                  // include value/sublabel/badge/status so search matches more than the visible
                  // label. The status label is in here on purpose: typing "offline" narrows the
                  // list to the broken clusters, which is how an operator finds them rather
                  // than only avoids them.
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
                  {/* Reserved even when this row has no status, so labels keep one
                      x-position and nothing shifts sideways when reachability lands
                      after the names. */}
                  {anyStatus && (
                    <span
                      aria-hidden
                      className={cn(
                        "mr-2 h-1.5 w-1.5 shrink-0 rounded-full",
                        opt.status ? opt.status.dotClassName : "invisible"
                      )}
                    />
                  )}
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
