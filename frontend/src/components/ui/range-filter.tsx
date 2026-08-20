"use client";

import * as React from "react";

import { cn } from "@/lib/utils";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { FilterTrigger } from "@/components/ui/filter-trigger";
import type { EmptyMode, RangeState } from "@/lib/filter-url";
import type { NumberPreset, NumberUnit } from "@/lib/table-filter";

export interface RangeFilterProps {
  label: string;
  summary?: string;
  value: RangeState;
  onChange: (next: RangeState) => void;
  unit: NumberUnit;
  integer: boolean;
  presets?: readonly NumberPreset[];
  emptyLabel?: string;
  /** Observed bounds over the co-filtered rows. Help text, never a default. */
  facts?: { min: number | null; max: number | null; emptyCount: number; matched: number; scoped: number };
  formatBound: (value: number, unit: string) => string;
  disabled?: boolean;
}

const EMPTY_MODES: { id: EmptyMode; label: string }[] = [
  { id: "exclude", label: "Hide" },
  { id: "include", label: "Show" },
  { id: "only", label: "Only" },
];

/**
 * Min and max bounds for a numeric column.
 *
 * note: Popover without cmdk. There is no option list to filter, and Command intercepts arrow
 * keys and typed characters, which fights a numeric input. cmdk is for lists; this is a form.
 *
 * important: the bounds are never pre-filled from the data. A pre-filled range is an active
 * filter nobody asked for, it would make every page load look filtered, and it would break the
 * assertion that a clean page carries no chips.
 */
export function RangeFilter({
  label,
  summary,
  value,
  onChange,
  unit,
  integer,
  presets,
  emptyLabel = "(none)",
  facts,
  formatBound,
  disabled,
}: RangeFilterProps) {
  const [open, setOpen] = React.useState(false);
  const panelId = React.useId();
  const [minDraft, setMinDraft] = React.useState("");
  const [maxDraft, setMaxDraft] = React.useState("");

  React.useEffect(() => {
    setMinDraft(value.min === null ? "" : String(value.min));
    setMaxDraft(value.max === null ? "" : String(value.max));
  }, [value.min, value.max, open]);

  const parse = (raw: string): number | null => {
    if (raw.trim() === "") return null;
    const n = Number(raw);
    if (!Number.isFinite(n)) return null;
    return integer ? Math.trunc(n) : n;
  };

  // Commit on blur and Enter rather than per keystroke: every keystroke would be a router
  // write, and a numeric field has a natural commit point.
  const commit = () => {
    let min = parse(minDraft);
    let max = parse(maxDraft);
    // A reversed range is swapped, not rejected. An unremovable validation error inside a
    // popover is worse than a range that means what the numbers say.
    if (min !== null && max !== null && min > max) [min, max] = [max, min];
    if (min !== value.min || max !== value.max) onChange({ ...value, min, max });
  };

  const active = value.min !== null || value.max !== null || value.empty !== "exclude";

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (!next) commit();
        setOpen(next);
      }}
    >
      <PopoverTrigger asChild>
        <FilterTrigger
          label={label}
          summary={summary}
          open={open}
          controls={panelId}
          field={label}
          kind="number"
          disabled={disabled}
        />
      </PopoverTrigger>
      <PopoverContent id={panelId} align="start" className="w-72 p-3">
        <div className="space-y-3">
          {presets && presets.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {presets.map((preset) => {
                const on = value.min === preset.min && value.max === preset.max;
                return (
                  <button
                    key={preset.label}
                    type="button"
                    aria-pressed={on}
                    onClick={() => onChange({ ...value, min: preset.min, max: preset.max })}
                    className={cn(
                      "rounded-md border px-2 py-1 text-xs hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      on && "border-ring font-medium",
                    )}
                  >
                    {preset.label}
                  </button>
                );
              })}
            </div>
          )}

          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <Label htmlFor={`${panelId}-min`} className="text-xs">Min</Label>
              <Input
                id={`${panelId}-min`}
                type="number"
                inputMode="decimal"
                value={minDraft}
                onChange={(e) => setMinDraft(e.target.value)}
                onBlur={commit}
                onKeyDown={(e) => e.key === "Enter" && commit()}
                className="h-8"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor={`${panelId}-max`} className="text-xs">Max</Label>
              <Input
                id={`${panelId}-max`}
                type="number"
                inputMode="decimal"
                value={maxDraft}
                onChange={(e) => setMaxDraft(e.target.value)}
                onBlur={commit}
                onKeyDown={(e) => e.key === "Enter" && commit()}
                className="h-8"
              />
            </div>
          </div>

          {facts && facts.min !== null && facts.max !== null && (
            <p className="text-xs text-muted-foreground">
              Values here run {formatBound(facts.min, unit)} to {formatBound(facts.max, unit)}.
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
                  {mode.label}
                </button>
              ))}
            </div>
          </div>

          {active && (
            <button
              type="button"
              onClick={() => onChange({ min: null, max: null, empty: "exclude" })}
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
