"use client";

/**
 * Create or edit a network ACL.
 *
 * The page has been a reader since it shipped: the routes, the API client and the hooks all
 * existed, with no call site anywhere, so an administrator had to drop to `lxc network acl
 * create` on the hypervisor to define a rule and then come back here to look at it. That is
 * the gap operators reported and repeated as a feature request.
 *
 * An ACL is a name plus two ordered rule lists. LXD validates the rule bodies itself and its
 * errors are specific, so this form checks the things the operator can fix without a round
 * trip (a name LXD will reject, a rule with no action) and lets the daemon answer the rest.
 *
 * An edit seeds from a fresh read, not from the list row: the row can be minutes old and
 * carries no version. The read's etag travels back as If-Match, so two operators editing one
 * ACL no longer end with the later save winning whole; the second one is told to reload.
 */

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Plus, RefreshCw } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DialogSubmitError } from "@/components/ui/dialog-submit-error";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/hooks/use-toast";
import {
  useCreateNetworkACL,
  useNetworkACLForEdit,
  useProjects,
  useUpdateNetworkACL,
} from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { APIError, type NetworkACLInfo } from "@/types/api";

import { AclRuleRow } from "./acl-rule-row";
import {
  NAME_RE,
  cleanRule,
  emptyRule,
  rulePortErrors,
  type AclRule,
  type Direction,
} from "./acl-rules";

export type { AclRule } from "./acl-rules";

export interface SavedAcl {
  node: string;
  project: string;
  name: string;
  created: boolean;
}

