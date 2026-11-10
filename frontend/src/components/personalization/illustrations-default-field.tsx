"use client";

import * as React from "react";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { isIllustrationPreference, type IllustrationPreference } from "@/types/personalization";

/**
 * Branding editor field: whether cards show illustrations for everyone the branding applies to.
 * There is no per-account override.
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
        Off by default. Show adds an illustration to cards for everyone this branding applies to.
      </p>
    </div>
  );
}
