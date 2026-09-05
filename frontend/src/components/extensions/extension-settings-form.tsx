"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import type { ExtensionSettingField } from "@/types/api";

/**
 * The add-on's own configuration, rendered by us from what its manifest declared.
 *
 * The add-on ships no UI, so this form is drawn from a closed vocabulary of six field types. That
 * is the whole reason it is closed: an open schema language would let an author describe a form
 * this dashboard cannot render consistently, and the first time that happens is in front of an
 * operator.
 *
 * important: a secret is write-only. The server returns the NAMES of the secret fields that hold
 * a value, never the values, so a configured secret shows as configured and its input starts
 * empty. Leaving it empty on save keeps what is stored; typing replaces it. Without that, saving
 * an unrelated field would blank every credential nobody retyped.
 */
export function ExtensionSettingsForm({
  schema,
  values,
  configuredSecrets,
  onSave,
  pending,
}: {
  schema: ExtensionSettingField[];
  values: Record<string, unknown>;
  configuredSecrets: string[];
  onSave: (values: Record<string, unknown>) => void;
  pending: boolean;
}) {
  const [draft, setDraft] = useState<Record<string, unknown>>({});

  useEffect(() => {
    const next: Record<string, unknown> = {};
    for (const field of schema) {
      if (field.type === "secret") continue; // never prefilled, because it is never returned
      next[field.key] = values[field.key] ?? field.default ?? (field.type === "boolean" ? false : "");
    }
    setDraft(next);
  }, [schema, values]);

  if (schema.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        This add-on declares no settings, so there is nothing to configure here.
      </p>
    );
  }

  const set = (key: string, value: unknown) => setDraft((d) => ({ ...d, [key]: value }));

  const submit = () => {
    // A secret left blank is omitted entirely rather than sent as an empty string, which is what
    // makes "leave it alone" different from "clear it".
    const payload: Record<string, unknown> = {};
    for (const field of schema) {
      const value = draft[field.key];
      if (field.type === "secret" && (value === undefined || value === "")) continue;
      payload[field.key] = value;
    }
    onSave(payload);
  };

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      {schema.map((field) => {
        const id = `setting-${field.key}`;
        const configured = configuredSecrets.includes(field.key);
        return (
          <div key={field.key} className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <Label htmlFor={id}>{field.label}</Label>
              {field.required ? (
                <Badge variant="muted" className="shrink-0">
                  Required
                </Badge>
              ) : null}
              {field.type === "secret" && configured ? (
                <Badge variant="positive" className="shrink-0">
                  Configured
                </Badge>
              ) : null}
            </div>

            {field.type === "boolean" ? (
              <Switch
                id={id}
                checked={Boolean(draft[field.key])}
                onCheckedChange={(value) => set(field.key, value)}
              />
            ) : field.type === "enum" ? (
              <Select
                value={String(draft[field.key] ?? "")}
                onValueChange={(value) => set(field.key, value)}
              >
                <SelectTrigger id={id}>
                  <SelectValue placeholder="Choose one" />
                </SelectTrigger>
                <SelectContent>
                  {(field.options ?? []).map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <Input
                id={id}
                type={field.type === "secret" ? "password" : field.type === "integer" ? "number" : "text"}
                inputMode={field.type === "integer" ? "numeric" : undefined}
                min={field.min ?? undefined}
                max={field.max ?? undefined}
                autoComplete={field.type === "secret" ? "new-password" : "off"}
                value={String(draft[field.key] ?? "")}
                onChange={(event) =>
                  set(
                    field.key,
                    field.type === "integer"
                      ? event.target.value === ""
                        ? ""
                        : Number(event.target.value)
                      : event.target.value,
                  )
                }
              />
            )}

            {field.help ? (
              <p className="text-sm text-muted-foreground">{field.help}</p>
            ) : field.type === "secret" && configured ? (
              <p className="text-sm text-muted-foreground">
                Stored. Leave blank to keep it, or type a new value to replace it.
              </p>
            ) : null}
          </div>
        );
      })}

      <Button type="submit" disabled={pending}>
        {pending ? (
          <>
            <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
            Saving
          </>
        ) : (
          "Save Settings"
        )}
      </Button>
    </form>
  );
}
