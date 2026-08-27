"use client";

import { Building2, User } from "lucide-react";
import { Button } from "@/components/ui/button";

export type SessionScope = "self" | "org";

/**
 * Whose sessions to show.
 *
 * The backend has always supported an organisation scope on the audit endpoint and the
 * page never asked for it, so a partner could not see who was on their own clusters.
 * Mirrors the Live/Paused control beside it rather than introducing a second toggle
 * idiom on the same card.
 */
export function SessionScopeToggle({
  scope,
  onChange,
  disabled = false,
}: {
  scope: SessionScope;
  onChange: (scope: SessionScope) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-center gap-1" role="group" aria-label="Session scope">
      <Button
        variant={scope === "self" ? "default" : "outline"}
        size="sm"
        onClick={() => onChange("self")}
        aria-pressed={scope === "self"}
        disabled={disabled}
        className="gap-1.5"
      >
        <User className="h-3.5 w-3.5" />
        Mine
      </Button>
      <Button
        variant={scope === "org" ? "default" : "outline"}
        size="sm"
        onClick={() => onChange("org")}
        aria-pressed={scope === "org"}
        disabled={disabled}
        className="gap-1.5"
      >
        <Building2 className="h-3.5 w-3.5" />
        Organization
      </Button>
    </div>
  );
}
