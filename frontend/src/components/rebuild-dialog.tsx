"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/hooks/use-toast";
import { useImagesByHost, useRemoteImages } from "@/hooks/use-lxd";
import { api, type RebuildPreflight, type RebuildRequest, type RebuildStatus } from "@/lib/api";
import type { ImageInfo, RemoteImageInfo } from "@/types/api";
import { formatBytes } from "@/lib/utils";
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
type Phase = "loading" | "error" | "form" | "running" | "done" | "failed";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

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
  pending: "Queued…",
  stopping: "Stopping the instance…",
  rebuilding: "Reinstalling the root disk…",
  starting: "Starting the instance…",
  provisioning: "Waiting for provisioning…",
  ready: "Done",
  failed: "Failed",
};

export function RebuildDialog({ open, onOpenChange, instance, onDone }: RebuildDialogProps) {
  const { toast } = useToast();

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

  // --- Load the read-only preflight when the dialog opens --------------------
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
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

    api.instances
      .rebuildPreflight(instance.name, instance.host, instance.project || "default")
      .then((pf) => {
        if (cancelled) return;
        setPreflight(pf);
        setPhase("form");
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setLoadError(e instanceof Error ? e.message : String(e));
        setPhase("error");
      });
    return () => {
      cancelled = true;
    };
  }, [open, instance.name, instance.host, instance.project]);

  // --- Image catalog (local + remote), filtered to the instance type ---------
  const wantType = preflight?.type; // "container" | "virtual-machine"
  const { data: localResp, isLoading: loadingLocal } = useImagesByHost(open ? instance.host : "");
  const { data: remoteResp, isLoading: loadingRemote } = useRemoteImages();

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
  }, [localResp, remoteResp, wantType]);

  const imageOptions = useMemo(
    () => imageRows.map((r) => ({ value: r.key, label: r.label })),
    [imageRows],
  );
  const selectedRow = imageRows.find((r) => r.key === selectedImageKey);
  const imagesLoading = loadingLocal || loadingRemote;

  // --- Validation ------------------------------------------------------------
  const snapshots = preflight?.snapshots ?? [];
  const blocked = snapshots.length > 0;
  const volumes = preflight?.attached_volumes ?? [];
  const needsImage = mode === "image" || mode === "cloud-init";
  const nameMatches = confirmName.trim() === instance.name;
  const imageOk = !needsImage || !!selectedRow;
  const cloudInitOk = mode !== "cloud-init" || !!cloudInit.trim();
  const canSubmit = phase === "form" && !blocked && nameMatches && imageOk && cloudInitOk;

  // --- Submit + poll ---------------------------------------------------------
  const startRebuild = async () => {
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
    try {
      const job = await api.instances.rebuild(instance.name, body);
      let status = job.status;
      setJobStatus(status);
      while (status !== "ready" && status !== "failed") {
        await sleep(2000);
        const j = await api.instances.getRebuildJob(instance.name, job.job_id);
        status = j.status;
        setJobStatus(status);
        if (status === "failed") throw new Error(j.error || "Rebuild failed");
      }
      setPhase("done");
      toast({
        title: "Rebuild complete",
        description:
          mode === "empty"
            ? `"${instance.name}" now has a blank root disk. Open the console to attach an ISO and install.`
            : `"${instance.name}" was rebuilt successfully.`,
      });
      onDone?.();
    } catch (e: unknown) {
      setPhase("failed");
      setRunError(e instanceof Error ? e.message : String(e));
    }
  };

  const busy = phase === "running";
  const handleOpenChange = (next: boolean) => {
    if (busy) return; // don't let the user close mid-rebuild
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
        <DialogHeader>
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

        {/* Loading preflight */}
        {phase === "loading" && (
          <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Checking the instance…
          </div>
        )}

        {/* Preflight failed */}
        {phase === "error" && (
          <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
            {loadError || "Could not read the instance."}
          </div>
        )}

        {/* Snapshot block — LXD refuses to rebuild an instance with snapshots */}
        {phase === "form" && blocked && (
          <div className="space-y-3">
            <div className="flex gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
              <div>
                <p className="font-medium">Delete snapshots before rebuilding</p>
                <p className="mt-1 text-muted-foreground">
                  LXD cannot rebuild an instance that has snapshots. Delete the{" "}
                  {snapshots.length} snapshot{snapshots.length === 1 ? "" : "s"} below, then reopen
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

        {/* Main form */}
        {phase === "form" && !blocked && (
          <div className="space-y-4">
            {/* Mode */}
            <div className="space-y-2">
              <Label>Rebuild from</Label>
              <div className="flex gap-2">
                {modeButton("image", "Image", "Reinstall from a base image")}
                {modeButton("cloud-init", "Cloud-init", "Image + provisioning")}
                {modeButton("empty", "Empty / ISO", "Blank disk, install via ISO")}
              </div>
            </div>

            {/* Image picker */}
            {needsImage && (
              <div className="space-y-2">
                <Label>
                  Image {wantType ? <span className="text-muted-foreground">({wantType})</span> : null}
                </Label>
                <SearchableSelect
                  options={imageOptions}
                  value={selectedImageKey}
                  onValueChange={setSelectedImageKey}
                  placeholder={imagesLoading ? "Loading images…" : "Select an image"}
                  searchPlaceholder="Search images…"
                  emptyText={imagesLoading ? "Loading…" : "No matching images"}
                  disabled={imagesLoading}
                />
              </div>
            )}

            {/* Cloud-init */}
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

            {/* Empty note */}
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

            {/* Reassurances — what survives the rebuild */}
            <div className="flex gap-2 rounded-md border bg-muted/30 p-3 text-sm">
              <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
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
                  <p>Only the root disk is replaced — attached data volumes are preserved.</p>
                )}
              </div>
            </div>

            {/* Provisioning toggle (not for empty) */}
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

            {/* Destructive warning + name gate */}
            <div className="space-y-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
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

        {/* Running / done / failed */}
        {(phase === "running" || phase === "done" || phase === "failed") && (
          <div className="space-y-3 py-2">
            {phase === "done" ? (
              <div className="flex items-center gap-2 text-sm font-medium text-emerald-600">
                <CheckCircle2 className="h-5 w-5" /> Rebuild complete
              </div>
            ) : phase === "failed" ? (
              <div className="flex items-center gap-2 text-sm font-medium text-destructive">
                <XCircle className="h-5 w-5" /> Rebuild failed
              </div>
            ) : (
              <div className="flex items-center gap-2 text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                {STATUS_LABEL[jobStatus]}
              </div>
            )}
            <Progress value={phase === "done" ? 100 : PROGRESS[jobStatus]} />
            {phase === "failed" && runError && (
              <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive">
                {runError}
              </div>
            )}
            {phase === "done" && mode === "empty" && (
              <p className="text-xs text-muted-foreground">
                The instance is stopped with a blank root disk. Open its console to attach an ISO and
                install a new OS.
              </p>
            )}
          </div>
        )}

        <DialogFooter>
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
