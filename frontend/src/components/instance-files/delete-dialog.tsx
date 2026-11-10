"use client";

import { formatCount } from "@/lib/utils";
import * as React from "react";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { instanceFilesAPI, type GuestEntry, type GuestTarget, type SelectionSummary } from "@/lib/instance-files/api";
import { guestText } from "@/lib/instance-files/guest-text";
import { isSystemPath, joinPath } from "@/lib/instance-files/path";

interface DeleteDialogProps {
  target: GuestTarget;
  folder: string;
  entries: GuestEntry[];
  open: boolean;
  onConfirm: (onProgress: (deleted: number) => void) => Promise<{ failed: string[]; failedCount: number }>;
  onClose: () => void;
}

/**
 * Confirmation in three weights: a file, a counted folder, and a folder that is a system path or
 * could not be counted, which needs its path typed. Folders are counted first so the prompt says
 * what will go.
 */
export function DeleteDialog({ target, folder, entries, open, onConfirm, onClose }: DeleteDialogProps) {
  const [summary, setSummary] = React.useState<SelectionSummary | null>(null);
  const [counting, setCounting] = React.useState(false);
  const [pending, setPending] = React.useState(false);
  const [deleted, setDeleted] = React.useState(0);
  const [error, setError] = React.useState<string | null>(null);
  const inFlight = React.useRef(false);

  const paths = React.useMemo(() => entries.map((entry) => joinPath(folder, entry.name)), [entries, folder]);
  const folders = entries.filter((entry) => entry.type === "directory").length;

  React.useEffect(() => {
    if (!open) return;
    setSummary(null);
    setError(null);
    setDeleted(0);
    if (folders === 0) return;
    const controller = new AbortController();
    setCounting(true);
    instanceFilesAPI
      .summarize(target, paths, controller.signal)
      .then(setSummary, () => setSummary(null))
      .finally(() => setCounting(false));
    return () => controller.abort();
  }, [folders, open, paths, target]);

  const hasLink = entries.some((entry) => entry.type === "symlink");
  const unknownSize = folders > 0 && !counting && !summary?.complete;
  const typed = folders > 0 && (paths.some(isSystemPath) || unknownSize);

  let description = entries.length === 1 ? `${guestText(entries[0].name)} will be deleted. This cannot be undone.` : "This cannot be undone.";
  if (folders > 0 && summary) {
    const total = summary.files + summary.folders;
    description = `${summary.complete ? "" : "At least "}${formatCount(total)} ${total === 1 ? "item" : "items"} will be deleted. This cannot be undone.`;
  } else if (unknownSize) {
    description = "This folder could not be counted, so what it holds is not known. This cannot be undone.";
  }
  if (hasLink) description += " Deleting a link leaves its target in place.";
  if (paths.some(isSystemPath)) description += " This can stop the instance from booting.";

  return (
    <DestructiveConfirmDialog
      open={open}
      onOpenChange={(next) => !next && !pending && onClose()}
      title={entries.length === 1 ? "Delete Item" : "Delete Items"}
      description={description}
      confirmLabel={entries.length === 1 ? "Delete" : `Delete ${entries.length} items`}
      pendingLabel={deleted > 0 ? `Deleted ${formatCount(deleted)}` : "Deleting"}
      pending={pending}
      preparing={counting}
      preparingLabel="Counting what this holds"
      dependents={
        entries.length > 1
          ? { summary: `Selected from ${guestText(folder)}`, names: entries.map((entry) => guestText(entry.name)) }
          : undefined
      }
      confirmPhrase={typed ? { prompt: <>Type <span className="font-mono">{guestText(paths[0])}</span> to confirm</>, value: paths[0] } : undefined}
      error={error}
      onConfirm={() => {
        if (inFlight.current) return;
        inFlight.current = true;
        setPending(true);
        setError(null);
        onConfirm(setDeleted)
          .then(({ failed, failedCount }) => {
            if (failedCount > 0) {
              setError(`${failedCount} could not be deleted, for example ${guestText(failed[0] ?? "")}. They may be immutable or in use.`);
            } else {
              onClose();
            }
          })
          .catch((failure: unknown) => setError(failure instanceof Error ? failure.message : "This could not be deleted."))
          .finally(() => {
            inFlight.current = false;
            setPending(false);
          });
      }}
    />
  );
}
