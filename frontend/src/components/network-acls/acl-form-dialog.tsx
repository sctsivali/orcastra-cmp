"use client";

/**
 * Create or edit a network ACL.
 *
 * The page has been a reader since it shipped: the routes, the API client and the hooks all
 * existed, with no call site anywhere, so an administrator had to drop to `lxc network acl
 * create` on the hypervisor to define a rule and then come back here to look at it. That is
 * the gap the EcoBytes audit filed as BUG-ORC-005A(b) and repeated as a feature request.
 *
 * An ACL is a name plus two ordered rule lists. LXD validates the rule bodies itself and its
 * errors are specific, so this form checks the things the operator can fix without a round
 * trip (a name LXD will reject, a rule with no action) and lets the daemon answer the rest.
 */

import { useEffect, useRef, useState } from "react";
import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/hooks/use-toast";
import {
  useCreateNetworkACL,
  useProjects,
  useUpdateNetworkACL,
} from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import type { NetworkACLInfo } from "@/types/api";

// LXD's own vocabulary for a rule. Anything outside these it rejects.
const ACTIONS = ["allow", "drop", "reject"] as const;
const PROTOCOLS = ["", "tcp", "udp", "icmp4", "icmp6"] as const;
const STATES = ["enabled", "disabled", "logged"] as const;

// Same rule LXD applies: start with a letter, letters, numbers and hyphens after it.
const NAME_RE = /^[a-zA-Z][a-zA-Z0-9-]*$/;

export interface AclRule {
  action: string;
  protocol?: string;
  source?: string;
  destination?: string;
  source_port?: string;
  destination_port?: string;
  description?: string;
  state?: string;
}

type Direction = "ingress" | "egress";

function emptyRule(): AclRule {
  return { action: "allow", state: "enabled" };
}

/** Strip the keys LXD has no use for, so an untouched field is absent rather than "". */
function cleanRule(rule: AclRule): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(rule)) {
    const trimmed = (value ?? "").trim();
    if (trimmed) out[key] = trimmed;
  }
  return out;
}

interface AclFormDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Editing an existing ACL, or undefined to create one. */
  existing?: NetworkACLInfo;
  onSaved?: () => void;
}

