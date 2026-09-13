"use client";

/**
 * Create or edit a profile.
 *
 * The Profiles page had been a reader since it shipped: the routes, the API client and the
 * hooks existed with no call site, so an operator changed a project-level profile with `lxc
 * profile edit` on the hypervisor. This dialog is the same shape as Create Network and Create
 * Network ACL: the body is a child of DialogContent so Radix unmounts it while closed, every
 * reopen starts clean, and Escape, the overlay and Cancel are refused while a write is in
 * flight.
 *
 * An edit seeds from a fresh read, not from the list row: the row can be minutes old and
 * carries no version. The read's etag travels back as If-Match, and a save over someone
 * else's change is refused with a Reload rather than overwriting it whole.
 */

import { useEffect, useRef, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { SearchableSelect } from "@/components/ui/searchable-select";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/hooks/use-toast";
import {
  useCreateProfile,
  useProfileForEdit,
  useProjects,
  useUpdateProfile,
} from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { APIError, type ProfileInfo } from "@/types/api";

import { KeyValueRows } from "./key-value-rows";
import { ProfileDevicesEditor } from "./profile-devices-editor";
import {
  DESCRIPTION_MAX,
  STALE_PROFILE_MESSAGE,
  configRowErrors,
  descriptionError,
  deviceErrors,
  devicesToDrafts,
  draftsToDevices,
  mapToRows,
  profileNameError,
  rowsToMap,
  type ConfigRow,
  type DeviceDraft,
} from "./profile-rules";

interface ProfileFormDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Editing an existing profile (its identity: name, node, project), or undefined to create one. */
  existing?: ProfileInfo;
  onSaved?: () => void;
}

export function ProfileFormDialog({ open, onOpenChange, existing, onSaved }: ProfileFormDialogProps) {
  const [busy, setBusy] = useState(false);
  const handleOpenChange = (next: boolean) => {
    if (!next && busy) return;
    onOpenChange(next);
  };
  const isEdit = !!existing;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-3xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0 pr-8 pb-4">
          <DialogTitle>{isEdit ? "Edit Profile" : "Create Profile"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? `${existing!.name} on ${existing!.node}${existing!.project && existing!.project !== "default" ? ` in project ${existing!.project}` : ""}.`
              : "A named set of configuration and devices that instances inherit."}
          </DialogDescription>
        </DialogHeader>
        <ProfileFormBody
          existing={existing}
          onBusyChange={setBusy}
          onDone={() => {
            onOpenChange(false);
            onSaved?.();
          }}
          onCancel={() => handleOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  );
}

interface ProfileFormBodyProps {
  existing?: ProfileInfo;
  onBusyChange: (busy: boolean) => void;
  onDone: () => void;
  onCancel: () => void;
}

