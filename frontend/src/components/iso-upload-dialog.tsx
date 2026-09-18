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
import { useStoragePools } from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import { Loader2, UploadCloud } from "lucide-react";

// 16 MiB chunks: large enough to keep request overhead low, small enough that a
// dropped connection only re-sends a little (the server tracks the real offset).
const CHUNK_SIZE = 16 * 1024 * 1024;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type Phase = "idle" | "uploading" | "importing" | "done" | "error";

interface IsoUploadDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The volume as it was written, so the list can bring the new row into view. */
  onUploaded?: (volume: { node: string; project: string; pool: string; name: string }) => void;
}

/** Derive a valid LXD custom-volume name from a filename. */
function suggestName(fileName: string): string {
  const base = fileName.replace(/\.iso$/i, "");
  const cleaned = base.replace(/[^A-Za-z0-9._-]/g, "-").replace(/^[^A-Za-z0-9]+/, "");
  return cleaned.slice(0, 63) || "custom-iso";
}

export function IsoUploadDialog({ open, onOpenChange, onUploaded }: IsoUploadDialogProps) {
  const { toast } = useToast();
  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: loadingNodes } = nodePicker;

  const [node, setNode] = useState("");
  const [pool, setPool] = useState("");
  const [volumeName, setVolumeName] = useState("");
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
      void api.isoUploads.abort(sid); // best-effort, never throws
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

  const { data: poolsResp } = useStoragePools(node || undefined, { enabled: !!node });
  const poolOptions = ((poolsResp as any)?.pools || [])
    .filter((p: any) => (p.node ? p.node === node : true))
    .map((p: any) => ({ value: p.name, label: p.name }));

  const busy = phase === "uploading" || phase === "importing";
  const nameValid = /^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/.test(volumeName);
  const canUpload =
    !!node && !!pool && nameValid && !!file && !busy && phase !== "done";

  const reset = () => {
    abortActive(); // release any session left behind by an error/cancel
    setNode("");
    setPool("");
    setVolumeName("");
    setFile(null);
    setPhase("idle");
    setProgress(0);
    setError(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const handleFile = (f: File | null) => {
    setFile(f);
    if (f && !volumeName) setVolumeName(suggestName(f.name));
  };

  const startUpload = async () => {
    if (!file) return;
    setError(null);
    setPhase("uploading");
    setProgress(0);
    try {
      const { session_id } = await api.isoUploads.create({
        cluster_id: node,
        project: "default",
        pool,
        volume_name: volumeName,
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
        offset = await api.isoUploads.patchChunk(session_id, offset, chunk);
        setProgress(Math.round((offset / file.size) * 100));
      }

      // Trigger the import and poll until the volume lands (or fails).
      setPhase("importing");
      await api.isoUploads.finalize(session_id);
      for (;;) {
        const s = await api.isoUploads.status(session_id);
        if (s.status === "ready") break;
        if (s.status === "failed") {
          throw new Error(s.error || "Import failed on the cluster");
        }
        await sleep(2000);
      }

      sessionIdRef.current = null; // completed: nothing to release
      setPhase("done");
      toast({
        title: "ISO uploaded",
        description: `"${volumeName}" is ready in pool "${pool}" on ${node}.`,
      });
      onUploaded?.({ node, project: "default", pool, name: volumeName });
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
      abortActive(); // release the slot the failed upload was holding
    }
  };

  const statusLine = useMemo(() => {
    if (phase === "uploading") return `Uploading… ${progress}%`;
    if (phase === "importing") return "Importing into the storage pool…";
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
          <DialogTitle>Upload ISO</DialogTitle>
          <DialogDescription>
            Upload a custom ISO as an LXD storage volume (content-type iso). It
            can then be attached to a VM as boot media.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label>Node *</Label>
              <SearchableSelect
                value={node}
                onValueChange={(v) => {
                  setNode(v);
                  setPool("");
                }}
                options={nodeOptions}
                placeholder={loadingNodes ? "Loading..." : "Select node"}
                searchPlaceholder="Search nodes..."
                emptyText="No nodes found."
                disabled={loadingNodes || busy}
              />
              <NodeStatusNotice
                meta={node ? nodePicker.statusOf(node) : undefined}
                reason={node ? nodePicker.availabilityOf(node)?.reason ?? undefined : undefined}
                nodeLabel={node ? nodePicker.labelOf(node) : undefined}
                unavailable={nodePicker.statusUnavailable}
                noSelectableNodes={nodePicker.selectableCount === 0}
                updatedAt={nodePicker.statusUpdatedAt}
                onRecheck={nodePicker.refreshNow}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="iso-pool">Storage pool *</Label>
              <SearchableSelect
                id="iso-pool"
                value={pool}
                onValueChange={setPool}
                options={poolOptions}
                placeholder={node ? "Select pool" : "Select a node first"}
                searchPlaceholder="Search pools..."
                emptyText="No pools found."
                disabled={!node || busy}
              />
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="iso-name">Volume name *</Label>
            <Input
              id="iso-name"
              value={volumeName}
              onChange={(e) => setVolumeName(e.target.value)}
              placeholder="ubuntu-24-04-iso"
              disabled={busy}
              aria-invalid={!!volumeName && !nameValid}
            />
            {volumeName && !nameValid && (
              <p className="text-xs text-destructive">
                Letters, numbers, dot, hyphen and underscore only; must start
                with a letter or number.
              </p>
            )}
          </div>

          <div className="grid gap-2">
            <Label htmlFor="iso-file">ISO file *</Label>
            <Input
              id="iso-file"
              ref={fileInputRef}
              type="file"
              accept=".iso,application/x-iso9660-image,application/octet-stream"
              onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
              disabled={busy}
            />
            {file && (
              <p className="text-xs text-muted-foreground">
                {file.name} · {formatBytes(file.size)}
              </p>
            )}
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
