"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/hooks/use-toast";
import { useByteFormat } from "@/hooks/use-byte-format";
import { useImagesByHost, useRemoteImages } from "@/hooks/use-lxd";
import { useClusterType } from "@/hooks/use-node-picker-options";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import {
  api,
  isAuthActorSynchronized,
  type RebuildPreflight,
  type RebuildRequest,
  type RebuildStatus,
} from "@/lib/api";
import { reconcileInstanceFleet } from "@/lib/instance-reconciliation";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { APIError, type ImageInfo, type RemoteImageInfo } from "@/types/api";
import { plural } from "@/lib/utils";
import {
  Loader2,
  RefreshCw,
  AlertTriangle,
  Disc3,
  ShieldCheck,
  CheckCircle2,
  XCircle,
} from "lucide-react";

interface RebuildDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  instance: { name: string; host: string; project?: string };
  onDone?: () => void;
}

type Mode = "image" | "empty" | "cloud-init";
type Phase = "loading" | "error" | "form" | "running" | "done" | "failed" | "unknown";

const REBUILD_POLL_MS = 2_000;
const REBUILD_MAX_POLL_MS = 30 * 60_000;
const REBUILD_MAX_TRANSIENT_FAILURES = 4;

class RebuildTerminalFailure extends Error {}
class RebuildOutcomeUnknown extends Error {}

function waitForRebuildPoll(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(finish, ms);
    const onAbort = () => {
      cleanup();
      reject(signal.reason ?? new DOMException("Cancelled", "AbortError"));
    };
    const cleanup = () => {
      clearTimeout(timeout);
      signal.removeEventListener("abort", onAbort);
    };
    function finish() {
      cleanup();
      resolve();
    }
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  });
}

// Coarse progress for the running phase, keyed by the job's server-side status.
const PROGRESS: Record<RebuildStatus, number> = {
  pending: 8,
  stopping: 25,
  rebuilding: 55,
  starting: 75,
  provisioning: 90,
  ready: 100,
  failed: 100,
};
const STATUS_LABEL: Record<RebuildStatus, string> = {
  pending: "Pending…",
  stopping: "Stopping the instance…",
  rebuilding: "Reinstalling the root disk…",
  starting: "Starting the instance…",
  provisioning: "Waiting for provisioning…",
  ready: "Done",
  failed: "Failed",
};