function ProfileFormBody({ existing, onBusyChange, onDone, onCancel }: ProfileFormBodyProps) {
  const { toast } = useToast();
  const { isReadOnly } = useReadOnlyMode();
  const isEdit = !!existing;
  const createProfile = useCreateProfile();
  const updateProfile = useUpdateProfile();

  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: loadingNodes } = nodePicker;

  const [node, setNode] = useState(existing?.node ?? "");
  const [project, setProject] = useState(existing?.project ?? "default");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [config, setConfig] = useState<ConfigRow[]>([]);
  const [devices, setDevices] = useState<DeviceDraft[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The version the edit was seeded at, and whether a save was refused because it moved on.
  const [etag, setEtag] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  // Bumped by Reload so the edit read runs again instead of answering from cache.
  const [nonce, setNonce] = useState(0);

  const editRead = useProfileForEdit(
    existing?.name ?? "",
    existing?.node ?? "",
    existing?.project,
    nonce,
    isEdit,
  );

  // Seed from the fresh read, and again after every Reload (the nonce is in the query key).
  useEffect(() => {
    if (!isEdit || !editRead.data) return;
    const profile = editRead.data;
    setDescription(profile.description ?? "");
    setConfig(mapToRows(profile.config));
    setDevices(devicesToDrafts(profile.devices));
    setEtag(profile.etag ?? null);
    setStale(false);
    setError(null);
  }, [isEdit, editRead.data]);

  // isPending, not isFetching: only the first read (and a Reload, which changes the key)
  // replaces the form; a background fetch must never swap it out from under an edit
  const reading = isEdit && editRead.isPending;
  const readFailed = isEdit && editRead.isError;
  const reload = () => setNonce((n) => n + 1);

  const { data: projectsResp } = useProjects(node);
  const projectOptions = (() => {
    const seen = new Set<string>(["default"]);
    for (const p of ((projectsResp as any)?.projects ?? []) as any[]) {
      if (p?.name) seen.add(p.name);
    }
    return Array.from(seen).sort();
  })();

  const nameError = isEdit ? "" : profileNameError(name);
  const descError = descriptionError(description);
  const configErrors = configRowErrors(config);
  const devErrors = deviceErrors(devices);
  const blankRow =
    config.some((r) => !r.key.trim()) ||
    devices.some((d) => !d.name.trim() || !d.type || d.props.some((r) => !r.key.trim()));
  const firstProblem =
    nameError ||
    descError ||
    Object.values(configErrors).find(Boolean) ||
    (() => {
      for (const err of Object.values(devErrors)) {
        const first = err.name || err.type || Object.values(err.props).find(Boolean);
        if (first) return first;
      }
      return "";
    })() ||
    (blankRow ? "Fill in or remove the empty row" : "");

  // important: `submitting` is React state, so it only reaches `canSubmit` on the next render.
  // Two clicks inside one tick both pass the disabled check and both fire a create, which is
  // two profiles on the cluster. The ref is claimed synchronously, before any await.
  const submitInFlightRef = useRef(false);

  const canSubmit =
    !!node &&
    (isEdit || name !== "") &&
    firstProblem === "" &&
    !submitting &&
    !isReadOnly &&
    !reading &&
    !readFailed &&
    !stale;

  const submit = async () => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setError(null);
    setSubmitting(true);
    onBusyChange(true);
    try {
      if (isEdit) {
        await updateProfile.mutateAsync({
          name: existing!.name,
          host: existing!.node,
          project: existing!.project,
          // the editor shows every key, so both maps are sent whole
          data: { description: description.trim(), config: rowsToMap(config), devices: draftsToDevices(devices) },
          etag,
        });
        toast({ title: "Profile updated", description: `"${existing!.name}" on ${existing!.node}.` });
      } else {
        const created = await createProfile.mutateAsync({
          data: { name, description: description.trim(), config: rowsToMap(config), devices: draftsToDevices(devices) },
          host: node,
          project,
        });
        // A project without features.profiles holds no profiles of its own, so the backend
        // resolves the create to the project that does. Name where it landed.
        const landed = created?.profile?.project ?? project;
        toast({
          title: "Profile created",
          description:
            landed === "default" ? `"${name}" created on ${node}.` : `"${name}" created on ${node} in project ${landed}.`,
        });
      }
      onDone();
    } catch (e) {
      if (e instanceof APIError && e.status === 412) {
        // Someone saved this profile after it was read. The edits stay on screen until the
        // operator chooses Reload, so nothing is lost silently either way.
        setStale(true);
      } else {
        setError(e instanceof Error ? e.message : "Failed to save the profile");
      }
    } finally {
      submitInFlightRef.current = false;
      setSubmitting(false);
      onBusyChange(false);
    }
  };

  const disabledReason = isReadOnly
    ? "Disabled in read-only mode"
    : reading
      ? "Reading the current profile"
      : readFailed
        ? "The profile could not be read"
        : stale
          ? "Reload before saving"
          : !node
            ? "Select a node"
            : !isEdit && name === ""
              ? "Enter a name"
              : firstProblem;

  return (
    <>
      <DialogBody className="space-y-6 pr-2">
        <section className="space-y-4">
          <h3 className="text-sm font-semibold">General</h3>
          {isEdit ? (
            <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
              <div>
                <dt className="text-xs text-muted-foreground">Name</dt>
                <dd className="font-mono">{existing!.name}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Node</dt>
                <dd>{existing!.node}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Project</dt>
                <dd>{existing!.project || "default"}</dd>
              </div>
            </dl>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="grid gap-2">
                <Label htmlFor="profile-node">Node *</Label>
                <SearchableSelect
                  id="profile-node"
                  value={node}
                  onValueChange={(next) => {
                    if (next === node) return;
                    setNode(next);
                    setProject("default");
                  }}
                  options={nodeOptions}
                  placeholder={loadingNodes ? "Loading..." : "Select a node"}
                  searchPlaceholder="Search nodes..."
                  emptyText="No nodes found."
                  disabled={loadingNodes}
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
                <Label htmlFor="profile-project">Project</Label>
                <Select value={project} onValueChange={setProject} disabled={!node}>
                  <SelectTrigger id="profile-project">
                    <SelectValue placeholder="default" />
                  </SelectTrigger>
                  <SelectContent>
                    {projectOptions.map((p) => (
                      <SelectItem key={p} value={p}>
                        {p}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  A project holds its own profiles only when it isolates them; otherwise the
                  profile lands in default.
                </p>
              </div>
              <div className="grid gap-2 sm:col-span-2">
                <Label htmlFor="profile-name">Name *</Label>
                <Input
                  id="profile-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="web-tier"
                  aria-invalid={nameError ? true : undefined}
                  aria-describedby={nameError ? "profile-name-error" : undefined}
                />
                {nameError ? (
                  <p id="profile-name-error" className="text-xs text-destructive">
                    {nameError}
                  </p>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    Up to 63 characters; letters, numbers, dots, underscores and hyphens.
                  </p>
                )}
              </div>
            </div>
          )}

          {readFailed ? (
            <div className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm bg-destructive/10 border border-destructive/30 text-destructive rounded-lg">
              <span>
                Could not read this profile:{" "}
                {editRead.error instanceof Error ? editRead.error.message : "the cluster did not answer"}
              </span>
              <Button type="button" variant="outline" size="sm" onClick={reload}>
                Retry
              </Button>
            </div>
          ) : reading ? (
            <div className="space-y-3" aria-busy="true" aria-label="Reading the profile">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : (
            <div className="grid gap-2">
              <Label htmlFor="profile-description">Description</Label>
              <Textarea
                id="profile-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="What instances with this profile are for"
                rows={2}
                aria-invalid={descError ? true : undefined}
              />
              {descError ? (
                <p className="text-xs text-destructive">{descError}</p>
              ) : description.length > DESCRIPTION_MAX - 55 ? (
                <p className="text-xs text-muted-foreground">
                  {description.length} characters; the table shows the first two lines
                </p>
              ) : null}
            </div>
          )}
        </section>

        {stale && (
          <Alert role="alert" data-testid="profile-stale-notice">
            <RefreshCw className="h-4 w-4" />
            <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
              <span>{STALE_PROFILE_MESSAGE}</span>
              <Button type="button" variant="outline" size="sm" onClick={reload} disabled={reading}>
                Reload
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {!reading && !readFailed && (
          <>
            <section className="space-y-3">
              <h3 className="text-sm font-semibold">Configuration</h3>
              <KeyValueRows
                rows={config}
                onChange={setConfig}
                errors={configErrors}
                noun="config key"
                emptyText="No config keys. Instances that use this profile inherit nothing from it."
                addLabel="Add key"
                keyPlaceholder="limits.cpu"
                valuePlaceholder="2"
                idPrefix="profile-config"
              />
            </section>
            <section className="space-y-3">
              <h3 className="text-sm font-semibold">Devices</h3>
              <ProfileDevicesEditor drafts={devices} onChange={setDevices} errors={devErrors} />
            </section>
          </>
        )}

        {error && (
          <div className="p-3 text-sm bg-destructive/10 border border-destructive/30 text-destructive rounded-lg">
            {error}
          </div>
        )}
      </DialogBody>

      <DialogFooter className="shrink-0 pt-4 sm:items-center">
        {!canSubmit && !submitting && (
          <span className="text-xs text-muted-foreground sm:mr-auto">{disabledReason}</span>
        )}
        <Button variant="outline" onClick={onCancel} disabled={submitting}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={!canSubmit}>
          {submitting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
          {isEdit ? "Save changes" : "Create"}
        </Button>
      </DialogFooter>
    </>
  );
}
