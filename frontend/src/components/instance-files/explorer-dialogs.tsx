"use client";

import type { TreeChange, useFileActions } from "@/hooks/use-instance-files";
import { toast } from "@/hooks/use-toast";
import { fileErrorCode, type GuestEntry, type GuestTarget } from "@/lib/instance-files/api";
import { joinPath, normalizePath } from "@/lib/instance-files/path";
import { DeleteDialog } from "./delete-dialog";
import { NameDialog } from "./name-dialog";
import { PropertiesDialog } from "./properties-dialog";

export type Prompt =
  | { kind: "new-folder" } | { kind: "new-file" }
  | { kind: "rename"; entry: GuestEntry }
  | { kind: "move"; entries: GuestEntry[] }
  | { kind: "delete"; entries: GuestEntry[] }
  | { kind: "properties"; entry: GuestEntry };

interface ExplorerDialogsProps {
  prompt: Prompt | null;
  target: GuestTarget;
  folder: string;
  writeReason?: string;
  actions: ReturnType<typeof useFileActions>;
  onClose: () => void;
  onSelect: (names: string[]) => void;
}

/** Says what a recursive change could not do. A dialog that just closes would claim all of it. */
function reportTreeChange(outcomes: (TreeChange | undefined)[]): void {
  if (outcomes.some((outcome) => outcome?.unfinished)) {
    throw new Error("This folder is too large to finish in one go. Part of it changed and the rest did not.");
  }
  const skipped = Math.max(0, ...outcomes.map((outcome) => outcome?.skipped ?? 0));
  if (skipped > 0) {
    toast({
      title: "Some items were left as they were",
      description: `${skipped.toLocaleString()} could not be changed, for example a device, a pipe or an immutable file.`,
    });
  }
}

/** Every question the explorer asks, in one place, so its root stays about the list. */
export function ExplorerDialogs({ prompt, target, folder, writeReason, actions, onClose, onSelect }: ExplorerDialogsProps) {
  const removePath = async (entries: GuestEntry[], onProgress: (deleted: number) => void) => {
    let deleted = 0;
    const failed: string[] = [];
    let failedCount = 0;
    for (const entry of entries) {
      const before = deleted;
      let outcome: Awaited<ReturnType<typeof actions.remove>>;
      try {
        outcome = await actions.remove(
          joinPath(folder, entry.name),
          entry.type === "directory",
          (count) => onProgress(before + count),
        );
      } catch (error) {
        // Already gone, which is what was asked: a retry after a partial failure starts here.
        if (fileErrorCode(error) === "not_found") continue;
        throw error;
      }
      if (outcome) {
        deleted += outcome.deleted;
        failed.push(...outcome.failed);
        failedCount += outcome.failedCount;
      }
    }
    onSelect([]);
    return { failed, failedCount };
  };

  return (
    <>
      <NameDialog
        open={prompt?.kind === "new-folder"}
        title="New Folder"
        fieldLabel="Name"
        submitLabel="Create"
        initial=""
        onClose={onClose}
        onSubmit={async (name) => {
          await actions.createFolder(joinPath(folder, name));
          onSelect([name]);
        }}
      />
      <NameDialog
        open={prompt?.kind === "new-file"}
        title="New File"
        fieldLabel="Name"
        submitLabel="Create"
        initial=""
        onClose={onClose}
        onSubmit={async (name) => {
          await actions.createFile(joinPath(folder, name));
          onSelect([name]);
        }}
      />
      <NameDialog
        open={prompt?.kind === "rename"}
        title="Rename"
        fieldLabel="Name"
        submitLabel="Rename"
        initial={prompt?.kind === "rename" ? prompt.entry.name : ""}
        onClose={onClose}
        onSubmit={async (name) => {
          if (prompt?.kind !== "rename" || name === prompt.entry.name) return;
          await actions.rename(joinPath(folder, prompt.entry.name), joinPath(folder, name));
          onSelect([name]);
        }}
      />
      <NameDialog
        open={prompt?.kind === "move"}
        title="Move To"
        fieldLabel="Destination Folder"
        submitLabel="Move"
        initial={folder}
        validate={(value) => (value.trim() ? null : "Enter a folder path.")}
        onClose={onClose}
        onSubmit={async (value) => {
          if (prompt?.kind !== "move") return;
          const destination = normalizePath(value.trim(), folder);
          for (const entry of prompt.entries) {
            try {
              await actions.rename(joinPath(folder, entry.name), joinPath(destination, entry.name));
            } catch (error) {
              if (fileErrorCode(error) !== "not_found") throw error;
            }
          }
          onSelect([]);
        }}
      />
      {prompt?.kind === "delete" && (
        <DeleteDialog
          target={target}
          folder={folder}
          entries={prompt.entries}
          open
          onConfirm={(onProgress) => removePath(prompt.entries, onProgress)}
          onClose={onClose}
        />
      )}
      <PropertiesDialog
        entry={prompt?.kind === "properties" ? prompt.entry : null}
        path={prompt?.kind === "properties" ? joinPath(folder, prompt.entry.name) : ""}
        writeReason={writeReason}
        onClose={onClose}
        onSave={async ({ mode: nextMode, uid, gid, deepMode, deepOwner }) => {
          if (prompt?.kind !== "properties") return;
          const path = joinPath(folder, prompt.entry.name);
          reportTreeChange([
            nextMode !== undefined ? await actions.setMode(path, nextMode, deepMode) : undefined,
            uid !== undefined && gid !== undefined ? await actions.setOwner(path, uid, gid, deepOwner) : undefined,
          ]);
        }}
      />
    </>
  );
}
