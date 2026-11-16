"use client";

import { useId } from "react";
import { AlertTriangle, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface RebuildWarningProps {
  instanceName: string;
  mode: "image" | "empty" | "cloud-init";
  volumes: string[];
  acknowledged: boolean;
  onAcknowledgedChange: (acknowledged: boolean) => void;
  confirmName: string;
  onConfirmNameChange: (value: string) => void;
}

/**
 * What a rebuild destroys and what it keeps, then the two confirmations every role gives: a ticked
 * acknowledgement and the instance name typed out. The server checks the name again.
 */
export function RebuildWarning({
  instanceName,
  mode,
  volumes,
  acknowledged,
  onAcknowledgedChange,
  confirmName,
  onConfirmNameChange,
}: RebuildWarningProps) {
  const id = useId();
  return (
    <div className="space-y-3">
      <div className="space-y-2 rounded-md border border-destructive/40 p-3 text-sm" role="note" aria-labelledby={`${id}-lost`}>
        <p id={`${id}-lost`} className="flex items-center gap-2 font-medium text-destructive">
          <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
          What is lost
        </p>
        <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
          <li>
            <span className="font-medium text-foreground">Everything on the root disk is erased permanently</span>:
            files, installed software, users, passwords and SSH keys inside the guest. It cannot be undone,
            and no snapshot is kept to go back to.
          </li>
          <li>
            The instance is stopped now and stays offline until the new system has booted, usually a few
            minutes.
          </li>
          {mode === "empty" ? (
            <li>The new root disk is blank. The instance stays stopped until an install ISO is attached.</li>
          ) : (
            <li>The new system has a new SSH host key, so SSH clients warn about a changed key.</li>
          )}
          {mode === "cloud-init" && <li>The user-data entered here replaces any the instance had.</li>}
        </ul>
      </div>

      <div className="flex gap-2 rounded-md border bg-muted/30 p-3 text-sm">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-status-online" aria-hidden="true" />
        <div className="space-y-1 text-muted-foreground">
          <p>
            <span className="font-medium text-foreground">Kept</span>: the name, size and profiles, the
            network configuration and IP address.
          </p>
          {volumes.length > 0 ? (
            <p>
              Attached data volume{volumes.length === 1 ? "" : "s"} kept:{" "}
              {volumes.map((volume) => (
                <Badge key={volume} variant="outline" className="ml-1 font-mono text-xs">
                  {volume}
                </Badge>
              ))}
            </p>
          ) : (
            <p>No data volume is attached: all of this instance&apos;s data is on the root disk.</p>
          )}
        </div>
      </div>

      <div className="flex items-start gap-2">
        <Checkbox
          id={`${id}-ack`}
          checked={acknowledged}
          onCheckedChange={(checked) => onAcknowledgedChange(checked === true)}
          className="mt-0.5"
        />
        <Label htmlFor={`${id}-ack`} className="text-sm font-normal leading-snug">
          I understand that everything on the root disk of{" "}
          <span className="font-mono font-medium">{instanceName}</span> is erased and cannot be recovered.
        </Label>
      </div>

      <div className="space-y-1.5">
        <Label htmlFor={`${id}-name`}>
          Type <span className="font-mono">{instanceName}</span> to confirm
        </Label>
        <Input
          id={`${id}-name`}
          value={confirmName}
          onChange={(event) => onConfirmNameChange(event.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
      </div>
    </div>
  );
}
