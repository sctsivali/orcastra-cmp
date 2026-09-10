"use client";

import { useRef, useState } from "react";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
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
import { useToast } from "@/hooks/use-toast";
import { useCreateNetwork, useNetworks, useProjects } from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { NetworkCreateRequest } from "@/types/api";
import { Loader2 } from "lucide-react";

type IpMode = "auto" | "none" | "custom";

/** LXD wants a real CIDR: "not-an-ip", a bare address, or /33 must be rejected. */
function isValidIpv4Cidr(value: string): boolean {
  const m = value.trim().match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/);
  if (!m) return false;
  const octets = [m[1], m[2], m[3], m[4]].map(Number);
  if (octets.some((o) => o > 255)) return false;
  const prefix = Number(m[5]);
  return prefix >= 0 && prefix <= 32;
}

function isValidIpv6Cidr(value: string): boolean {
  const m = value.trim().match(/^([0-9a-fA-F:]+)\/(\d{1,3})$/);
  if (!m) return false;
  const prefix = Number(m[2]);
  if (prefix > 128) return false;
  const addr = m[1];
  if ((addr.match(/::/g) || []).length > 1) return false; // at most one "::"
  const groups = addr.split(":").filter((g) => g !== "");
  if (groups.length === 0 || groups.length > 8) return false;
  return groups.every((g) => /^[0-9a-fA-F]{1,4}$/.test(g));
}

// Orcastra only supports Bridge and OVN networks.
const NETWORK_TYPES = [
  { value: "bridge", label: "Bridge" },
  { value: "ovn", label: "OVN" },
];

export function NetworkCreateForm() {
  // note: navigate() rather than a bare push, so the leaving transition raises the
  // global progress bar. These hops follow a mutation and are the slowest in the app.
  const { navigate } = useNavigationProgress();
  const { toast } = useToast();

  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: loadingNodes } = nodePicker;
  const createNetwork = useCreateNetwork();
  const { isReadOnly } = useReadOnlyMode();

  const [node, setNode] = useState("");
  const [project, setProject] = useState("default");
  const [type, setType] = useState<"bridge" | "ovn">("bridge");
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
  const { data: networksResp } = useNetworks(node || undefined, "default");
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

  // Bridge network names become Linux interface names (<= 15 chars). OVN names
  // don't map to a host interface, so they can be longer. LXD also rejects a
  // leading hyphen and all-digit names, so require a leading letter.
  const nameMax = type === "bridge" ? 15 : 63;
  const nameError =
    name === ""
      ? ""
      : !/^[a-zA-Z][a-zA-Z0-9-]*$/.test(name)
        ? "Must start with a letter and contain only letters, numbers and hyphens."
        : name.length > nameMax
          ? `Name must be at most ${nameMax} characters (${type}).`
          : "";
  const nameValid = name !== "" && nameError === "";

  const ipv4Error =
    ipv4Mode !== "custom"
      ? ""
      : ipv4Custom.trim() === ""
        ? "Enter an IPv4 address in CIDR notation."
        : !isValidIpv4Cidr(ipv4Custom)
          ? "Invalid IPv4 CIDR, for example 10.10.10.1/24."
          : "";
  const ipv6Error =
    ipv6Mode !== "custom"
      ? ""
      : ipv6Custom.trim() === ""
        ? "Enter an IPv6 address in CIDR notation."
        : !isValidIpv6Cidr(ipv6Custom)
          ? "Invalid IPv6 CIDR, for example fd42:1:2:3::1/64."
          : "";
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
      navigate("/inventory/networks");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create network");
    } finally {
      submitInFlightRef.current = false;
      setSubmitting(false);
    }
  };

  const ipField = (
    label: string,
    mode: IpMode,
    setMode: (m: IpMode) => void,
    custom: string,
    setCustom: (v: string) => void,
    which: string,
    fieldError: string,
  ) => (
    <div className="grid gap-2">
      <Label>{label}</Label>
      <div className="flex flex-col sm:flex-row gap-3 sm:items-center">
        <Select value={mode} onValueChange={(v) => setMode(v as IpMode)}>
          <SelectTrigger className="sm:w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="auto">Auto</SelectItem>
            <SelectItem value="none">None</SelectItem>
            <SelectItem value="custom">Custom</SelectItem>
          </SelectContent>
        </Select>
        {mode === "custom" && (
          <Input
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            placeholder={
              which === "IPv4" ? "10.10.10.1/24" : "fd42:1:2:3::1/64"
            }
            className="flex-1"
            aria-invalid={!!fieldError}
          />
        )}
      </div>
      {fieldError ? (
        <p className="text-xs text-destructive">{fieldError}</p>
      ) : (
        <p className="text-xs text-muted-foreground">
          Use CIDR notation. <span className="font-mono">auto</span> generates an
          unused subnet, <span className="font-mono">none</span> disables {which}.
        </p>
      )}
    </div>
  );

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h1 className="text-3xl font-bold">Create New Network</h1>
        <p className="text-muted-foreground mt-2">
          Create a Bridge or OVN network on a node
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>General</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label>Node *</Label>
              <SearchableSelect
                value={node}
                onValueChange={setNode}
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
                  const next = v as "bridge" | "ovn";
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
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Addresses</CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          {ipField("IPv4 address", ipv4Mode, setIpv4Mode, ipv4Custom, setIpv4Custom, "IPv4", ipv4Error)}
          {ipField("IPv6 address", ipv6Mode, setIpv6Mode, ipv6Custom, setIpv6Custom, "IPv6", ipv6Error)}
        </CardContent>
      </Card>

      {error && (
        <div className="p-3 text-sm bg-destructive/10 border border-destructive/30 text-destructive rounded-lg">
          {error}
        </div>
      )}

      <div className="flex items-center justify-end gap-3">
        {/* A disabled button shows no title tooltip, so state the reason inline. */}
        {!canSubmit && !submitting && (
          <span className="text-xs text-muted-foreground">
            {isReadOnly
              ? "Disabled in read-only mode"
              : !node
                ? "Select a node"
                : name === ""
                  ? "Enter a name"
                  : nameError || ipv4Error || ipv6Error || uplinkError || ""}
          </span>
        )}
        <Button
          variant="outline"
          onClick={() => navigate("/inventory/networks")}
        >
          Cancel
        </Button>
        <Button onClick={submit} disabled={!canSubmit}>
          {submitting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
          Create
        </Button>
      </div>
    </div>
  );
}
