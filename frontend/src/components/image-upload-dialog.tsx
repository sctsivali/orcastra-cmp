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
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { useProjects } from "@/hooks/use-lxd";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import {
  api,
  imageAPI,
  isAuthActorSynchronized,
  onAuthActorChanged,
} from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import {
  followUploadToTerminal,
  UploadOutcomeUnknown,
  UploadTerminalFailure,
} from "@/lib/upload-terminal";
import { APIError, type ProjectListResponse } from "@/types/api";
import { formatBytes } from "@/lib/utils";
import { Loader2, UploadCloud } from "lucide-react";

// 16 MiB chunks: large enough to keep request overhead low, small enough that a
// dropped connection only re-sends a little (the server tracks the real offset).
const CHUNK_SIZE = 16 * 1024 * 1024;

type Phase = "idle" | "uploading" | "importing" | "done" | "failed" | "unknown";
type ImageType = "container" | "virtual-machine";

interface ActiveUploadSession {
  id: string;
  actorScope: string;
  abortable: boolean;
}

const TYPE_OPTIONS = [
  { value: "virtual-machine", label: "Virtual Machine" },
  { value: "container", label: "Container" },
];

interface ImageUploadDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * The image as the cluster reports it once the import landed. `project` is the store that
   * holds it: the chosen project's own when it keeps one, otherwise the host's default store.
   */
  onUploaded?: (image: { node: string; project: string; fingerprint: string | null; alias: string }) => void;
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
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { scopeKey } = useActorQueryScope();
  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: loadingNodes } = nodePicker;

  const [node, setNode] = useState("");
  const [project, setProject] = useState("default");
  const [alias, setAlias] = useState("");
  // The projects this node offers. Built from a successful read only, and the default
  // project is always offered because every node has one.
  const { data: projectsResp, isLoading: loadingProjects, isError: projectsFailed } = useProjects(node);
  const projectOptions = Array.from(
    new Set([
      "default",
      ...(projectsFailed ? [] : ((projectsResp as ProjectListResponse | undefined)?.projects ?? []).map((p) => p.name)),
    ]),
  ).map((name) => ({ value: name, label: name }));
  const selectNode = (next: string) => {
    setNode(next);
    // A project belongs to one node, so the pick cannot carry across.
    setProject("default");
  };
  const [imageType, setImageType] = useState<ImageType>("virtual-machine");
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState(0); // 0..100 (upload bytes)
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  // The live session, so cancel, error or teardown releases its server-side slot
  // instead of leaving it to the idle sweep. A ref rather than state, so the
  // unmount and pagehide handlers see the latest value.
  const sessionRef = useRef<ActiveUploadSession | null>(null);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const runInFlightRef = useRef(false);
  const actorEpochRef = useRef(0);
  const contextKey = `${scopeKey}\u0000${node}\u0000${project}\u0000${alias}\u0000${imageType}`;
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
      void api.imageUploads.abort(session.id, {
        actorScope: session.actorScope,
        keepalive: true,
      });
    }
  }, []);

  const clearFormState = useCallback(() => {
    setNode("");
    setProject("default");
    setAlias("");
    setImageType("virtual-machine");
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
      // important: the new actor's bearer must never delete the previous actor's upload.
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

  const busy = phase === "uploading" || phase === "importing";
  const aliasValid = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/.test(alias);
  const canUpload =
    !!node && aliasValid && !!file && !busy && phase !== "done";

  const reset = () => {
    uploadAbortRef.current?.abort();
    uploadAbortRef.current = null;
    abortActive(); // Releases the session only if finalization has not started.
    runInFlightRef.current = false;
    clearFormState();
  };

  const handleFile = (f: File | null) => {
    setFile(f);
    if (f && !alias) setAlias(suggestAlias(f.name));
  };

  const startUpload = async () => {
    if (!file || runInFlightRef.current) return;
    runInFlightRef.current = true;
    const requestContext = activeContextRef.current;
    const requestActorEpoch = actorEpochRef.current;
    const requestScope = scopeKey;
    const targetNode = node;
    const targetProject = project;
    const targetAlias = alias;
    const targetImageType = imageType;
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
        reconcileMutationImpact(queryClient, "image.upload"),
      ]);
      return isCurrentRequest();
    };
    setError(null);
    setPhase("uploading");
    setProgress(0);
    let finalizationAttempted = false;
    let finalizationDefinitivelyRejected = false;
    try {
      const { session_id } = await api.imageUploads.create({
        cluster_id: targetNode,
        project: targetProject,
        alias: targetAlias,
        instance_type: targetImageType,
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
        offset = await api.imageUploads.patchChunk(session_id, offset, chunk, {
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
        initialStatus = await api.imageUploads.finalize(session_id, {
          signal: controller.signal,
          actorScope: requestScope,
        });
      } catch (error) {
        if (!isCurrentRequest() || controller.signal.aborted) throw error;
        finalizationDefinitivelyRejected =
          error instanceof APIError && error.status >= 400 && error.status < 500;
        if (finalizationDefinitivelyRejected) throw error;
        // A lost/5xx response can hide an accepted import. Never replay finalize; status is the
        // only safe way to determine whether the first request took effect.
      }
      const terminal = await followUploadToTerminal({
        signal: controller.signal,
        initialStatus,
        startedAt: terminalStartedAt,
        readStatus: (signal) =>
          api.imageUploads.status(session_id, { signal, actorScope: requestScope }),
        unknownMessage:
          "Orcastra could not confirm the final image-import state within 30 minutes. Check Images and Operations before trying again.",
      });
      if (!isCurrentRequest()) return;

      sessionRef.current = null; // Terminal: nothing left to release.
      // The store that holds it. The upload names a project, but a project without its own
      // image store lands in the host's default store, and the list read says which.
      const store = await imageAPI
        .listByHost(targetNode, targetProject, { fresh: true, signal: controller.signal })
        .then((listing) => listing.project || targetProject)
        .catch(() => targetProject);
      if (!isCurrentRequest()) return;
      // Finalize only starts the import. Terminal ready is the point at which every image
      // consumer must become stale; the owning page performs its focused refetch below.
      if (!(await reconcileOutcome())) return;
      setPhase("done");
      toast({
        title: "Image uploaded",
        description:
          store === "default"
            ? `"${targetAlias}" is ready in the default image store on ${targetNode}.`
            : `"${targetAlias}" is ready in project ${store}'s image store on ${targetNode}.`,
      });
      onUploaded?.({
        node: targetNode,
        project: store,
        fingerprint: terminal.fingerprint,
        alias: targetAlias,
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
            : "Orcastra could not confirm the final image-import state. Check Images and Operations before trying again."
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
    if (phase === "importing") return "Importing into the image store…";
    if (phase === "done") return "Done.";
    return null;
  }, [phase, progress]);

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (busy) return; // Stays open mid-upload.
        if (!o) reset();
        onOpenChange(o);
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Upload Image</DialogTitle>
          <DialogDescription>
            Upload a custom LXD image (a unified image tarball) into the cluster&apos;s
            image store under a private alias. It can then be used to create
            instances of the matching type.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 pt-2">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label>Cluster *</Label>
              <SearchableSelect
                value={node}
                onValueChange={selectNode}
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
              <Label>Image Type *</Label>
              <SearchableSelect
                value={imageType}
                onValueChange={(v) => setImageType(v as ImageType)}
                options={TYPE_OPTIONS}
                placeholder="Select type"
                searchPlaceholder="Search…"
                emptyText="No types."
                disabled={busy}
              />
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="image-upload-project">Project</Label>
            <SearchableSelect
              id="image-upload-project"
              value={project}
              onValueChange={setProject}
              options={projectOptions}
              placeholder={loadingProjects ? "Loading…" : "Select project"}
              searchPlaceholder="Search projects…"
              emptyText="No projects found."
              disabled={!node || loadingProjects || busy}
            />
            <p className="text-xs text-muted-foreground">
              A project with its own image store keeps the image there; any other project
              shares the default store on the cluster.
            </p>
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
            <Label htmlFor="image-file">Image File *</Label>
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
