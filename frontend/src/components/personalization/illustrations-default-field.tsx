"use client";

import * as React from "react";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { isIllustrationPreference, type IllustrationPreference } from "@/types/personalization";

/**
 * Whether cards show their illustration, for everyone this branding applies to, beside Default Theme
 * in the branding editor. It is the only place the choice is made (owner decision 2026-11-09): there
 * is no per-account override, so a tenant sees what its organization chose.
 */
export function IllustrationsDefaultField({
  value,
  onChange,
}: {
  value: IllustrationPreference;
  onChange: (choice: IllustrationPreference) => void;
}) {
  return (
    <div className="space-y-2">
      <Label htmlFor="defaultIllustrations">Card Illustrations</Label>
      <Select value={value} onValueChange={(next) => isIllustrationPreference(next) && onChange(next)}>
        <SelectTrigger id="defaultIllustrations" className="w-full sm:w-[220px]">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="show">Show</SelectItem>
          <SelectItem value="hide">Hide</SelectItem>
        </SelectContent>
      </Select>
      <p className="text-xs text-muted-foreground">
        Whether cards show their illustration for everyone this branding applies to.
      </p>
    </div>
  );
}
