"use client";

import { useEffect, useState } from "react";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { useToast } from "@/hooks/use-toast";
import { useDeleteImage } from "@/hooks/use-lxd";
import { imageAPI } from "@/lib/api";
import type { ImageInfo } from "@/types/api";

export type ImageRow = ImageInfo & { node: string };

// What to call a row when a control has to name it. An alias is what an operator typed, the
// description is what the registry wrote, and the fingerprint is the last resort rather than the
// first. Never a slice of it: a name ending in three dots names nothing.
export function imageLabel(image: { aliases?: string[]; description?: string; fingerprint: string }): string {
  return image.aliases?.find(Boolean) || image.description || image.fingerprint;
}

/** Where a row lives, for a sentence: the store on the node. */
export function imageStoreLabel(image: { node: string; project: string }): string {
  return image.project === "default"
    ? `the default image store on ${image.node}`
    : `project ${image.project}'s image store on ${image.node}`;
}

interface ImageDeleteDialogProps {
  target: ImageRow | null;
  onClose: () => void;
  onDeleted: () => void;
  /** What this page can see was built from the image, the fallback when the cluster does not answer. */
  localDependents: (fingerprint: string) => string[];
}

/**
 * The delete preflight and confirm for one image row. The row is keyed by node, store and
 * fingerprint, because the same fingerprint can sit in several stores and each is deleted
 * on its own.
 */
export function ImageDeleteDialog({ target, onClose, onDeleted, localDependents }: ImageDeleteDialogProps) {
  const { toast } = useToast();
  const deleteImage = useDeleteImage();
  // Held one render longer than target so the dialog keeps its heading through Radix's
  // exit fade instead of blanking to "Delete image ?".
  const [display, setDisplay] = useState<ImageRow | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Dependents the backend knew about and this page could not see. The instances list is
  // tenant-filtered server side, so a local count of zero is not proof of zero. `hidden` is
  // how many sit in projects outside the caller's bound: counted, never named.
  const [serverDependents, setServerDependents] = useState<{ names: string[]; hidden: number } | null>(null);
  // null while the preflight is in flight, so the confirm can block until it answers.
  const preflightPending = target !== null && serverDependents === null;

  useEffect(() => {
    if (!target) return;
    setDisplay(target);
    setError(null);
    // Ask the cluster rather than trusting the page. The local list is built from a
    // tenant-filtered read, so zero locally is not zero on the cluster, and the operator
    // should see that before committing rather than after being refused.
    setServerDependents(null);
    let cancelled = false;
    imageAPI
      .usage(target.fingerprint, target.node, target.project)
      .then((usage) => {
        if (!cancelled) setServerDependents({ names: usage.dependents, hidden: usage.hidden_dependents ?? 0 });
      })
      .catch(() => {
        // A failed preflight must not block the delete: the backend refuses on its own if
        // anything depends on the image. Fall back to what this page can see.
        if (!cancelled) setServerDependents({ names: localDependents(target.fingerprint), hidden: 0 });
      });
    return () => {
      cancelled = true;
    };
    // localDependents is a stable accessor over the page's memo; re-running on it would
    // restart the preflight on every instance poll.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  const close = () => {
    setError(null);
    setServerDependents(null);
    onClose();
  };

  const dependents = serverDependents?.names ?? [];
  const hidden = serverDependents?.hidden ?? 0;
  const dependentCount = dependents.length + hidden;

  const confirm = async () => {
    if (!target) return;
    setError(null);
    try {
      await deleteImage.mutateAsync({
        fingerprint: target.fingerprint,
        host: target.node,
        project: target.project,
        // Only ever forced when the operator was shown the dependents and ticked through.
        force: dependentCount > 0,
      });
      toast({
        title: "Image deleted",
        description: `${imageLabel(target)} removed from ${imageStoreLabel(target)}.`,
      });
      close();
      onDeleted();
    } catch (err) {
      // Stay open and say why. Closing on failure loses both the message and the row it
      // was about.
      setError(err instanceof Error ? err.message : "Could not delete the image");
    }
  };

  return (
    <DestructiveConfirmDialog
      open={!!target}
      onOpenChange={(next) => {
        if (!next) close();
      }}
      title={`Delete image ${display ? imageLabel(display) : ""}?`}
      description={
        <>
          This removes the image from {display ? imageStoreLabel(display) : ""}. Instances already
          built from it keep running, but it can no longer be used to launch or refresh.
          {display?.cached
            ? " This one was pulled from a remote, so it can be fetched again."
            : " This one was uploaded here, so it cannot be recovered from a remote."}
          {display?.public
            ? " It is a public image, so anything else using this store loses it too."
            : ""}
        </>
      }
      dependents={
        dependentCount > 0
          ? {
              summary:
                `${dependentCount} instance(s) on ${display?.node} were created from this image` +
                (hidden > 0 ? ` (${hidden} in projects you cannot see)` : ""),
              names: dependents,
            }
          : undefined
      }
      acknowledgeLabel={
        dependentCount > 0
          ? "I understand these instances were built from this image and will keep running without it."
          : undefined
      }
      confirmLabel="Delete image"
      pendingLabel="Deleting"
      pending={deleteImage.isPending}
      preparing={preflightPending}
      preparingLabel="Checking what was built from this image."
      error={error}
      onConfirm={confirm}
    />
  );
}
