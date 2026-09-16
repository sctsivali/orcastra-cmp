"use client";

import { useRef, useState } from "react";
import { Loader2 } from "lucide-react";

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
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { useToast } from "@/hooks/use-toast";
import { useCreateNetwork, useNetworks, useProjects } from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import type { NetworkCreateRequest } from "@/types/api";
import { AddressField } from "@/components/networks/address-field";
import {
  NETWORK_TYPES,
  ipv4AddressError,
  ipv6AddressError,
  networkNameError,
  networkNameMax,
  type IpMode,
  type NetworkType,
} from "@/components/networks/network-rules";

interface NetworkCreateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called after a successful create, once the dialog has closed. */
  onSaved?: () => void;
}

/**
 * Create Network as a dialog on the Networks page, the same shape as Create Network ACL and
 * Upload ISO. The form body is a child of DialogContent on purpose: Radix unmounts it while
 * the dialog is closed, so its node, project and uplink queries (useNetworks is always enabled
 * and polls the fleet) run only while an operator is actually creating a network, and every
 * reopen starts from a blank form.
 */
export function NetworkCreateDialog({ open, onOpenChange, onSaved }: NetworkCreateDialogProps) {
  const [busy, setBusy] = useState(false);

  // A create that resolves after the body unmounted would drop its error on the floor, so
  // Escape, the overlay, the X and Cancel are all refused while the request is in flight.
  const handleOpenChange = (next: boolean) => {
    if (!next && busy) return;
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-3xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0 pr-8 pb-4">
          <DialogTitle>Create Network</DialogTitle>
          <DialogDescription>Create a Bridge or OVN network on a node.</DialogDescription>
        </DialogHeader>
        <NetworkCreateBody
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

interface NetworkCreateBodyProps {
  onBusyChange: (busy: boolean) => void;
  onDone: () => void;
  onCancel: () => void;
}

function NetworkCreateBody({ onBusyChange, onDone, onCancel }: NetworkCreateBodyProps) {
  const { toast } = useToast();
  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: loadingNodes } = nodePicker;
  const createNetwork = useCreateNetwork();
  const { isReadOnly } = useReadOnlyMode();

  const [node, setNode] = useState("");
  const [project, setProject] = useState("default");
  const [type, setType] = useState<NetworkType>("bridge");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [uplink, setUplink] = useState("");
  const [ipv4Mode, setIpv4Mode] = useState<IpMode>("auto");
  const [ipv4Custom, setIpv4Custom] = useState("");
  const [ipv6Mode, setIpv6Mode] = useState<IpMode>("auto");
  const [ipv6Custom, setIpv6Custom] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Uplink candidates for OVN.
  //
  // important: LXD accepts only a managed bridge or physical network as an OVN uplink, and it
  // resolves the uplink in the DEFAULT project whatever project the OVN network itself lives
  // in. This list used to be filtered by node alone, so it offered other OVN networks, macvlan
  // interfaces and the loopback. On one node in this fleet that is 11 unusable candidates out
  // of 23. Picking one produced a create the daemon rejected, and an instance that never
  // finished coming up.
  const { data: networksResp } = useNetworks(node || undefined, "default", { enabled: !!node });
  const uplinkOptions = ((networksResp as any)?.networks || [])
    .filter((n: any) => (n.node ? n.node === node : true))
    .filter((n: any) => n.managed && (n.type === "bridge" || n.type === "physical"))
    .map((n: any) => ({ value: n.name, label: `${n.name} (${n.type})` }));

  // Projects that can hold a network. LXD refuses every type except OVN outside default, so a
  // bridge create is pinned to default rather than offered a choice it cannot honour.
  const { data: projectsResp } = useProjects(node);
  const projectOptions = (() => {
    const seen = new Set<string>(["default"]);
    for (const p of ((projectsResp as any)?.projects ?? []) as any[]) {
      if (p?.name) seen.add(p.name);
    }
    return Array.from(seen).sort();
  })();

  const nameMax = networkNameMax(type);
  const nameError = networkNameError(name, type);
  const nameValid = name !== "" && nameError === "";
  const ipv4Error = ipv4AddressError(ipv4Mode, ipv4Custom);
  const ipv6Error = ipv6AddressError(ipv6Mode, ipv6Custom);
  const uplinkError =
    type === "ovn" && !uplink ? "Select an uplink network for the OVN network." : "";
  const projectError =
    type !== "ovn" && project !== "default"
      ? "Only OVN networks can be created outside the default project."
      : "";

  // important: `submitting` is React state, so it only reaches `canSubmit` on the next render.
  // Two clicks inside one tick both pass the disabled check and both fire a create, which is
  // two networks on the cluster. The ref is claimed synchronously, before any await, so the
  // second click has something to lose against. Same guard as instance-create-form.tsx.
  const submitInFlightRef = useRef(false);

  const canSubmit =
    !!node &&
    nameValid &&
    !ipv4Error &&
    !ipv6Error &&
    !uplinkError &&
    !projectError &&
    !submitting &&
    !isReadOnly;

  const submit = async () => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setError(null);
    setSubmitting(true);
    onBusyChange(true);
    try {
      const config: Record<string, string> = {
        "ipv4.address": ipv4Mode === "custom" ? ipv4Custom.trim() : ipv4Mode,
        "ipv6.address": ipv6Mode === "custom" ? ipv6Custom.trim() : ipv6Mode,
      };
      if (type === "ovn") config["network"] = uplink;

      const payload: NetworkCreateRequest = {
        name,
        description: description.trim() || undefined,
        type,
        config,
      };
      const created = await createNetwork.mutateAsync({ data: payload, host: node, project });
      // A project with features.networks unset does not hold its own networks, so the backend
      // resolves the create to the project that does. Name where it landed, not what was asked
      // for, or the toast points the operator at a project the network is not in.
      const landed = (created as any)?.network?.project ?? project;
      toast({
        title: "Network created",
        description:
          landed === "default"
            ? `"${name}" created on ${node}.`
            : `"${name}" created on ${node} in project ${landed}.`,
      });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create network");
    } finally {
      submitInFlightRef.current = false;
      setSubmitting(false);
      onBusyChange(false);
    }
  };

  return (
    <>
      <DialogBody className="space-y-6 pr-2">
        <section className="space-y-4">
          <h3 className="text-sm font-semibold">General</h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label>Node *</Label>
              <SearchableSelect
                value={node}
                onValueChange={(next) => {
                  if (next === node) return;
                  setNode(next);
                  // The uplink and the project were read from the previous node; a stale
                  // uplink from node A used to survive a switch to node B and pass canSubmit.
                  setUplink("");
                  setProject("default");
                }}
                options={nodeOptions}
                placeholder={loadingNodes ? "Loading..." : "Select node"}
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
              <Label>Type *</Label>
              <Select
                value={type}
                onValueChange={(v) => {
                  const next = v as NetworkType;
                  setType(next);
                  // LXD refuses every type except OVN outside the default project, so
                  // leaving a project selected here would build a create it always rejects.
                  if (next !== "ovn") setProject("default");
                }}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {NETWORK_TYPES.map((t) => (
                    <SelectItem key={t.value} value={t.value}>
                      {t.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="net-project">Project</Label>
            <Select
              value={project}
              onValueChange={setProject}
              disabled={type !== "ovn" || !node}
            >
              <SelectTrigger id="net-project">
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
            {projectError ? (
              <p className="text-xs text-destructive">{projectError}</p>
            ) : (
              <p className="text-xs text-muted-foreground">
                {type === "ovn"
                  ? "An OVN network can belong to a project that isolates its networks."
                  : "A bridge network always belongs to the default project."}
              </p>
            )}
          </div>

          <div className="grid gap-2">
            <Label htmlFor="net-name">Name *</Label>
            <Input
              id="net-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-network"
              aria-invalid={!!nameError}
            />
            {nameError ? (
              <p className="text-xs text-destructive">{nameError}</p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Up to {nameMax} characters; letters, numbers and hyphens.
              </p>
            )}
          </div>

          <div className="grid gap-2">
            <Label htmlFor="net-desc">Description</Label>
            <Textarea
              id="net-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Optional description"
              rows={2}
            />
          </div>

          {type === "ovn" && (
            <div className="grid gap-2">
              <Label>Uplink network *</Label>
              <SearchableSelect
                value={uplink}
                onValueChange={setUplink}
                options={uplinkOptions}
                placeholder={
                  !node
                    ? "Select a node first"
                    : uplinkOptions.length === 0
                      ? "No usable uplink on this node"
                      : "Select uplink network"
                }
                searchPlaceholder="Search networks..."
                emptyText="No managed bridge or physical network here."
                disabled={!node || uplinkOptions.length === 0}
              />
              {uplinkError ? (
                <p className="text-xs text-destructive">{uplinkError}</p>
              ) : node && uplinkOptions.length === 0 ? (
                <p className="text-xs text-destructive">
                  This node has no managed bridge or physical network to uplink through.
                  Create one in the default project first.
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Only a managed bridge or physical network can carry an OVN uplink, and it is
                  always read from the default project.
                </p>
              )}
            </div>
          )}
        </section>

        <section className="space-y-4">
          <h3 className="text-sm font-semibold">Addresses</h3>
          <AddressField
            label="IPv4 address"
            mode={ipv4Mode}
            onModeChange={setIpv4Mode}
            custom={ipv4Custom}
            onCustomChange={setIpv4Custom}
            which="IPv4"
            error={ipv4Error}
          />
          <AddressField
            label="IPv6 address"
            mode={ipv6Mode}
            onModeChange={setIpv6Mode}
            custom={ipv6Custom}
            onCustomChange={setIpv6Custom}
            which="IPv6"
            error={ipv6Error}
          />
        </section>

        {error && (
          <div className="p-3 text-sm bg-destructive/10 border border-destructive/30 text-destructive rounded-lg">
            {error}
          </div>
        )}
      </DialogBody>

      <DialogFooter className="shrink-0 pt-4 sm:items-center">
        {/* A disabled button shows no title tooltip, so state the reason inline. */}
        {!canSubmit && !submitting && (
          <span className="text-xs text-muted-foreground sm:mr-auto">
            {isReadOnly
              ? "Disabled in read-only mode"
              : !node
                ? "Select a node"
                : name === ""
                  ? "Enter a name"
                  : nameError || ipv4Error || ipv6Error || uplinkError || ""}
          </span>
        )}
        <Button variant="outline" onClick={onCancel} disabled={submitting}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={!canSubmit}>
          {submitting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
          Create
        </Button>
      </DialogFooter>
    </>
  );
}
