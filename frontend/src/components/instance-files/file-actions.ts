import {
  Download,
  Eye,
  FilePen,
  FolderInput,
  FolderOpen,
  Pencil,
  ClipboardCopy,
  ShieldCheck,
  Trash2,
  type LucideIcon,
} from "lucide-react";
import type { GuestEntry } from "@/lib/instance-files/api";
import { isKernelView, isProtectedPath, joinPath } from "@/lib/instance-files/path";

export type FileActionId =
  | "open" | "preview" | "edit" | "download" | "rename" | "move" | "copy-path" | "properties" | "delete";

export interface FileAction {
  id: FileActionId;
  label: string;
  icon: LucideIcon;
  destructive?: boolean;
  separatorBefore?: boolean;
  unavailableReason?: string;
}

const IMAGE_NAME = /\.(png|jpe?g|gif|webp)$/i;

export function isDirectoryLike(entry: GuestEntry): boolean {
  return entry.type === "directory" || (entry.type === "symlink" && entry.target_type === "directory");
}

export function isFileLike(entry: GuestEntry): boolean {
  return entry.type === "file" || (entry.type === "symlink" && entry.target_type === "file");
}

export function isImageName(name: string): boolean {
  return IMAGE_NAME.test(name);
}

/** One list for the row menu and the right-click menu, so they cannot drift apart. */
export function actionsFor(folder: string, selected: GuestEntry[], writeReason: string | undefined): FileAction[] {
  const single = selected.length === 1 ? selected[0] : undefined;
  const paths = selected.map((entry) => joinPath(folder, entry.name));
  const protectedReason = paths.some(isProtectedPath) ? "System paths cannot be changed from here" : undefined;
  const actions: FileAction[] = [];
  if (single && isDirectoryLike(single)) actions.push({ id: "open", label: "Open", icon: FolderOpen });
  if (single && isFileLike(single)) {
    if (isImageName(single.name)) actions.push({ id: "preview", label: "Preview", icon: Eye });
    actions.push({ id: "edit", label: writeReason ? "View" : "Open as text", icon: writeReason ? Eye : FilePen });
  }
  const unreadable = selected.some((entry) => !isFileLike(entry) && !isDirectoryLike(entry));
  actions.push({
    id: "download",
    label: selected.length > 1 || (single && isDirectoryLike(single)) ? "Download as archive" : "Download",
    icon: Download,
    unavailableReason:
      unreadable && selected.length === 1
        ? "Only files and folders can be downloaded"
        : paths.some(isKernelView) && !(single && isFileLike(single))
          ? "System paths cannot be archived"
          : undefined,
  });
  if (single) {
    actions.push({
      id: "rename",
      label: "Rename",
      icon: Pencil,
      separatorBefore: true,
      unavailableReason: writeReason ?? protectedReason,
    });
  }
  actions.push({
    id: "move",
    label: "Move To",
    icon: FolderInput,
    separatorBefore: !single,
    unavailableReason: writeReason ?? protectedReason,
  });
  if (single) {
    actions.push({ id: "copy-path", label: "Copy Path", icon: ClipboardCopy });
    actions.push({ id: "properties", label: "Properties", icon: ShieldCheck });
  }
  actions.push({
    id: "delete",
    label: "Delete",
    icon: Trash2,
    destructive: true,
    separatorBefore: true,
    unavailableReason: writeReason ?? protectedReason,
  });
  return actions;
}
