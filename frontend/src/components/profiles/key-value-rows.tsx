"use client";

import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { rowId, type ConfigRow } from "./profile-rules";

interface KeyValueRowsProps {
  rows: ConfigRow[];
  onChange: (rows: ConfigRow[]) => void;
  errors: Record<string, string>;
  /** Names the rows for a screen reader and the remove buttons, ex; "config key". */
  noun: string;
  /** What the operator reads when there are no rows. */
  emptyText: string;
  addLabel: string;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
  disabled?: boolean;
  /** Prefix for the input ids, so two editors on one dialog do not collide. */
  idPrefix: string;
}

/**
 * One key/value editor for a profile's config and for each device's properties, so the two
 * cannot drift. Labels sit above the inputs, the reason for a bad row sits under it, and every
 * icon-only button carries a name.
 */
export function KeyValueRows({
  rows,
  onChange,
  errors,
  noun,
  emptyText,
  addLabel,
  keyPlaceholder = "key",
  valuePlaceholder = "value",
  disabled,
  idPrefix,
}: KeyValueRowsProps) {
  const patch = (id: string, next: Partial<ConfigRow>) =>
    onChange(rows.map((r) => (r.id === id ? { ...r, ...next } : r)));
  const remove = (id: string) => onChange(rows.filter((r) => r.id !== id));
  const add = () => onChange([...rows, { id: rowId(), key: "", value: "" }]);

  return (
    <div className="space-y-2">
      {rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">{emptyText}</p>
      ) : (
        rows.map((row, index) => (
          <div key={row.id} className="grid grid-cols-[1fr_1fr_auto] gap-2 items-start">
            <div className="space-y-1">
              {index === 0 && (
                <Label htmlFor={`${idPrefix}-${row.id}-key`} className="text-xs text-muted-foreground">
                  Key
                </Label>
              )}
              <Input
                id={`${idPrefix}-${row.id}-key`}
                className="font-mono"
                value={row.key}
                onChange={(e) => patch(row.id, { key: e.target.value })}
                placeholder={keyPlaceholder}
                disabled={disabled}
                aria-label={index === 0 ? undefined : `${noun} ${index + 1} key`}
                aria-invalid={errors[row.id] ? true : undefined}
                aria-describedby={errors[row.id] ? `${idPrefix}-${row.id}-error` : undefined}
              />
              {errors[row.id] && (
                <p id={`${idPrefix}-${row.id}-error`} className="text-xs text-destructive">
                  {errors[row.id]}
                </p>
              )}
            </div>
            <div className="space-y-1">
              {index === 0 && (
                <Label htmlFor={`${idPrefix}-${row.id}-value`} className="text-xs text-muted-foreground">
                  Value
                </Label>
              )}
              <Input
                id={`${idPrefix}-${row.id}-value`}
                className="font-mono"
                value={row.value}
                onChange={(e) => patch(row.id, { value: e.target.value })}
                placeholder={valuePlaceholder}
                disabled={disabled}
                aria-label={index === 0 ? undefined : `${noun} ${index + 1} value`}
              />
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className={index === 0 ? "mt-5" : undefined}
              onClick={() => remove(row.id)}
              disabled={disabled}
              aria-label={`Remove ${noun} ${row.key || index + 1}`}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        ))
      )}
      <Button type="button" variant="outline" size="sm" onClick={add} disabled={disabled}>
        <Plus className="h-4 w-4 mr-2" />
        {addLabel}
      </Button>
    </div>
  );
}