export function AclFormDialog({
  open,
  onOpenChange,
  existing,
  onSaved,
}: AclFormDialogProps) {
  const { toast } = useToast();
  const createACL = useCreateNetworkACL();
  const updateACL = useUpdateNetworkACL();
  const isEdit = !!existing;

  // important: the node list comes from the fleet, not from the rows on the page. Deriving it
  // from the rows fails exactly when there are none to derive it from, which is the state an
  // operator is in the first time they open this.
  const { options: nodeOptions } = useNodePickerOptions();

  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [project, setProject] = useState("default");
  const [description, setDescription] = useState("");
  const [ingress, setIngress] = useState<AclRule[]>([]);
  const [egress, setEgress] = useState<AclRule[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset every time the dialog opens, so a cancelled edit does not seed the next one.
  useEffect(() => {
    if (!open) return;
    setName(existing?.name ?? "");
    setHost(existing?.node ?? "");
    setProject(existing?.project ?? "default");
    setDescription(existing?.description ?? "");
    setIngress((existing?.ingress as AclRule[]) ?? []);
    setEgress((existing?.egress as AclRule[]) ?? []);
    setError(null);
  }, [open, existing]);

  const nameError =
    isEdit || name === ""
      ? ""
      : !NAME_RE.test(name)
        ? "Must start with a letter and contain only letters, numbers and hyphens."
        : name.length > 63
          ? "Name must be at most 63 characters."
          : "";
  const { data: projectsResp } = useProjects(host);
  const projectOptions = (() => {
    const seen = new Set<string>(["default"]);
    for (const p of ((projectsResp as any)?.projects ?? []) as any[]) {
      if (p?.name) seen.add(p.name);
    }
    return Array.from(seen).sort();
  })();

  const rulesValid = [...ingress, ...egress].every((r) => !!r.action);
  const canSubmit =
    !!host && (isEdit || (name !== "" && nameError === "")) && rulesValid && !submitting;

  const setRule = (dir: Direction, index: number, patch: Partial<AclRule>) => {
    const apply = (rules: AclRule[]) =>
      rules.map((r, i) => (i === index ? { ...r, ...patch } : r));
    if (dir === "ingress") setIngress(apply);
    else setEgress(apply);
  };
  const addRule = (dir: Direction) =>
    dir === "ingress"
      ? setIngress((r) => [...r, emptyRule()])
      : setEgress((r) => [...r, emptyRule()]);
  const removeRule = (dir: Direction, index: number) =>
    dir === "ingress"
      ? setIngress((r) => r.filter((_, i) => i !== index))
      : setEgress((r) => r.filter((_, i) => i !== index));

  // important: `submitting` is React state, so it only reaches `canSubmit` on the next render.
  // Two clicks inside one tick both pass the disabled check and both fire a create, which is
  // two ACLs on the cluster. The ref is claimed synchronously, before any await, so the second
  // click has something to lose against. Same guard as network-create-form.tsx.
  const submitInFlightRef = useRef(false);

  const submit = async () => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const payload = {
        name: isEdit ? existing!.name : name,
        description: description.trim(),
        ingress: ingress.map(cleanRule),
        egress: egress.map(cleanRule),
        // important: omit config on an edit. Sending {} replaces whatever LXD holds, so an
        // edit that only touched a rule silently dropped the ACL's config keys. Leaving the
        // key out lets the backend's read-modify-write carry the live value through.
        ...(isEdit ? {} : { config: {} }),
      };
      if (isEdit) {
        await updateACL.mutateAsync({
          name: existing!.name,
          host,
          data: payload,
          project: existing!.project,
        });
      } else {
        await createACL.mutateAsync({ data: payload, host, project });
      }
      toast({
        title: isEdit ? "Network ACL updated" : "Network ACL created",
        description: `"${payload.name}" on ${host}.`,
      });
      onOpenChange(false);
      onSaved?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save the network ACL");
    } finally {
      submitInFlightRef.current = false;
      setSubmitting(false);
    }
  };

  const ruleList = (dir: Direction, rules: AclRule[]) => (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <Label className="text-sm font-medium">
          {dir === "ingress" ? "Ingress rules" : "Egress rules"}
        </Label>
        <Button type="button" variant="outline" size="sm" onClick={() => addRule(dir)}>
          <Plus className="h-4 w-4 mr-2" />
          Add rule
        </Button>
      </div>
      {rules.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          {dir === "ingress"
            ? "No ingress rules. Traffic into the attached instances follows the network default."
            : "No egress rules. Traffic out of the attached instances follows the network default."}
        </p>
      ) : (
        rules.map((rule, index) => (
          <div
            key={`${dir}-${index}`}
            className="grid grid-cols-2 sm:grid-cols-[1fr_1fr_1fr_1fr_auto] gap-2 rounded-lg border p-3 items-end"
          >
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">Action</Label>
              <Select
                value={rule.action}
                onValueChange={(v) => setRule(dir, index, { action: v })}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ACTIONS.map((a) => (
                    <SelectItem key={a} value={a}>
                      {a}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">Protocol</Label>
              <Select
                value={rule.protocol || "any"}
                onValueChange={(v) =>
                  setRule(dir, index, { protocol: v === "any" ? "" : v })
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="any">any</SelectItem>
                  {PROTOCOLS.filter(Boolean).map((p) => (
                    <SelectItem key={p} value={p}>
                      {p}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">
                {dir === "ingress" ? "Source" : "Destination"}
              </Label>
              <Input
                value={dir === "ingress" ? rule.source ?? "" : rule.destination ?? ""}
                onChange={(e) =>
                  setRule(
                    dir,
                    index,
                    dir === "ingress"
                      ? { source: e.target.value }
                      : { destination: e.target.value },
                  )
                }
                placeholder="Any address"
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">Destination port</Label>
              <Input
                value={rule.destination_port ?? ""}
                onChange={(e) =>
                  setRule(dir, index, { destination_port: e.target.value })
                }
                placeholder="Any port"
              />
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => removeRule(dir, index)}
              aria-label={`Remove ${dir} rule ${index + 1}`}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
            <div className="col-span-2 sm:col-span-5 space-y-1">
              <Label className="text-xs text-muted-foreground">State</Label>
              <Select
                value={rule.state || "enabled"}
                onValueChange={(v) => setRule(dir, index, { state: v })}
              >
                <SelectTrigger className="sm:w-48">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {STATES.map((st) => (
                    <SelectItem key={st} value={st}>
                      {st}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        ))
      )}
    </div>
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>
            {isEdit ? `Edit ${existing!.name}` : "Create Network ACL"}
          </DialogTitle>
          <DialogDescription>
            {isEdit
              ? "Change the ingress and egress rules this ACL applies."
              : "Define a named set of ingress and egress rules that networks and instances can attach to."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6 max-h-[60vh] overflow-y-auto pr-1">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid gap-2">
              <Label htmlFor="acl-node">Node *</Label>
              <Select value={host} onValueChange={setHost} disabled={isEdit}>
                <SelectTrigger id="acl-node">
                  <SelectValue placeholder="Select a node" />
                </SelectTrigger>
                <SelectContent>
                  {nodeOptions.map((n) => (
                    <SelectItem key={n.value} value={n.value} disabled={n.disabled}>
                      {n.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="acl-name">Name *</Label>
              <Input
                id="acl-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="web-tier"
                disabled={isEdit}
                aria-invalid={!!nameError}
              />
              {nameError ? (
                <p className="text-xs text-destructive">{nameError}</p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  {isEdit ? "A name cannot be changed after creation." : "Up to 63 characters."}
                </p>
              )}
            </div>
            <div className="grid gap-2">
              <Label htmlFor="acl-project">Project</Label>
              <Select value={project} onValueChange={setProject} disabled={isEdit}>
                <SelectTrigger id="acl-project">
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
                An ACL belongs to one project, and only a project that isolates its networks
                holds its own.
              </p>
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="acl-description">Description</Label>
            <Textarea
              id="acl-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this policy is for"
              rows={2}
            />
          </div>

          {ruleList("ingress", ingress)}
          {ruleList("egress", egress)}

          {error && (
            <div className="p-3 text-sm bg-destructive/10 border border-destructive/30 text-destructive rounded-lg">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={!canSubmit}>
            {submitting ? "Saving..." : isEdit ? "Save changes" : "Create ACL"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
