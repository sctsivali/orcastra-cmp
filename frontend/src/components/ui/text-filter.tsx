"use client";

import * as React from "react";

import { cn } from "@/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { SearchInput } from "@/components/ui/search-input";
import { FilterTrigger } from "@/components/ui/filter-trigger";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import type { EmptyMode, TextState } from "@/lib/filter-url";

export interface TextFilterProps {
  label: string;
  summary?: string;
  value: TextState;
  onChange: (next: TextState) => void;
  placeholder?: string;
  emptyLabel?: string;
  /** Match count over the co-filtered rows. Honest here because a typed query is a predicate. */
  facts?: { emptyCount: number; matched: number; scoped: number };
  disabled?: boolean;
}

const EMPTY_MODES: { id: EmptyMode; label: string }[] = [
  { id: "exclude", label: "Hide" },
  { id: "include", label: "Show" },
  { id: "only", label: "Only" },
];

/**
 * Contains-text for one column.
 *
 * note: this is not a duplicate of the toolbar search. The search box spans several columns at
 * once; this scopes to one, which is a different question. Where a table's search covers this
 * column and no other, the column is kept out of the "Add filter" menu so there is still one
 * control per piece of state.
 *
 * The matching rule is deliberately identical to the toolbar search: case-insensitive
 * substring over every string the accessor returns, so there is one rule in the product
 * rather than two that drift.
 */
export function TextFilter({
  label,
  summary,
  value,
  onChange,
  placeholder,
  emptyLabel = "(none)",
  facts,
  disabled,
}: TextFilterProps) {
  const [open, setOpen] = React.useState(false);
  const panelId = React.useId();
  const [draft, setDraft] = React.useState(value.query);
  const debounced = useDebouncedValue(draft, 250);
  const lastCommitted = React.useRef(value.query);

  React.useEffect(() => {
    if (debounced === lastCommitted.current) return;
    lastCommitted.current = debounced;
    onChange({ ...value, query: debounced });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced]);

  React.useEffect(() => {
    if (value.query === lastCommitted.current) return;
    lastCommitted.current = value.query;
    setDraft(value.query);
  }, [value.query]);

  const active = value.query !== "" || value.empty !== "exclude";

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <FilterTrigger
          label={label}
          summary={summary}
          open={open}
          controls={panelId}
          field={label}
          kind="text"
          disabled={disabled}
        />
      </PopoverTrigger>
      <PopoverContent id={panelId} align="start" className="w-72 p-3">
        <div className="space-y-3">
          <SearchInput
            value={draft}
            onChange={setDraft}
            placeholder={placeholder ?? `Contains…`}
            label={`Filter ${label} by text`}
          />

          {facts && value.query !== "" && (
            <p className="text-xs text-muted-foreground" role="status" aria-live="polite">
              {facts.matched} of {facts.scoped} match
            </p>
          )}

          <div className="space-y-1">
            <span id={`${panelId}-empty`} className="text-xs text-muted-foreground">
              Rows with no value{facts ? ` (${facts.emptyCount})` : ""}
            </span>
            <div role="radiogroup" aria-labelledby={`${panelId}-empty`} className="flex gap-1">
              {EMPTY_MODES.map((mode) => (
                <button
                  key={mode.id}
                  type="button"
                  role="radio"
                  aria-checked={value.empty === mode.id}
                  onClick={() => onChange({ ...value, empty: mode.id })}
                  className={cn(
                    "flex-1 rounded-md border px-2 py-1 text-xs hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    value.empty === mode.id && "border-ring font-medium",
                  )}
                >
                  {mode.id === "only" ? emptyLabel : mode.label}
                </button>
              ))}
            </div>
          </div>

          {active && (
            <button
              type="button"
              onClick={() => {
                setDraft("");
                lastCommitted.current = "";
                onChange({ query: "", empty: "exclude" });
              }}
              className="w-full rounded-sm border-t px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Clear {label.toLowerCase()}
            </button>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