export function RebuildDialog({ open, onOpenChange, instance, onDone }: RebuildDialogProps) {
  const { toast } = useToast();
  const { formatBytes } = useByteFormat();
  const { scopeKey } = useActorQueryScope();
  const queryClient = useQueryClient();
  const contextKey = `${scopeKey}\u0000${instance.host}\u0000${instance.project || "default"}\u0000${instance.name}`;
  const activeContextRef = useRef<{ key: string }>({ key: contextKey });
  if (activeContextRef.current.key !== contextKey) {
    activeContextRef.current = { key: contextKey };
  }
  const pollAbortRef = useRef<AbortController | null>(null);
  const runInFlightRef = useRef(false);

  const [phase, setPhase] = useState<Phase>("loading");
  const [preflight, setPreflight] = useState<RebuildPreflight | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [mode, setMode] = useState<Mode>("image");
  const [selectedImageKey, setSelectedImageKey] = useState<string>("");
  const [cloudInit, setCloudInit] = useState<string>("");
  const [runProvisioning, setRunProvisioning] = useState(true);
  const [confirmName, setConfirmName] = useState("");

  const [jobStatus, setJobStatus] = useState<RebuildStatus>("pending");
  const [runError, setRunError] = useState<string | null>(null);
  const [remoteImageIntent, setRemoteImageIntent] = useState(false);

  // Load the read-only preflight when the dialog opens.
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setPhase("loading");
    setPreflight(null);
    setLoadError(null);
    setMode("image");
    setSelectedImageKey("");
    setCloudInit("");
    setRunProvisioning(true);
    setConfirmName("");
    setJobStatus("pending");
    setRunError(null);
    setRemoteImageIntent(false);

    api.instances
      .rebuildPreflight(
        instance.name,
        instance.host,
        instance.project || "default",
        { signal: controller.signal },
      )
      .then((pf) => {
        if (controller.signal.aborted) return;
        setPreflight(pf);
        setPhase("form");
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        setLoadError(e instanceof Error ? e.message : String(e));
        setPhase("error");
      });
    return () => {
      controller.abort();
    };
  }, [open, scopeKey, instance.name, instance.host, instance.project]);

  useEffect(
    () => () => {
      pollAbortRef.current?.abort();
      pollAbortRef.current = null;
      runInFlightRef.current = false;
    },
    [contextKey],
  );

  // Image catalog, local and remote, filtered to the instance type.
  const wantType = preflight?.type; // "container" | "virtual-machine"
  // The store the instance's project reads, not the host's default store: a rebuild resolves a
  // local fingerprint the same way a create does, inside the target project.
  const {
    data: localResp,
    isLoading: loadingLocal,
    isError: localImagesFailed,
    refetch: refetchLocalImages,
  } = useImagesByHost(
    open ? instance.host : "",
    instance.project || "default",
  );
  const hostEngine = useClusterType(open ? instance.host : undefined);
  const {
    data: remoteResp,
    isLoading: loadingRemote,
    isError: remoteImagesFailed,
    refetch: refetchRemoteImages,
  } = useRemoteImages({ enabled: open && remoteImageIntent, engine: hostEngine });

  const imageRows = useMemo(() => {
    const local = ((localResp?.images ?? []) as ImageInfo[]).map((img) => ({
      key: `local:${img.fingerprint}`,
      value: img.fingerprint,
      server: "",
      type: img.type,
      label:
        (img.aliases?.find(Boolean) || img.description || img.fingerprint) +
        (img.size ? ` · ${formatBytes(img.size)}` : "") +
        " · Local",
    }));
    const remote = ((remoteResp?.images ?? []) as RemoteImageInfo[]).map((img) => ({
      key: `remote:${img.server}:${img.alias}:${img.type}:${img.architecture}`,
      value: img.alias,
      server: img.server,
      type: img.type,
      label:
        `${img.distribution} ${img.release}`.trim() +
        (img.variant && img.variant !== "default" ? ` (${img.variant})` : "") +
        ` · ${img.source}`,
    }));
    return [...local, ...remote].filter((r) => !wantType || r.type === wantType);
  }, [formatBytes, localResp, remoteResp, wantType]);

  const imageOptions = useMemo(
    () => imageRows.map((r) => ({ value: r.key, label: r.label })),
    [imageRows],
  );
  const selectedRow = imageRows.find((r) => r.key === selectedImageKey);

  const snapshots = preflight?.snapshots ?? [];
  const blocked = snapshots.length > 0;
  const volumes = preflight?.attached_volumes ?? [];
  const needsImage = mode === "image" || mode === "cloud-init";
  const nameMatches = confirmName.trim() === instance.name;
  const imageOk = !needsImage || !!selectedRow;
  const cloudInitOk = mode !== "cloud-init" || !!cloudInit.trim();
  const canSubmit = phase === "form" && !blocked && nameMatches && imageOk && cloudInitOk;

  const startRebuild = async () => {
    if (runInFlightRef.current) return;
    runInFlightRef.current = true;
    const requestContext = activeContextRef.current;
    const requestScope = scopeKey;
    const isCurrentRequest = () =>
      activeContextRef.current === requestContext && isAuthActorSynchronized(requestScope);
    const pollController = new AbortController();
    pollAbortRef.current?.abort();
    pollAbortRef.current = pollController;
    const reconcileAcceptedOutcome = async (): Promise<boolean> => {
      await Promise.allSettled([
        reconcileMutationImpact(queryClient, "instance.rebuild"),
      ]);
      if (!isCurrentRequest()) return false;
      await Promise.allSettled([
        reconcileInstanceFleet(queryClient, requestScope, { fresh: true }),
      ]);
      return isCurrentRequest();
    };
    const body: RebuildRequest = {
      host: instance.host,
      project: instance.project || "default",
      mode,
      confirm_name: confirmName.trim(),
      run_provisioning: runProvisioning,
    };
    if (needsImage && selectedRow) {
      body.image = selectedRow.value;
      if (selectedRow.server) {
        body.image_server = selectedRow.server;
        body.image_protocol = "simplestreams";
      }
    }
    if (mode === "cloud-init") body.cloud_init = cloudInit;

    setPhase("running");
    setJobStatus("pending");
    setRunError(null);
    let accepted = false;
    try {
      const job = await api.instances.rebuild(instance.name, body);
      accepted = true;
      if (!isCurrentRequest()) return;
      const startedAt = Date.now();
      let status = job.status;
      let terminalError = job.error;
      let transientFailures = 0;
      setJobStatus(status);
      while (status !== "ready" && status !== "failed") {
        if (Date.now() - startedAt >= REBUILD_MAX_POLL_MS) {
          throw new RebuildOutcomeUnknown(
            "Orcastra could not confirm the rebuild within 30 minutes. Check Operations and refresh the instance before trying again.",
          );
        }
        await waitForRebuildPoll(REBUILD_POLL_MS, pollController.signal);
        let j;
        try {
          j = await api.instances.getRebuildJob(instance.name, job.job_id, {
            fresh: true,
            signal: pollController.signal,
          });
        } catch (error) {
          if (!isCurrentRequest() || pollController.signal.aborted) throw error;
          if (error instanceof APIError && error.status === 429) {
            const remainingMs = Math.max(
              0,
              REBUILD_MAX_POLL_MS - (Date.now() - startedAt),
            );
            await waitForRebuildPoll(
              Math.min(
                remainingMs,
                Math.max(REBUILD_POLL_MS, (error.retryAfter ?? 30) * 1_000),
              ),
              pollController.signal,
            );
            continue;
          }
          if (error instanceof APIError && [401, 403, 404].includes(error.status)) {
            throw new RebuildOutcomeUnknown(
              "Orcastra can no longer read this rebuild job. Check Operations and refresh the instance before trying again.",
            );
          }
          transientFailures += 1;
          if (transientFailures >= REBUILD_MAX_TRANSIENT_FAILURES) {
            throw new RebuildOutcomeUnknown(
              "Orcastra could not confirm the rebuild after repeated status-read failures. Check Operations and refresh the instance before trying again.",
            );
          }
          continue;
        }
        if (!isCurrentRequest()) return;
        transientFailures = 0;
        status = j.status;
        terminalError = j.error;
        setJobStatus(status);
      }
      if (status === "failed") {
        throw new RebuildTerminalFailure(terminalError || "Rebuild failed");
      }
      if (!(await reconcileAcceptedOutcome())) return;
      setPhase("done");
      toast({
        title: "Instance rebuilt",
        description:
          mode === "empty"
            ? `"${instance.name}" now has a blank root disk. Open the console to attach an ISO and install.`
            : `"${instance.name}" was rebuilt successfully.`,
      });
      onDone?.();
    } catch (e: unknown) {
      if (!isCurrentRequest() || pollController.signal.aborted) return;
      if (accepted) {
        // A failed or unreadable terminal job can still have changed the root disk or power
        // state. Reconcile actual state, and never replay the destructive rebuild automatically.
        if (!(await reconcileAcceptedOutcome())) return;
      }
      const outcomeUnknown = accepted && !(e instanceof RebuildTerminalFailure);
      setPhase(outcomeUnknown ? "unknown" : "failed");
      setRunError(
        outcomeUnknown
          ? e instanceof RebuildOutcomeUnknown
            ? e.message
            : "Orcastra could not confirm the final rebuild state. Check Operations and refresh the instance before trying again."
          : e instanceof Error
            ? e.message
            : String(e),
      );
    } finally {
      if (pollAbortRef.current === pollController) pollAbortRef.current = null;
      if (activeContextRef.current === requestContext) runInFlightRef.current = false;
    }
  };

  const busy = phase === "running";
  const handleOpenChange = (next: boolean) => {
    if (busy) return; // The dialog cannot close mid-rebuild.
    onOpenChange(next);
  };

  const modeButton = (value: Mode, title: string, desc: string) => (
    <button
      key={value}
      type="button"
      onClick={() => setMode(value)}
      className={`flex-1 rounded-md border p-3 text-left transition-colors ${
        mode === value
          ? "border-primary bg-primary/5 ring-1 ring-primary"
          : "border-border hover:bg-muted/50"
      }`}
    >
      <div className="text-sm font-medium">{title}</div>
      <div className="mt-0.5 text-xs text-muted-foreground">{desc}</div>
    </button>
  );

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader className="pb-4">
          <DialogTitle className="flex items-center gap-2">
            <RefreshCw className="h-4 w-4" />
            Rebuild Instance
          </DialogTitle>
          <DialogDescription>
            Reinstalls <span className="font-medium text-foreground">{instance.name}</span>&apos;s
            root disk from a fresh source. The instance keeps its name, configuration, network and
            attached volumes.
          </DialogDescription>
        </DialogHeader>

        {phase === "loading" && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Checking the instance…
          </div>
        )}

        {phase === "error" && (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" aria-hidden />
            <AlertDescription>{loadError || "Could not read the instance."}</AlertDescription>
          </Alert>
        )}

        {/* LXD refuses to rebuild an instance that has snapshots. */}
        {phase === "form" && blocked && (
          <div className="space-y-3">
            <div className="flex gap-2 rounded-md border border-status-pending/40 p-3 text-sm">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-status-pending" aria-hidden="true" />
              <div>
                <p className="font-medium">Delete snapshots before rebuilding</p>
                <p className="mt-1 text-muted-foreground">
                  LXD cannot rebuild an instance that has snapshots. Delete the{" "}
                  {plural(snapshots.length, "snapshot")} below, then reopen
                  this dialog.
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {snapshots.map((s) => (
                    <Badge key={s} variant="outline" className="font-mono text-xs">
                      {s}
                    </Badge>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}

        {phase === "form" && !blocked && (
          <div className="space-y-4">
            <div className="space-y-2">
              <Label>Rebuild from</Label>
              <div className="flex gap-2">
                {modeButton("image", "Image", "Reinstall from a base image")}
                {modeButton("cloud-init", "Cloud-init", "Image + provisioning")}
                {modeButton("empty", "Empty / ISO", "Blank disk, install via ISO")}
              </div>
            </div>

            {needsImage && (
              <div className="space-y-2">
                <Label>
                  Image {wantType ? <span className="text-muted-foreground">({wantType})</span> : null}
                </Label>
                <SearchableSelect
                  options={imageOptions}
                  value={selectedImageKey}
                  onValueChange={setSelectedImageKey}
                  onOpenChange={(pickerOpen) => {
                    if (pickerOpen) setRemoteImageIntent(true);
                  }}
                  freezeOptionsWhileOpen={false}
                  placeholder={loadingLocal ? "Loading local images…" : "Select an image"}
                  searchPlaceholder="Search images…"
                  emptyText={
                    loadingLocal || loadingRemote
                      ? "Loading image sources…"
                      : "No matching images"
                  }
                />
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-live="polite">
                  <span>
                    Local: {loadingLocal ? "loading" : localImagesFailed ? "unavailable" : `${localResp?.images.length ?? 0} available`}
                    {localImagesFailed ? (
                      <Button
                        type="button"
                        variant="link"
                        size="sm"
                        className="h-auto px-1 py-0 text-xs"
                        onClick={() => void refetchLocalImages()}
                      >
                        Retry
                      </Button>
                    ) : null}
                  </span>
                  <span>
                    Remote: {!remoteImageIntent ? "loads when opened" : loadingRemote ? "loading" : remoteImagesFailed ? "unavailable" : `${remoteResp?.images.length ?? 0} available`}
                    {remoteImagesFailed ? (
                      <Button
                        type="button"
                        variant="link"
                        size="sm"
                        className="h-auto px-1 py-0 text-xs"
                        onClick={() => void refetchRemoteImages()}
                      >
                        Retry
                      </Button>
                    ) : null}
                  </span>
                </div>
              </div>
            )}

            {mode === "cloud-init" && (
              <div className="space-y-2">
                <Label htmlFor="rebuild-cloud-init">cloud-init user-data</Label>
                <Textarea
                  id="rebuild-cloud-init"
                  value={cloudInit}
                  onChange={(e) => setCloudInit(e.target.value)}
                  placeholder={"#cloud-config\npackages:\n  - nginx"}
                  className="min-h-[120px] font-mono text-xs"
                />
              </div>
            )}

            {mode === "empty" && (
              <div className="flex gap-2 rounded-md border bg-muted/30 p-3 text-sm">
                <Disc3 className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                <p className="text-muted-foreground">
                  The root disk is wiped to a blank volume and the instance is left{" "}
                  <span className="font-medium text-foreground">stopped</span>. Attach an install ISO
                  and boot it from the console to install a new OS.
                </p>
              </div>
            )}

            <div className="flex gap-2 rounded-md border bg-muted/30 p-3 text-sm">
              <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-status-online" aria-hidden="true" />
              <div className="space-y-1 text-muted-foreground">
                <p>Kept: name, profiles/config, network config and the instance&apos;s IP.</p>
                {volumes.length > 0 ? (
                  <p>
                    Attached data volume{volumes.length === 1 ? "" : "s"} preserved:{" "}
                    {volumes.map((v) => (
                      <Badge key={v} variant="outline" className="ml-1 font-mono text-xs">
                        {v}
                      </Badge>
                    ))}
                  </p>
                ) : (
                  <p>Only the root disk is replaced. Attached data volumes are preserved.</p>
                )}
              </div>
            </div>

            {mode !== "empty" && (
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={runProvisioning}
                  onChange={(e) => setRunProvisioning(e.target.checked)}
                />
                <span>
                  <span className="font-medium">Wait for provisioning</span>
                  <span className="block text-xs text-muted-foreground">
                    Start the instance and wait until it boots and gets an IP before marking the
                    rebuild done.
                  </span>
                </span>
              </label>
            )}

            <div className="space-y-2 rounded-md border border-destructive/40 p-3">
              <div className="flex gap-2 text-sm text-destructive">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <p>
                  This <span className="font-semibold">erases the root filesystem</span>. Anything on
                  the root disk (not on a separate data volume) is lost. Type the instance name to
                  confirm.
                </p>
              </div>
              <Input
                value={confirmName}
                onChange={(e) => setConfirmName(e.target.value)}
                placeholder={instance.name}
                autoComplete="off"
                spellCheck={false}
              />
            </div>
          </div>
        )}

        {(phase === "running" || phase === "done" || phase === "failed" || phase === "unknown") && (
          <div className="space-y-3">
            {phase === "done" ? (
              <div className="flex items-center gap-2 text-sm font-medium">
                <CheckCircle2 className="h-5 w-5 text-status-online" aria-hidden="true" /> Rebuild complete
              </div>
            ) : phase === "failed" ? (
              <div className="flex items-center gap-2 text-sm font-medium text-destructive">
                <XCircle className="h-5 w-5" /> Rebuild failed
              </div>
            ) : phase === "unknown" ? (
              <div className="flex items-center gap-2 text-sm font-medium">
                <AlertTriangle className="h-5 w-5 text-status-pending" aria-hidden="true" /> Rebuild status unknown
              </div>
            ) : (
              <div className="flex items-center gap-2 text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                {STATUS_LABEL[jobStatus]}
              </div>
            )}
            <Progress value={phase === "done" ? 100 : PROGRESS[jobStatus]} />
            {(phase === "failed" || phase === "unknown") && runError && (
              <Alert variant={phase === "failed" ? "destructive" : "warning"}>
                <AlertTriangle className="h-4 w-4" aria-hidden />
                <AlertDescription className="break-words">{runError}</AlertDescription>
              </Alert>
            )}
            {phase === "done" && mode === "empty" && (
              <p className="text-xs text-muted-foreground">
                The instance is stopped with a blank root disk. Open its console to attach an ISO and
                install a new OS.
              </p>
            )}
          </div>
        )}

        <DialogFooter className="pt-4">
          {phase === "form" && !blocked ? (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={startRebuild}
                disabled={!canSubmit}
              >
                <RefreshCw className="mr-2 h-4 w-4" />
                Rebuild
              </Button>
            </>
          ) : phase === "running" ? (
            <Button disabled>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Rebuilding…
            </Button>
          ) : (
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Close
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
