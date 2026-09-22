/**
 * File Upload Dialog - Upload files to instances from the terminal window.
 * Supports both file picker and drag-and-drop. Shows upload progress.
 */

"use client";

import * as React from "react";
import { useState, useCallback, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Upload,
  File as FileIcon,
  X,
  CheckCircle2,
  AlertCircle,
  Loader2,
  FolderUp,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn, formatBytes } from "@/lib/utils";
import { toast } from "@/hooks/use-toast";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { isAuthActorSynchronized } from "@/lib/api";
import { fileTransferAPI } from "@/lib/file-transfer-api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { OverflowText } from "@/components/ui/overflow-text";

interface FileUploadDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  instanceName: string;
  host: string;
  project: string;
  /** Files pre-selected from drag-and-drop */
  initialFiles?: File[];
}

type UploadStatus = "idle" | "uploading" | "success" | "error";

interface FileUploadEntry {
  file: File;
  destPath: string;
  status: UploadStatus;
  progress: number;
  error?: string;
}

const MAX_FILE_SIZE = 100 * 1000 * 1000; // 100 MB (decimal file-size limit)

export function FileUploadDialog({
  open,
  onOpenChange,
  instanceName,
  host,
  project,
  initialFiles,
}: FileUploadDialogProps) {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  const [entries, setEntries] = useState<FileUploadEntry[]>([]);
  const [destDir, setDestDir] = useState("/root/");
  const [isUploading, setIsUploading] = useState(false);
  const [isDragOver, setIsDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const initialized = useRef(false);

  // Initialize with drag-dropped files
  React.useEffect(() => {
    if (open && initialFiles && initialFiles.length > 0 && !initialized.current) {
      initialized.current = true;
      const newEntries: FileUploadEntry[] = initialFiles.map((file) => ({
        file,
        destPath: `/root/${file.name}`,
        status: "idle" as UploadStatus,
        progress: 0,
      }));
      setEntries(newEntries);
    }
    if (!open) {
      initialized.current = false;
    }
  }, [open, initialFiles]);

  // Reset state when dialog closes
  React.useEffect(() => {
    if (!open) {
      setEntries([]);
      setIsUploading(false);
      setIsDragOver(false);
    }
  }, [open]);

  // Add files from file picker or drop
  const addFiles = useCallback(
    (files: FileList | File[]) => {
      const newEntries: FileUploadEntry[] = [];
      for (const file of Array.from(files)) {
        if (file.size > MAX_FILE_SIZE) {
          toast({
            title: "File too large",
            description: `"${file.name}" exceeds the 100 MB limit.`,
            variant: "destructive",
          });
          continue;
        }
        // avoid duplicates
        const alreadyAdded = entries.some((e) => e.file.name === file.name && e.file.size === file.size);
        if (alreadyAdded) continue;

        newEntries.push({
          file,
          destPath: `${destDir.replace(/\/$/, "")}/${file.name}`,
          status: "idle",
          progress: 0,
        });
      }
      setEntries((prev) => [...prev, ...newEntries]);
    },
    [entries, destDir]
  );

  // Handle file input change
  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (e.target.files) {
        addFiles(e.target.files);
        // Reset the input so the same file can be selected again
        e.target.value = "";
      }
    },
    [addFiles]
  );

  // Drag and drop handlers for the drop zone inside dialog
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(false);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDragOver(false);
      if (e.dataTransfer.files.length > 0) {
        addFiles(e.dataTransfer.files);
      }
    },
    [addFiles]
  );

  // Remove a file entry
  const removeEntry = useCallback((index: number) => {
    setEntries((prev) => prev.filter((_, i) => i !== index));
  }, []);

  // Update destination path for an entry
  const updateDestPath = useCallback((index: number, path: string) => {
    setEntries((prev) =>
      prev.map((entry, i) => (i === index ? { ...entry, destPath: path } : entry))
    );
  }, []);

  // Update all dest paths when destDir changes
  const handleDestDirChange = useCallback(
    (newDir: string) => {
      setDestDir(newDir);
      setEntries((prev) =>
        prev.map((entry) => ({
          ...entry,
          destPath: `${newDir.replace(/\/$/, "")}/${entry.file.name}`,
        }))
      );
    },
    []
  );

  // Upload all files sequentially
  const uploadAll = useCallback(async () => {
    if (entries.length === 0) return;

    setIsUploading(true);
    const actorScope = scopeKey;
    let successCount = 0;
    let failCount = 0;

    for (let i = 0; i < entries.length; i++) {
      const entry = entries[i];
      if (entry.status === "success") {
        successCount++;
        continue;
      }

      // Mark as uploading
      setEntries((prev) =>
        prev.map((e, idx) => (idx === i ? { ...e, status: "uploading" as UploadStatus, progress: 0 } : e))
      );

      try {
        await fileTransferAPI.push(entry.file, {
          host,
          instanceName,
          project,
          destinationPath: entry.destPath,
          mode: "0644",
          uid: 0,
          gid: 0,
        });
        if (!isAuthActorSynchronized(actorScope)) return;

        setEntries((prev) =>
          prev.map((e, idx) =>
            idx === i ? { ...e, status: "success" as UploadStatus, progress: 100 } : e
          )
        );
        successCount++;
      } catch (err) {
        if (!isAuthActorSynchronized(actorScope)) return;
        const errorMsg = err instanceof Error ? err.message : "Unknown error";
        setEntries((prev) =>
          prev.map((e, idx) =>
            idx === i ? { ...e, status: "error" as UploadStatus, error: errorMsg } : e
          )
        );
        failCount++;
      }
    }

    if (successCount > 0 && isAuthActorSynchronized(actorScope)) {
      await reconcileMutationImpact(queryClient, "file-transfer.push");
      if (!isAuthActorSynchronized(actorScope)) return;
    }

    setIsUploading(false);

    if (failCount === 0) {
      toast({
        title: "Upload complete",
        description: `${successCount} file(s) uploaded to ${instanceName}`,
      });
      // Auto-close after brief delay
      setTimeout(() => onOpenChange(false), 800);
    } else {
      toast({
        title: "Upload finished with errors",
        description: `${successCount} succeeded, ${failCount} failed`,
        variant: "destructive",
      });
    }
  }, [entries, host, instanceName, onOpenChange, project, queryClient, scopeKey]);

  const pendingCount = entries.filter((e) => e.status === "idle" || e.status === "error").length;

  return (
    <Dialog open={open} onOpenChange={isUploading ? undefined : onOpenChange}>
      <DialogContent className="sm:max-w-lg bg-terminal border-terminal-border text-gray-200 space-y-4">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-gray-100">
            <Upload className="h-5 w-5 text-blue-400" />
            Upload Files to {instanceName}
          </DialogTitle>
          <DialogDescription className="text-gray-400">
            Upload files to the container/VM. Files will be pushed to the specified path inside the instance.
          </DialogDescription>
        </DialogHeader>

        {/* Destination directory */}
        <div className="space-y-2">
          <Label className="text-gray-300 text-xs">Destination Directory</Label>
          <div className="flex items-center gap-2">
            <FolderUp className="h-4 w-4 text-gray-500 shrink-0" />
            <Input
              value={destDir}
              onChange={(e) => handleDestDirChange(e.target.value)}
              className="bg-terminal-deep border-terminal-border text-gray-200 text-sm h-8"
              placeholder="/root/"
              disabled={isUploading}
            />
          </div>
        </div>

        {/* Drop zone */}
        <div
          className={cn(
            "border-2 border-dashed rounded-lg p-6 text-center transition-colors cursor-pointer",
            isDragOver
              ? "border-blue-400 bg-blue-500/10"
              : "border-terminal-border hover:border-gray-500",
            isUploading && "pointer-events-none opacity-50"
          )}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          onClick={() => !isUploading && fileInputRef.current?.click()}
        >
          <Upload className="h-8 w-8 text-gray-500 mx-auto mb-2" />
          <p className="text-sm text-gray-400">
            Drag & drop files here or <span className="text-blue-400 underline">browse</span>
          </p>
          <p className="text-xs text-gray-500 mt-1">Max 100 MB per file</p>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={handleFileSelect}
          />
        </div>

        {/* File list */}
        {entries.length > 0 && (
          <div className="max-h-48 overflow-y-auto space-y-2">
            {entries.map((entry, idx) => (
              <div
                key={`${entry.file.name}-${idx}`}
                className="flex items-center gap-2 px-3 py-2 rounded bg-terminal-deep border border-terminal-border"
              >
                {/* Status icon */}
                {entry.status === "uploading" && (
                  <Loader2 className="h-4 w-4 text-blue-400 animate-spin shrink-0" />
                )}
                {entry.status === "success" && (
                  <CheckCircle2 className="h-4 w-4 text-green-400 shrink-0" />
                )}
                {entry.status === "error" && (
                  <AlertCircle className="h-4 w-4 text-red-400 shrink-0" />
                )}
                {entry.status === "idle" && (
                  <FileIcon className="h-4 w-4 text-gray-500 shrink-0" />
                )}

                {/* File info */}
                <div className="flex-1 min-w-0">
                  <p className="text-sm text-gray-200">
                    <OverflowText
                      value={entry.file.name}
                      label={`name of the file queued for ${entry.destPath}`}
                      tone="chrome"
                    />
                  </p>
                  <p className="text-xs text-gray-500">
                    {formatBytes(entry.file.size)} → {entry.destPath}
                  </p>
                  {entry.error && (
                    <p className="text-xs text-red-400 mt-0.5">{entry.error}</p>
                  )}
                </div>

                {/* Remove button (only when not uploading) */}
                {!isUploading && entry.status !== "success" && (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 text-gray-500 hover:text-red-400"
                    onClick={(e) => {
                      e.stopPropagation();
                      removeEntry(idx);
                    }}
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                )}
              </div>
            ))}
          </div>
        )}

        <DialogFooter className="gap-2 pt-2 sm:space-x-2">
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={isUploading}
            size="sm"
            className="h-8 px-3 text-gray-400 hover:text-gray-200"
          >
            Cancel
          </Button>
          <Button
            onClick={uploadAll}
            disabled={isUploading || pendingCount === 0}
            size="sm"
            className="h-8 min-w-[84px] px-3 bg-blue-600 hover:bg-blue-700 text-white"
          >
            {isUploading ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Uploading...
              </>
            ) : (
              <>Upload {pendingCount > 0 ? `(${pendingCount})` : ""}</>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
