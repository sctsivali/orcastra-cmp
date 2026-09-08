"use client";

import { Loader2 } from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  FORCE_OFF_HINT,
  FORCE_ON_HINT,
  FORCE_OPTION_LABEL,
} from "@/lib/instance-action-copy";
import { VMInfo } from "@/types/api";

export interface ConfirmActionState {
  open: boolean;
  // null before the first action is chosen, which is the page's initial state.
  action: "stop" | "restart" | null;
  name: string;
  host: string;
  /** A name is unique per project, not per host, so the action carries the row's project. */
  project: string;
  force: boolean;
}

interface InstanceActionDialogsProps {
  deleteTarget: VMInfo | null;
  /** Held one render longer than deleteTarget so the dialog does not blank out while closing. */
  deleteDisplay: VMInfo | null;
  deletePending: boolean;
  onDeleteOpenChange: (open: boolean) => void;
  onDeleteConfirm: () => void;
  confirmAction: ConfirmActionState;
  onConfirmActionChange: (next: ConfirmActionState) => void;
  onConfirmActionRun: () => void;
}

/**
 * The two confirm-and-mutate dialogs for the instances table.
 *
 * Extracted from the page because confirm-and-mutate is one responsibility and it does not
 * belong in the same file as the live-streamed table. Behaviour is unchanged, including the
 * preventDefault that keeps the delete dialog open for the length of the request.
 */
export function InstanceActionDialogs({
  deleteTarget,
  deleteDisplay,
  deletePending,
  onDeleteOpenChange,
  onDeleteConfirm,
  confirmAction,
  onConfirmActionChange,
  onConfirmActionRun,
}: InstanceActionDialogsProps) {
  return (
    <>
      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={onDeleteOpenChange}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Delete Instance &quot;{deleteDisplay?.name}&quot;?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This permanently deletes the instance and its data from{" "}
              {deleteDisplay?.host}.
              {deleteDisplay?.status === "Running" &&
                " The instance is currently running and will be force-stopped first."}
              {deleteDisplay?.status === "Frozen" &&
                " The instance is frozen by billing policy and will be force-stopped first."}{" "}
              This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deletePending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              // preventDefault blocks Radix's built-in close so the dialog stays
              // open (and populated, with the spinner) for the full 10-30s that a
              // force-stop + delete takes. The finally in handleDeleteConfirm
              // closes it once the request resolves.
              onClick={(e) => {
                e.preventDefault();
                onDeleteConfirm();
              }}
              disabled={deletePending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deletePending && (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              )}
              {deletePending ? "Deleting…" : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Stop / Restart confirmation, with a Force (power-cycle) option. */}
      <AlertDialog
        open={confirmAction.open}
        onOpenChange={(open) => onConfirmActionChange({ ...confirmAction, open })}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmAction.action === "stop" ? "Stop" : "Restart"} &quot;{confirmAction.name}&quot;?
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmAction.action === "stop"
                ? "This will shut down the instance."
                : "This will restart the instance."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {/* A graceful stop asks the guest OS to shut down over ACPI. A VM with
              no OS installed, or one sitting at a boot menu/UEFI shell, has
              nothing listening and will just time out. Force is the only way to
              power-cycle those. Copy lives in lib/instance-action-copy.ts, which
              two other dialogs share. */}
          <label className="flex items-start gap-2 rounded-md border p-3 cursor-pointer">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={confirmAction.force}
              onChange={(e) =>
                onConfirmActionChange({ ...confirmAction, force: e.target.checked })
              }
            />
            <span className="text-sm">
              <span className="font-medium">{FORCE_OPTION_LABEL}</span>
              <span className="block text-xs text-muted-foreground mt-0.5">
                {confirmAction.force ? FORCE_ON_HINT : FORCE_OFF_HINT}
              </span>
            </span>
          </label>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={onConfirmActionRun}>
              {confirmAction.force ? "Force " : ""}
              {confirmAction.action === "stop" ? "Stop" : "Restart"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