interface AclFormDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Editing an existing ACL, or undefined to create one. */
  existing?: NetworkACLInfo;
  /** What was written, and whether it is new: only a create asks the list to focus it. */
  onSaved?: (saved: SavedAcl) => void;
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
  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: loadingNodes } = nodePicker;

  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [project, setProject] = useState("default");
  const [description, setDescription] = useState("");
  const [ingress, setIngress] = useState<AclRule[]>([]);
  const [egress, setEgress] = useState<AclRule[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The version the edit was seeded at, and whether a save was refused because it moved on.
  const [etag, setEtag] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  // Bumped by Reload so the edit read runs again instead of answering from cache.
  const [nonce, setNonce] = useState(0);

  const editRead = useNetworkACLForEdit(
    existing?.name ?? "",
    existing?.node ?? "",
    existing?.project,
    nonce,
    open && isEdit,
  );

  // Reset every time the dialog opens, so a cancelled edit does not seed the next one. The
  // row gives an edit its identity (name, node, project) at once; the rules and description
  // arrive with the fresh read below.
  useEffect(() => {
    if (!open) return;
    setName(existing?.name ?? "");
    setHost(existing?.node ?? "");
    setProject(existing?.project ?? "default");
    setDescription("");
    setIngress([]);
    setEgress([]);
    setError(null);
    setEtag(null);
    setStale(false);
    setNonce(0);
  }, [open, existing]);

  // Seed from the fresh read, and again after every Reload (the nonce is in the query key).
  useEffect(() => {
    if (!open || !isEdit || !editRead.data) return;
    const acl = editRead.data;
    setDescription(acl.description ?? "");
    setIngress((acl.ingress as AclRule[]) ?? []);
    setEgress((acl.egress as AclRule[]) ?? []);
    setEtag(acl.etag ?? null);
    setStale(false);
    setError(null);
  }, [open, isEdit, editRead.data]);

  // isPending, not isFetching: only the first read (and a Reload, which changes the key)
  // replaces the form; a background fetch must never swap it out from under an edit
  const reading = isEdit && editRead.isPending;
  const readFailed = isEdit && editRead.isError;
  const reload = () => setNonce((n) => n + 1);

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

  const rulesValid = [...ingress, ...egress].every(
    (r) => !!r.action && Object.keys(rulePortErrors(r)).length === 0,
  );
  const canSubmit =
    !!host &&
    (isEdit || (name !== "" && nameError === "")) &&
    rulesValid &&
    !submitting &&
    !reading &&
    !readFailed &&
    !stale;

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
  // click has something to lose against. Same guard as networks/network-create-dialog.tsx.
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
          etag,
        });
      } else {
        await createACL.mutateAsync({ data: payload, host, project });
      }
      toast({
        title: isEdit ? "Network ACL updated" : "Network ACL created",
        description: `"${payload.name}" on ${host}.`,
      });
      onOpenChange(false);
      onSaved?.({
        node: host,
        project: isEdit ? existing!.project || "default" : project,
        name: payload.name,
        created: !isEdit,
      });
    } catch (e) {
      if (e instanceof APIError && e.status === 412) {
        // Someone saved this ACL after it was read. The form keeps the operator's edits on
        // screen until they choose Reload, so nothing is lost silently either way.
        setStale(true);
      } else {
        setError(e instanceof Error ? e.message : "Failed to save the network ACL");
      }
    } finally {
      submitInFlightRef.current = false;
      setSubmitting(false);
    }
  };

  const ruleList = (dir: Direction, rules: AclRule[]) => (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <Label className="text-sm font-medium">
          {dir === "ingress" ? "Ingress Rules" : "Egress Rules"}
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
          <AclRuleRow
            key={`${dir}-${index}`}
            dir={dir}
            index={index}
            rule={rule}
            onChange={(patch) => setRule(dir, index, patch)}
            onRemove={() => removeRule(dir, index)}
          />
        ))
      )}
    </div>
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader className="pb-4">
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
              <Label htmlFor="acl-node">Cluster *</Label>
              <SearchableSelect
                id="acl-node"
                value={host}
                onValueChange={setHost}
                options={nodeOptions}
                placeholder={loadingNodes ? "Loading…" : "Select a cluster"}
                searchPlaceholder="Search clusters…"
                emptyText="No clusters found."
                disabled={isEdit || loadingNodes}
              />
              <NodeStatusNotice
                meta={host ? nodePicker.statusOf(host) : undefined}
                reason={host ? nodePicker.availabilityOf(host)?.reason ?? undefined : undefined}
                nodeLabel={host ? nodePicker.labelOf(host) : undefined}
                unavailable={nodePicker.statusUnavailable}
                noSelectableNodes={nodePicker.selectableCount === 0}
                updatedAt={nodePicker.statusUpdatedAt}
                onRecheck={nodePicker.refreshNow}
              />
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
              <SearchableSelect
                id="acl-project"
                value={project}
                onValueChange={setProject}
                options={projectOptions.map((p) => ({ value: p, label: p }))}
                placeholder="default"
                searchPlaceholder="Search projects…"
                emptyText="No projects found."
                disabled={isEdit}
              />
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
              disabled={reading}
            />
          </div>

          {stale && (
            <Alert role="alert" data-testid="acl-stale-notice">
              <RefreshCw className="h-4 w-4" />
              <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
                <span>
                  This ACL changed since you opened it. Reload to see the latest rules, then
                  apply your change again.
                </span>
                <Button type="button" variant="outline" size="sm" onClick={reload} disabled={reading}>
                  Reload
                </Button>
              </AlertDescription>
            </Alert>
          )}

          {readFailed ? (
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" aria-hidden />
              <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
                <span>
                  Could not read this ACL:{" "}
                  {editRead.error instanceof Error ? editRead.error.message : "the cluster did not answer"}
                </span>
                <Button type="button" variant="outline" size="sm" onClick={reload}>
                  Retry
                </Button>
              </AlertDescription>
            </Alert>
          ) : reading ? (
            <div className="space-y-3" aria-busy="true" aria-label="Reading the ACL">
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-16 w-full" />
            </div>
          ) : (
            <>
              {ruleList("ingress", ingress)}
              {ruleList("egress", egress)}
            </>
          )}

        </div>

        <DialogSubmitError message={error} />

        <DialogFooter className="pt-4">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={!canSubmit}>
            {submitting ? "Saving…" : isEdit ? "Save Changes" : "Create ACL"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
