import { Pencil, Trash2 } from "lucide-react";

import type { RowAction } from "@/components/ui/row-actions";
import type { ProfileInfo } from "@/types/api";

interface ProfileActionsInput {
  profile: ProfileInfo;
  isReadOnly: boolean;
  onEdit: () => void;
  onDelete: () => void;
}

/** The row menu of a profile: edit always, delete with the reason it is unavailable. */
export function profileActions({ profile, isReadOnly, onEdit, onDelete }: ProfileActionsInput): RowAction[] {
  const used = profile.used_by?.length ?? 0;
  return [
    {
      id: "edit",
      label: "Edit Profile",
      icon: Pencil,
      onSelect: onEdit,
      unavailableReason: isReadOnly ? "Disabled in read-only mode" : undefined,
    },
    {
      id: "delete",
      label: "Delete Profile",
      icon: Trash2,
      destructive: true,
      separatorBefore: true,
      onSelect: onDelete,
      unavailableReason: isReadOnly
        ? "Disabled in read-only mode"
        : profile.name === "default"
          ? "LXD keeps the default profile in every project"
          : used > 0
            ? `In use by ${used} instance${used === 1 ? "" : "s"}`
            : undefined,
    },
  ];
}
