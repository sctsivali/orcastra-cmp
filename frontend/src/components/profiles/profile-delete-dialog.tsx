"use client";

import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { referencesFromUsedBy } from "@/lib/used-by";
import type { ProfileInfo } from "@/types/api";

interface ProfileDeleteDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The row being deleted. Held one render longer than the real target, so the body does not blank. */
  profile: ProfileInfo | null;
  pending: boolean;
  error: string | null;
  onConfirm: () => void;
}

/** Confirm deleting one profile; a profile still in use lists the instances that hold it. */
export function ProfileDeleteDialog({ open, onOpenChange, profile, pending, error, onConfirm }: ProfileDeleteDialogProps) {
  const names = profile
    ? referencesFromUsedBy(profile.used_by)
        .map((ref) => ref.instance || ref.name || ref.raw)
        .filter((n): n is string => !!n)
    : [];
  return (
    <DestructiveConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Delete ${profile?.name ?? "profile"}?`}
      description={
        profile
          ? `This removes the profile from ${profile.node}${profile.project && profile.project !== "default" ? ` in project ${profile.project}` : ""}. It cannot be undone.`
          : ""
      }
      confirmLabel="Delete profile"
      pendingLabel="Deleting…"
      pending={pending}
      onConfirm={() => onConfirm()}
      dependents={
        names.length
          ? { summary: `${names.length} instance${names.length === 1 ? "" : "s"} still use this profile; LXD will refuse the delete.`, names }
          : undefined
      }
      error={error}
    />
  );
}
