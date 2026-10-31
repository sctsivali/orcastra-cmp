"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DialogSubmitError } from "@/components/ui/dialog-submit-error";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/hooks/use-toast";
import { useStoragePools } from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { api, isAuthActorSynchronized, onAuthActorChanged } from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import {
  followUploadToTerminal,
  UploadOutcomeUnknown,
  UploadTerminalFailure,
} from "@/lib/upload-terminal";
import { APIError } from "@/types/api";
import { formatBytes } from "@/lib/utils";
import { Loader2, UploadCloud } from "lucide-react";

// 16 MiB chunks: large enough to keep request overhead low, small enough that a
// dropped connection only re-sends a little (the server tracks the real offset).
const CHUNK_SIZE = 16 * 1024 * 1024;

type Phase = "idle" | "uploading" | "importing" | "done" | "failed" | "unknown";

interface ActiveUploadSession {
  id: string;
  actorScope: string;
  abortable: boolean;
}

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
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { scopeKey } = useActorQueryScope();
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
  const sessionRef = useRef<ActiveUploadSession | null>(null);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const runInFlightRef = useRef(false);
  const actorEpochRef = useRef(0);
  const contextKey = `${scopeKey}\u0000${node}\u0000${pool}\u0000${volumeName}`;
  const activeContextRef = useRef<{ key: string }>({ key: contextKey });
  if (activeContextRef.current.key !== contextKey) {
    activeContextRef.current = { key: contextKey };
  }

  const abortActive = useCallback(() => {
    const session = sessionRef.current;
    sessionRef.current = null;
    if (
      session?.abortable &&
      isAuthActorSynchronized(session.actorScope)
    ) {
      void api.isoUploads.abort(session.id, {
        actorScope: session.actorScope,
        keepalive: true,
      });
    }
  }, []);

  const clearFormState = useCallback(() => {
    setNode("");
    setPool("");
    setVolumeName("");
    setFile(null);
    setPhase("idle");
    setProgress(0);
    setError(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, []);

  // If the tab is closed/reloaded or the component unmounts mid-upload, free the
  // slot. abort() uses keepalive so the DELETE survives an unload.
  useEffect(() => {
    const onUnload = () => abortActive();
    const stopActorListener = onAuthActorChanged(() => {
      actorEpochRef.current += 1;
      uploadAbortRef.current?.abort(
        new DOMException("Authenticated actor changed", "AbortError"),
      );
      uploadAbortRef.current = null;
      sessionRef.current = null;
      runInFlightRef.current = false;
      clearFormState();
    });
    window.addEventListener("pagehide", onUnload);
    return () => {
      stopActorListener();
      window.removeEventListener("pagehide", onUnload);
      uploadAbortRef.current?.abort();
      abortActive();
    };
  }, [abortActive, clearFormState]);

  const { data: poolsResp } = useStoragePools(node || undefined, { enabled: !!node });
  const poolOptions = ((poolsResp as any)?.pools || [])
    .filter((p: any) => (p.node ? p.node === node : true))
    .map((p: any) => ({ value: p.name, label: p.name }));

  const busy = phase === "uploading" || phase === "importing";
  const nameValid = /^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/.test(volumeName);
  const canUpload =
    !!node && !!pool && nameValid && !!file && !busy && phase !== "done";

  const reset = () => {
    uploadAbortRef.current?.abort();
    uploadAbortRef.current = null;
    abortActive(); // release a session only when finalization has not started
    runInFlightRef.current = false;
    clearFormState();
  };

  const handleFile = (f: File | null) => {
    setFile(f);
    if (f && !volumeName) setVolumeName(suggestName(f.name));
  };

  const startUpload = async () => {
    if (!file || runInFlightRef.current) return;
    runInFlightRef.current = true;
    const requestContext = activeContextRef.current;
    const requestActorEpoch = actorEpochRef.current;
    const requestScope = scopeKey;
    const targetNode = node;
    const targetPool = pool;
    const targetVolumeName = volumeName;
    const uploadFile = file;
    const controller = new AbortController();
    uploadAbortRef.current?.abort();
    uploadAbortRef.current = controller;
    const isCurrentRequest = () =>
      activeContextRef.current === requestContext &&
      actorEpochRef.current === requestActorEpoch &&
      isAuthActorSynchronized(requestScope);
    const reconcileOutcome = async (): Promise<boolean> => {
      if (!isCurrentRequest()) return false;
      await Promise.allSettled([
        reconcileMutationImpact(queryClient, "iso.upload"),
      ]);
      return isCurrentRequest();
    };
    setError(null);
    setPhase("uploading");
    setProgress(0);
    let finalizationAttempted = false;
    let finalizationDefinitivelyRejected = false;
    try {
      const { session_id } = await api.isoUploads.create({
        cluster_id: targetNode,
        project: "default",
        pool: targetPool,
        volume_name: targetVolumeName,
        total_bytes: uploadFile.size,
        max_bytes: uploadFile.size,
      }, { signal: controller.signal, actorScope: requestScope });
      if (!isCurrentRequest()) return;
      sessionRef.current = { id: session_id, actorScope: requestScope, abortable: true };

      // Stream the file in chunks; the server returns the authoritative offset,
      // which lets a failed/retried chunk resume from the right place.
      let offset = 0;
      while (offset < uploadFile.size) {
        const end = Math.min(offset + CHUNK_SIZE, uploadFile.size);
        const chunk = uploadFile.slice(offset, end);
        offset = await api.isoUploads.patchChunk(session_id, offset, chunk, {
          signal: controller.signal,
          actorScope: requestScope,
        });
        if (!isCurrentRequest()) return;
        setProgress(Math.round((offset / uploadFile.size) * 100));
      }

      // Finalize is non-idempotent. Attempt it once, then follow only the durable status read.
      setPhase("importing");
      finalizationAttempted = true;
      if (sessionRef.current?.id === session_id) {
        sessionRef.current.abortable = false;
      }
      const terminalStartedAt = Date.now();
      let initialStatus;
      try {
        initialStatus = await api.isoUploads.finalize(session_id, {
          signal: controller.signal,
          actorScope: requestScope,
        });
      } catch (error) {
        if (!isCurrentRequest() || controller.signal.aborted) throw error;
        finalizationDefinitivelyRejected =
          error instanceof APIError && error.status >= 400 && error.status < 500;
        if (finalizationDefinitivelyRejected) throw error;
        // A lost/5xx response can hide an accepted import. Never replay finalize.
      }
      await followUploadToTerminal({
        signal: controller.signal,
        initialStatus,
        startedAt: terminalStartedAt,
        readStatus: (signal) =>
          api.isoUploads.status(session_id, { signal, actorScope: requestScope }),
        unknownMessage:
          "Orcastra could not confirm the final ISO-import state within 30 minutes. Check ISO Images and Operations before trying again.",
      });
      if (!isCurrentRequest()) return;

      sessionRef.current = null; // terminal: nothing to release
      // Mark create-form volume and capacity caches stale only after the import is terminal-ready.
      if (!(await reconcileOutcome())) return;
      setPhase("done");
      toast({
        title: "ISO uploaded",
        description: `"${targetVolumeName}" is ready in pool "${targetPool}" on ${targetNode}.`,
      });
      onUploaded?.({
        node: targetNode,
        project: "default",
        pool: targetPool,
        name: targetVolumeName,
      });
    } catch (e: unknown) {
      if (!isCurrentRequest() || controller.signal.aborted) return;
      if (finalizationAttempted) {
        sessionRef.current = null;
        if (!(await reconcileOutcome())) return;
      } else {
        abortActive();
      }
      const outcomeUnknown =
        finalizationAttempted &&
        !finalizationDefinitivelyRejected &&
        !(e instanceof UploadTerminalFailure);
      setPhase(outcomeUnknown ? "unknown" : "failed");
      setError(
        outcomeUnknown
          ? e instanceof UploadOutcomeUnknown
            ? e.message
            : "Orcastra could not confirm the final ISO-import state. Check ISO Images and Operations before trying again."
          : e instanceof Error
            ? e.message
            : String(e),
      );
    } finally {
      if (uploadAbortRef.current === controller) uploadAbortRef.current = null;
      if (activeContextRef.current === requestContext) runInFlightRef.current = false;
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

        <div className="space-y-4 pt-2">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label>Cluster *</Label>
              <SearchableSelect
                value={node}
                onValueChange={(v) => {
                  setNode(v);
                  setPool("");
                }}
                options={nodeOptions}
                placeholder={loadingNodes ? "Loading…" : "Select a cluster"}
                searchPlaceholder="Search clusters…"
                emptyText="No clusters found."
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
              <Label htmlFor="iso-pool">Storage Pool *</Label>
              <SearchableSelect
                id="iso-pool"
                value={pool}
                onValueChange={setPool}
                options={poolOptions}
                placeholder={node ? "Select pool" : "Select a cluster first"}
                searchPlaceholder="Search pools…"
                emptyText="No pools found."
                disabled={!node || busy}
              />
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="iso-name">Volume Name *</Label>
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
            <Label htmlFor="iso-file">ISO File *</Label>
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

        </div>

        <DialogSubmitError message={error} />

        <DialogFooter className="pt-4">
          {phase === "done" || phase === "unknown" ? (
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
