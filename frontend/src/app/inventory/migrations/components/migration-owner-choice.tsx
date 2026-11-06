"use client";

import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { MigrationOwnerCandidate } from "@/types/migrations";

/**
 * The organization an Admin names as the owner of an instance in an ambiguous `default`.
 *
 * Shown only after the server asks (a 409 that only an Admin receives), and never preselected:
 * an instance attributed to the wrong organization hands that organization's partners the job.
 */
export function MigrationOwnerChoice({
  candidates,
  value,
  onChange,
}: {
  candidates: MigrationOwnerCandidate[];
  value: number | null;
  onChange: (organizationId: number) => void;
}) {
  return (
    <div className="space-y-2">
      <Label htmlFor="migration-source-owner">Owning Organization</Label>
      <Select
        value={value === null ? undefined : String(value)}
        onValueChange={(next) => onChange(Number(next))}
        disabled={candidates.length === 0}
      >
        <SelectTrigger id="migration-source-owner" aria-describedby="migration-source-owner-help">
          <SelectValue placeholder={candidates.length ? "Choose an organization" : "No organization can be named"} />
        </SelectTrigger>
        <SelectContent>
          {candidates.map((candidate) => (
            <SelectItem key={candidate.id} value={String(candidate.id)}>
              {candidate.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <p id="migration-source-owner-help" className="text-xs text-muted-foreground">
        {candidates.length
          ? "The default project on the source cluster is held by several organizations. Choose the one that owns this instance; it must also own the destination."
          : "No organization bound to the source cluster owns the destination, so this instance cannot be moved there."}
      </p>
    </div>
  );
}
