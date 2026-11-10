"use client";

import * as React from "react";

import { cn } from "@/lib/utils";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { FilterTrigger } from "@/components/ui/filter-trigger";
import {
  DATE_PRESET_LABELS,
  toDayString,
  type DatePresetId,
  type DateState,
  type EmptyMode,
} from "@/lib/filter-url";

export interface DateFilterProps {
  label: string;
  summary?: string;
  value: DateState;
  onChange: (next: DateState) => void;
  presets: readonly DatePresetId[];
  emptyLabel?: string;
  facts?: { min: number | null; max: number | null; emptyCount: number };
  disabled?: boolean;
}

const EMPTY_MODES: { id: EmptyMode; label: string }[] = [
  { id: "exclude", label: "Hide" },
  { id: "include", label: "Show" },
  { id: "only", label: "Only" },
];

const DAY = 86_400_000;

/**
 * Presets plus two native date inputs.
 *
 * No calendar component: react-day-picker peers on date-fns v2/v3 while this repo is on v4,
 * and a keyboard-accessible month grid is a lot of code to own. A native date input is
 * keyboard-operable, locale-aware and labelled, and the common questions (expiring soon,
 * created today, last 7 days) are one preset click.
 *
 * The only count shown is for rows with no date.
 */
export function DateFilter({
  label,
  summary,
  value,
  onChange,
  presets,
  emptyLabel = "(none)",
  facts,
  disabled,
}: DateFilterProps) {
  const [open, setOpen] = React.useState(false);
  const panelId = React.useId();

  const fromDraft = value.min === null ? "" : toDayString(value.min);
  const toDraft = value.max === null ? "" : toDayString(value.max);

  const setBound = (which: "from" | "to", raw: string) => {
    const parsed = raw === "" ? null : new Date(`${raw}T00:00:00`).getTime();
    const valid = parsed !== null && Number.isFinite(parsed) ? parsed : null;
    // Choosing a custom bound clears the preset: they are one piece of state with two
    // spellings, and the URL stores whichever the operator last chose.
    if (which === "from") onChange({ ...value, preset: null, min: valid });
    else onChange({ ...value, preset: null, max: valid === null ? null : valid + DAY - 1 });
  };

  const active = value.preset !== null || value.min !== null || value.max !== null || value.empty !== "exclude";

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <FilterTrigger
          label={label}
          summary={summary}
          open={open}
          controls={panelId}
          field={label}
          kind="date"
          disabled={disabled}
        />
      </PopoverTrigger>
      <PopoverContent id={panelId} align="start" className="w-72 p-3">
        <div className="space-y-3">
          <div role="radiogroup" aria-label={`${label} presets`} className="flex flex-wrap gap-1.5">
            {presets.map((id) => (
              <button
                key={id}
                type="button"
                role="radio"
                aria-checked={value.preset === id}
                onClick={() =>
                  onChange({
                    ...value,
                    preset: value.preset === id ? null : id,
                    min: null,
                    max: null,
                  })
                }
                className={cn(
                  "rounded-md border px-2 py-1 text-xs hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  value.preset === id && "border-ring font-medium",
                )}
              >
                {DATE_PRESET_LABELS[id]}
              </button>
            ))}
          </div>

          <div className="grid grid-cols-2 gap-2 border-t pt-3">
            <div className="space-y-1">
              <Label htmlFor={`${panelId}-from`} className="text-xs">From</Label>
              <Input
                id={`${panelId}-from`}
                type="date"
                value={fromDraft}
                onChange={(e) => setBound("from", e.target.value)}
                className="h-8"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor={`${panelId}-to`} className="text-xs">To</Label>
              <Input
                id={`${panelId}-to`}
                type="date"
                value={toDraft}
                onChange={(e) => setBound("to", e.target.value)}
                className="h-8"
              />
            </div>
          </div>

          {facts && facts.min !== null && facts.max !== null && (
            <p className="text-xs text-muted-foreground">
              Dates here run {toDayString(facts.min)} to {toDayString(facts.max)}.
            </p>
          )}

          <div className="space-y-1">
            <span id={`${panelId}-empty`} className="text-xs text-muted-foreground">
              Rows with no date{facts ? ` (${facts.emptyCount})` : ""}
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
              onClick={() => onChange({ min: null, max: null, empty: "exclude", preset: null })}
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
