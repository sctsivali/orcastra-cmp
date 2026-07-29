"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/hooks/use-toast";
import { useNodeNames } from "@/hooks/use-lxd";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import { Loader2, UploadCloud } from "lucide-react";

// 16 MiB chunks: large enough to keep request overhead low, small enough that a
// dropped connection only re-sends a little (the server tracks the real offset).
const CHUNK_SIZE = 16 * 1024 * 1024;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type Phase = "idle" | "uploading" | "importing" | "done" | "error";
type ImageType = "container" | "virtual-machine";

const TYPE_OPTIONS = [
  { value: "virtual-machine", label: "Virtual machine" },
  { value: "container", label: "Container" },
];

interface ImageUploadDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onUploaded?: () => void;
}

/** Derive a valid LXD image alias from a filename (strips common image extensions). */
function suggestAlias(fileName: string): string {
  const base = fileName.replace(
    /\.(tar\.(gz|xz|bz2)|tar|txz|tgz|squashfs|qcow2|img|raw)$/i,
    "",
  );
  const cleaned = base
    .replace(/[^A-Za-z0-9._/-]/g, "-")
    .replace(/^[^A-Za-z0-9]+/, "");
  return cleaned.slice(0, 255) || "custom-image";
}

export function ImageUploadDialog({ open, onOpenChange, onUploaded }: ImageUploadDialogProps) {
  const { toast } = useToast();
  const { data: nodeNames, isLoading: loadingNodes } = useNodeNames();
  const nodeOptions = (nodeNames || []).map((n) => ({ value: n, label: n }));

  const [node, setNode] = useState("");
  const [alias, setAlias] = useState("");
  const [imageType, setImageType] = useState<ImageType>("virtual-machine");
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState(0); // 0..100 (upload bytes)
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  // Tracks the live session so we can release its server-side slot on
  // cancel/error/teardown instead of leaving it for the idle sweep. Held in a
  // ref (not state) so the unmount/pagehide handlers see the latest value.
  const sessionIdRef = useRef<string | null>(null);

  const abortActive = () => {
    const sid = sessionIdRef.current;
    if (sid) {
      sessionIdRef.current = null;
      void api.imageUploads.abort(sid); // best-effort, never throws
    }
  };

  // If the tab is closed/reloaded or the component unmounts mid-upload, free the
  // slot. abort() uses keepalive so the DELETE survives an unload.
  useEffect(() => {
    const onUnload = () => abortActive();
    window.addEventListener("pagehide", onUnload);
    return () => {
      window.removeEventListener("pagehide", onUnload);
      abortActive();
    };
  }, []);

  const busy = phase === "uploading" || phase === "importing";
  const aliasValid = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/.test(alias);
  const canUpload =
    !!node && aliasValid && !!file && !busy && phase !== "done";

  const reset = () => {
    abortActive(); // release any session left behind by an error/cancel
    setNode("");
    setAlias("");
    setImageType("virtual-machine");
    setFile(null);
    setPhase("idle");
    setProgress(0);
    setError(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const handleFile = (f: File | null) => {
    setFile(f);
    if (f && !alias) setAlias(suggestAlias(f.name));
  };

  const startUpload = async () => {
    if (!file) return;
    setError(null);
    setPhase("uploading");
    setProgress(0);
    try {
      const { session_id } = await api.imageUploads.create({
        cluster_id: node,
        project: "default",
        alias,
        instance_type: imageType,
        total_bytes: file.size,
        max_bytes: file.size,
      });
      sessionIdRef.current = session_id;

      // Stream the file in chunks; the server returns the authoritative offset,
      // which lets a failed/retried chunk resume from the right place.
      let offset = 0;
      while (offset < file.size) {
        const end = Math.min(offset + CHUNK_SIZE, file.size);
        const chunk = file.slice(offset, end);
        offset = await api.imageUploads.patchChunk(session_id, offset, chunk);
        setProgress(Math.round((offset / file.size) * 100));
      }

      // Trigger the import and poll until the image lands (or fails).
      setPhase("importing");
      await api.imageUploads.finalize(session_id);
      for (;;) {
        const s = await api.imageUploads.status(session_id);
        if (s.status === "ready") break;
        if (s.status === "failed") {
          throw new Error(s.error || "Import failed on the cluster");
        }
        await sleep(2000);
      }

      sessionIdRef.current = null; // completed: nothing to release
      setPhase("done");
      toast({
        title: "Image uploaded",
        description: `"${alias}" is ready in the image store on ${node}.`,
      });
      onUploaded?.();
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
      abortActive(); // release the slot the failed upload was holding
    }
  };

  const statusLine = useMemo(() => {
    if (phase === "uploading") return `Uploading… ${progress}%`;
    if (phase === "importing") return "Importing into the image store…";
    if (phase === "done") return "Done.";
    return null;
  }, [phase, progress]);

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (busy) return; // don't let it close mid-upload
        if (!o) reset();
        onOpenChange(o);
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Upload image</DialogTitle>
          <DialogDescription>
            Upload a custom LXD image (a unified image tarball) into the cluster&apos;s
            image store under a private alias. It can then be used to create
            instances of the matching type.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label>Node *</Label>
              <SearchableSelect
                value={node}
                onValueChange={setNode}
                options={nodeOptions}
                placeholder={loadingNodes ? "Loading..." : "Select node"}
                searchPlaceholder="Search nodes..."
                emptyText="No nodes found."
                disabled={loadingNodes || busy}
              />
            </div>
            <div className="grid gap-2">
              <Label>Image type *</Label>
              <SearchableSelect
                value={imageType}
                onValueChange={(v) => setImageType(v as ImageType)}
                options={TYPE_OPTIONS}
                placeholder="Select type"
                searchPlaceholder="Search..."
                emptyText="No types."
                disabled={busy}
              />
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="image-alias">Alias *</Label>
            <Input
              id="image-alias"
              value={alias}
              onChange={(e) => setAlias(e.target.value)}
              placeholder="windows-11 or ubuntu/22.04"
              disabled={busy}
              aria-invalid={!!alias && !aliasValid}
            />
            {alias && !aliasValid && (
              <p className="text-xs text-destructive">
                Letters, numbers, dot, hyphen, underscore and slash only; must
                start with a letter or number.
              </p>
            )}
          </div>

          <div className="grid gap-2">
            <Label htmlFor="image-file">Image file *</Label>
            <Input
              id="image-file"
              ref={fileInputRef}
              type="file"
              accept=".tar.gz,.tar.xz,.tar,.tgz,.txz,.squashfs,.qcow2,.img,application/gzip,application/x-xz,application/octet-stream"
              onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
              disabled={busy}
            />
            {file && (
              <p className="text-xs text-muted-foreground">
                {file.name} · {formatBytes(file.size)}
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              The image&apos;s real type is determined by the file; set the type
              above to match it.
            </p>
          </div>

          {(busy || phase === "done") && (
            <div className="space-y-1">
              <Progress
                value={phase === "importing" || phase === "done" ? 100 : progress}
              />
              <p className="text-xs text-muted-foreground">{statusLine}</p>
            </div>
          )}

          {error && (
            <div className="p-3 text-sm bg-destructive/10 border border-destructive/30 text-destructive rounded-lg">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          {phase === "done" ? (
            <Button
              onClick={() => {
                reset();
                onOpenChange(false);
              }}
            >
              Close
            </Button>
          ) : (
            <>
              <Button
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={busy}
              >
                Cancel
              </Button>
              <Button onClick={startUpload} disabled={!canUpload}>
                {busy ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <UploadCloud className="h-4 w-4 mr-2" />
                )}
                Upload
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
