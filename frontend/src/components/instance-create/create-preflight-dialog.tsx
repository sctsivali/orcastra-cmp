"use client";

import { AlertTriangle, Loader2 } from "lucide-react";

import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import type { InstanceCreatePreflightResponse } from "@/types/api";

interface CreatePreflightDialogProps {
  open: boolean;
  preflight: InstanceCreatePreflightResponse | null;
  busy: boolean;
  disabledReason?: string;
  onCancel: () => void;
  onConfirm: () => void;
}

export function CreatePreflightDialog({
  open,
  preflight,
  busy,
  disabledReason,
  onCancel,
  onConfirm,
}: CreatePreflightDialogProps) {
  if (!preflight) return null;
  const networkless = preflight.required_acknowledgements.includes("networkless");

  return (
    <AlertDialog open={open} onOpenChange={(next) => {
      if (!next && !busy) onCancel();
    }}>
      <AlertDialogContent className="min-w-0">
        <AlertDialogHeader>
          <AlertDialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-5 w-5" aria-hidden="true" />
            {networkless ? "Create without network access?" : "Review create warnings"}
          </AlertDialogTitle>
          <AlertDialogDescription>
            {networkless
              ? "No network interface remains after profiles and instance devices are combined. The instance will be isolated unless networking is added later."
              : "The server found warnings that need confirmation before creating this instance."}
          </AlertDialogDescription>
        </AlertDialogHeader>

        <div className="space-y-3 text-sm">
          {preflight.warnings.map((warning) => (
            <p key={warning.code}>{warning.message}</p>
          ))}
          <dl className="grid min-w-0 grid-cols-1 gap-x-3 gap-y-1 rounded-md border p-3 sm:grid-cols-[7rem_minmax(0,1fr)]">
            <dt className="text-muted-foreground">Profiles</dt>
            <dd className="min-w-0 break-words">{preflight.effective.profiles.join(", ") || "None"}</dd>
            <dt className="text-muted-foreground">Interfaces</dt>
            <dd className="min-w-0 break-words">{preflight.effective.nic_names.join(", ") || "None"}</dd>
            <dt className="text-muted-foreground">Root device</dt>
            <dd className="min-w-0 break-all">{preflight.effective.root_device || "Not resolved"}</dd>
          </dl>
          {disabledReason && <p className="text-sm text-muted-foreground">{disabledReason}</p>}
        </div>

        <AlertDialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={busy}>
            Back to configuration
          </Button>
          <Button onClick={onConfirm} disabled={busy || !!disabledReason} title={disabledReason}>
            {busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {networkless ? "Create isolated instance" : "Accept and create"}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
