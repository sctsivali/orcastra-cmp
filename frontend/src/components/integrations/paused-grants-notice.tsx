"use client";

import { AlertTriangle } from "lucide-react";

import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import type { ClusterPermission } from "@/types";

interface PausedGrantsNoticeProps {
  /** Grants the server reports with `registered: false`. */
  grants: ClusterPermission[];
  rebind: string[];
  onToggle: (clusterId: string) => void;
  disabled?: boolean;
}

/**
 * The grants on a key whose registration has ended, and the one way to make one reach again.
 *
 * A cluster id can be removed and registered again, possibly by another organization. A grant
 * written for the earlier registration reaches nothing until someone allowed on the new one grants
 * it again, so saving this dialog for any other reason leaves it paused. Ticking "Grant again"
 * sends the id as a rebind.
 */
export function PausedGrantsNotice({ grants, rebind, onToggle, disabled }: PausedGrantsNoticeProps) {
  if (grants.length === 0) return null;
  return (
    <div role="status" className="space-y-2 rounded-md border p-3 text-xs">
      <div className="flex items-start gap-2">
        <AlertTriangle aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-pending" />
        <p className="text-muted-foreground">
          Access is paused where the cluster this key was granted has been removed. If its id is
          registered again, possibly by another organization, the grant stays paused until you
          grant it again.
        </p>
      </div>
      <ul className="space-y-1.5">
        {grants.map((grant, index) => {
          const id = String(grant.cluster_id);
          const checkboxId = `rebind-${index}`;
          return (
            <li key={`${id}-${index}`} className="flex items-center justify-between gap-3">
              <span className="min-w-0 break-all font-mono">{id}</span>
              {grant.rebindable ? (
                <div className="flex shrink-0 items-center gap-2">
                  <Checkbox
                    id={checkboxId}
                    checked={rebind.includes(id)}
                    onCheckedChange={() => onToggle(id)}
                    disabled={disabled}
                  />
                  <Label htmlFor={checkboxId} className="text-xs font-normal">
                    Grant again
                  </Label>
                </div>
              ) : (
                <span className="shrink-0 text-muted-foreground">No cluster is registered under this id</span>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
