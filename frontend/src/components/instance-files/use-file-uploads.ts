"use client";

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { toast } from "@/hooks/use-toast";
import { instanceFilesAPI, type GuestTarget } from "@/lib/instance-files/api";
import { treeFromDrop, type PickedTree } from "@/lib/instance-files/drop-entries";
import { directoryKey } from "@/lib/instance-files/keys";
import { joinPath, keepBothName } from "@/lib/instance-files/path";
import { enqueueUploads } from "@/lib/instance-files/transfer-pump";
import { formatBytes } from "@/lib/utils";
import type { ConflictChoice } from "./conflict-dialog";
import { isDirectoryLike } from "./file-actions";

interface UploadOptions {
  target: GuestTarget;
  folder: string;
  active: boolean;
  writeReason?: string;
}

/** The folder row under a drag. Read from the event: a highlight set on an earlier one can be stale. */
function folderRowAt(event: React.DragEvent): string | null {
  const node = event.target as Node;
  const element = node instanceof Element ? node : node.parentElement;
  return element?.closest<HTMLElement>("tr[data-drop-folder]")?.dataset.dropFolder ?? null;
}

/** Getting files into the queue: pickers, drag and drop, and the questions asked before a byte moves. */
export function useFileUploads({ target, folder, active, writeReason }: UploadOptions) {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  const [dragging, setDragging] = React.useState(false);
  const [dropTarget, setDropTarget] = React.useState<string | null>(null);
  const [pending, setPending] = React.useState<{ tree: PickedTree; folder: string; conflicts: string[]; taken: string[] } | null>(null);
  const dragDepth = React.useRef(0);

  const begin = React.useCallback(
    async (tree: PickedTree, into: string) => {
      if (tree.files.length === 0 && tree.folders.length === 0) return;
      const total = tree.files.reduce((sum, item) => sum + item.file.size, 0);
      try {
        const current = await queryClient.fetchQuery({
          queryKey: directoryKey(scopeKey, target, into),
          queryFn: ({ signal }) => instanceFilesAPI.list(target, into, signal),
          staleTime: 2_000,
        });
        if (current.free_bytes !== null && total > current.free_bytes) {
          toast({
            title: "Not enough free space",
            description: `This upload needs ${formatBytes(total)} and the guest has ${formatBytes(current.free_bytes)} free.`,
            variant: "destructive",
          });
          return;
        }
        const existing = new Map(current.entries.map((entry) => [entry.name, entry]));
        const conflicts = tree.files
          .map((item) => item.relativePath)
          .filter((relative) => {
            const entry = existing.get(relative);
            return entry !== undefined && !isDirectoryLike(entry);
          });
        if (conflicts.length) {
          setPending({ tree, folder: into, conflicts, taken: [...existing.keys()] });
          return;
        }
        enqueueUploads(target, into, tree, "refuse");
      } catch (error) {
        toast({
          title: "Could not start upload",
          description: error instanceof Error ? error.message : "The folder could not be read.",
          variant: "destructive",
        });
      }
    },
    [queryClient, scopeKey, target],
  );

  const applyConflicts = (choices: ReadonlyMap<string, ConflictChoice>) => {
    if (!pending) return;
    const { tree, folder: into, taken } = pending;
    setPending(null);
    const names = new Set(taken);
    const rename = new Map<string, string>();
    const replacing: PickedTree = { files: [], folders: [] };
    const others: PickedTree = { files: [], folders: tree.folders };
    for (const item of tree.files) {
      const choice = choices.get(item.relativePath);
      if (choice === "skip") continue;
      if (choice === "replace") replacing.files.push(item);
      else {
        if (choice === "keep") {
          const name = keepBothName(item.relativePath, names);
          names.add(name);
          rename.set(item.relativePath, name);
        }
        others.files.push(item);
      }
    }
    if (others.files.length || others.folders.length) enqueueUploads(target, into, others, "refuse", rename);
    if (replacing.files.length) enqueueUploads(target, into, replacing, "replace");
  };

  React.useEffect(() => {
    if (!active) return;
    // A file dropped beside a target must not navigate the tab away from every open console.
    const hold = (event: DragEvent) => {
      if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
    };
    window.addEventListener("dragover", hold);
    window.addEventListener("drop", hold);
    return () => {
      window.removeEventListener("dragover", hold);
      window.removeEventListener("drop", hold);
    };
  }, [active]);

  const handleDrop = async (event: React.DragEvent) => {
    event.preventDefault();
    event.stopPropagation();
    dragDepth.current = 0;
    setDragging(false);
    const row = folderRowAt(event);
    const into = row ? joinPath(folder, row) : folder;
    setDropTarget(null);
    if (writeReason) {
      toast({ title: "Upload not available", description: writeReason, variant: "destructive" });
      return;
    }
    try {
      await begin(await treeFromDrop(event.dataTransfer.items), into);
    } catch {
      toast({ title: "Could not start upload", description: "What was dropped could not be read.", variant: "destructive" });
    }
  };

  const dropHandlers = {
    onDragEnter: (event: React.DragEvent) => {
      if (!event.dataTransfer.types.includes("Files")) return;
      dragDepth.current += 1;
      setDragging(true);
    },
    onDragLeave: () => {
      dragDepth.current = Math.max(0, dragDepth.current - 1);
      if (dragDepth.current === 0) {
        setDragging(false);
        setDropTarget(null);
      }
    },
    onDragOver: (event: React.DragEvent) => {
      if (!event.dataTransfer.types.includes("Files")) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = writeReason ? "none" : "copy";
      setDropTarget(folderRowAt(event));
    },
    onDrop: (event: React.DragEvent) => void handleDrop(event),
  };

  return {
    begin,
    dragging,
    dropTarget,
    pending,
    applyConflicts,
    cancelPending: () => setPending(null),
    dropHandlers,
  };
}
