"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
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
import { DialogSubmitError } from "@/components/ui/dialog-submit-error";
import { Label } from "@/components/ui/label";
import { OverflowText } from "@/components/ui/overflow-text";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { toast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import type { ClusterBindPreview, ClusterBindingKind, IntegrationOrganization } from "@/types/api";

const KIND_LABEL: Record<ClusterBindingKind, string> = {
  registered: "Registered",
  granted: "Granted",
  purchase: "Purchase",
  bound: "Bound",
};

const KIND_HINT: Record<ClusterBindingKind, string> = {
  registered: "A partner of this organization registered the cluster.",
  granted: "An administrator bound the cluster to this organization.",
  purchase: "Provisioning bound it for a sale; it ends with the sale.",
  bound: "Bound before the dashboard recorded who granted it.",
};

/** The consequences a preview names, in the order an operator weighs them. */
function consequences(preview: ClusterBindPreview): { tone: "info" | "warn"; text: string }[] {
  const out: { tone: "info" | "warn"; text: string }[] = [];
  out.push({
    tone: "info",
    text:
      preview.binding_kind === "registered"
        ? "Recorded as registered by you: your party holds this cluster whole."
        : "Recorded as granted by you, an administrator. It is not a purchase and does not end with one.",
  });
  out.push({
    tone: "info",
    text:
      "Projects this organization's partners, and the keys they created, make on the cluster are recorded to it, so " +
      "they can see and manage them.",
  });
  if (preview.several_organizations_after && !preview.several_organizations_before) {
    out.push({
      tone: "warn",
      text:
        "The cluster becomes shared: partners on it see only the default project and their own projects, and new " +
        "projects follow the shared-cluster policy (isolated, restricted).",
    });
  }
  if (preview.holders_lose_whole_cluster_tools) {
    const holders = (preview.organizations_on_cluster ?? []).map((org) => org.name);
    const who = holders.length ? holders.join(", ") : "The organizations already on it";
    const verb = holders.length === 1 ? "loses" : "lose";
    out.push({
      tone: "warn",
      text: `${who} ${verb} the cluster's own tools (certificates, storage pools, networks), because another party now holds it.`,
    });
  }
  if (preview.share_audit && preview.share_audit.complete === false) {
    out.push({ tone: "warn", text: "The cluster could not be audited completely, so the bind will be refused. Try again shortly." });
  } else if (preview.share_audit && preview.share_audit.safe === false) {
    const count = preview.share_audit.findings.reduce((sum, finding) => sum + finding.count, 0);
    out.push({
      tone: "warn",
      text: `The cluster has ${count} privileged or unisolated ${count === 1 ? "resource" : "resources"}, so it cannot be shared until an administrator fixes ${count === 1 ? "it" : "them"}.`,
    });
  }
  for (const note of preview.notes ?? []) out.push({ tone: "info", text: note });
  return out;
}

interface OrganizationClustersDialogProps {
  organization: IntegrationOrganization;
  onOpenChange: (open: boolean) => void;
}

/**
 * The clusters a synced organization is bound to, for an administrator or a partner who runs it.
 *
 * A project its partners (or their keys, OrcaHub's included) create on a cluster another party
 * holds is recorded to the organization only where it is bound, which is what the refusal "None of
 * the organizations this key's creator ... is bound to cluster" points here for. A bind is previewed
 * first, because a second organization changes the cluster for everyone on it.
 */
export function OrganizationClustersDialog({ organization, onOpenChange }: OrganizationClustersDialogProps) {
  const queryClient = useQueryClient();
  const orgId = String(organization.id);
  const [target, setTarget] = useState("");
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const inFlight = useRef(false);

  useEffect(() => {
    setTarget("");
    setConfirmRemove(null);
    setSubmitError(null);
  }, [organization.id]);

  const candidates = useQuery({
    queryKey: ["organizations", orgId, "cluster-candidates"],
    queryFn: () => api.organizations.listClusterCandidates(orgId),
    staleTime: 15000,
  });
  const preview = useQuery({
    queryKey: ["organizations", orgId, "cluster-preview", target],
    queryFn: () => api.organizations.previewClusterBind(orgId, target),
    enabled: Boolean(target),
    staleTime: 0,
  });

  const bind = useMutation({
    mutationFn: (clusterId: string) => api.organizations.bindCluster(orgId, { cluster_id: clusterId }),
    onSuccess: async (_answer, clusterId) => {
      await reconcileMutationImpact(queryClient, "organization.write").catch(() => undefined);
      await queryClient.invalidateQueries({ queryKey: ["organizations", orgId] });
      toast({ title: "Cluster bound", description: `${clusterId} is bound to ${organization.name}.` });
      setTarget("");
      setSubmitError(null);
    },
    onError: (error: Error) => setSubmitError(error.message),
  });
  const unbind = useMutation({
    mutationFn: (clusterId: string) => api.organizations.unbindCluster(orgId, clusterId),
    onSuccess: async (_answer, clusterId) => {
      await reconcileMutationImpact(queryClient, "organization.write").catch(() => undefined);
      await queryClient.invalidateQueries({ queryKey: ["organizations", orgId] });
      toast({ title: "Cluster removed", description: `${clusterId} is no longer bound to ${organization.name}.` });
      setConfirmRemove(null);
      setSubmitError(null);
    },
    onError: (error: Error) => setSubmitError(error.message),
  });

  const run = async (action: () => Promise<unknown>) => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      await action();
    } catch {
      // The error sits next to the buttons (submitError).
    } finally {
      inFlight.current = false;
    }
  };

  const bindings = organization.cluster_bindings ?? (organization.cluster_ids ?? []).map((id) => ({ cluster_id: id, kind: "bound" as const }));
  const busy = bind.isPending || unbind.isPending;
  const answer = preview.data;
  const blocked =
    !answer || !answer.allowed || answer.already_bound === true ||
    answer.share_audit?.safe === false || answer.share_audit?.complete === false;
  const options = (candidates.data ?? []).map((clusterId) => ({ value: clusterId, label: clusterId }));

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0">
          <DialogTitle>Clusters</DialogTitle>
          <DialogDescription>
            The clusters {organization.name} is bound to. A project its partners or their keys create on a cluster is
            recorded to the organization only where it is bound.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="-mr-2 pr-2">
          <div className="grid gap-4 py-4">
            {bindings.length === 0 ? (
              <p className="text-sm text-muted-foreground">No cluster is bound to this organization yet.</p>
            ) : (
              <ul className="space-y-2" aria-label={`Clusters bound to ${organization.slug}`}>
                {bindings.map((binding) => (
                  <li key={binding.cluster_id} className="flex flex-wrap items-center gap-2 rounded-md border p-2 text-sm">
                    <div className="min-w-0 flex-1">
                      <p className="font-medium">
                        <OverflowText value={binding.cluster_id} label={`cluster bound to ${organization.slug}`} mono />
                      </p>
                      <p className="text-xs text-muted-foreground">{KIND_HINT[binding.kind]}</p>
                    </div>
                    <Badge variant="outline">{KIND_LABEL[binding.kind]}</Badge>
                    {binding.kind !== "purchase" &&
                      (confirmRemove === binding.cluster_id ? (
                        <>
                          <Button type="button" size="sm" variant="destructive" disabled={busy}
                            onClick={() => void run(() => unbind.mutateAsync(binding.cluster_id))}>
                            {unbind.isPending && <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden />}
                            Confirm Remove<span className="sr-only">: {binding.cluster_id}</span>
                          </Button>
                          <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setConfirmRemove(null)}>
                            Cancel
                          </Button>
                        </>
                      ) : (
                        <Button type="button" size="sm" variant="outline" disabled={busy}
                          onClick={() => { setConfirmRemove(binding.cluster_id); setSubmitError(null); }}>
                          Remove<span className="sr-only">: {binding.cluster_id}</span>
                        </Button>
                      ))}
                  </li>
                ))}
              </ul>
            )}

            <div className="space-y-2">
              <Label htmlFor="organization-cluster-add">Add Cluster</Label>
              <SearchableSelect
                id="organization-cluster-add"
                value={target}
                onValueChange={(value) => { setTarget(value); setSubmitError(null); }}
                options={options}
                placeholder={
                  candidates.isLoading ? "Reading clusters…" : options.length ? "Choose a cluster" : "No cluster can be added"
                }
                searchPlaceholder="Search clusters"
                emptyText="No cluster you may bind matches."
                disabled={candidates.isLoading || options.length === 0 || busy}
                ariaDescribedBy="organization-cluster-add-help"
                ariaInvalid={Boolean(candidates.error)}
              />
              <p id="organization-cluster-add-help" className="text-xs text-muted-foreground">
                {candidates.error
                  ? "The clusters you may bind could not be read; close and open this again."
                  : "Administrators may bind any registered cluster. A partner may bind a cluster its own party holds whole."}
              </p>
            </div>

            {target && (
              <div className="space-y-2 rounded-md border p-3" role="status" aria-live="polite">
                <p className="text-sm font-medium">Binding {target} would</p>
                {preview.isLoading ? (
                  <p className="text-sm text-muted-foreground">Checking what changes…</p>
                ) : preview.error ? (
                  <p className="text-sm text-muted-foreground">The preview could not be read: {(preview.error as Error).message}</p>
                ) : answer && !answer.allowed ? (
                  <p className="text-sm text-muted-foreground">Not allowed: {answer.refusal}</p>
                ) : answer?.already_bound ? (
                  <p className="text-sm text-muted-foreground">{target} is already bound to this organization.</p>
                ) : answer ? (
                  <ul className="space-y-1.5 text-sm">
                    {consequences(answer).map((item) => (
                      <li key={item.text} className="flex items-start gap-2">
                        {item.tone === "warn" ? (
                          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-pending" aria-hidden="true" />
                        ) : (
                          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                        )}
                        <span className="text-muted-foreground">{item.text}</span>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            )}
          </div>
        </DialogBody>
        {submitError && <DialogSubmitError title="The change was not made" message={submitError} className="shrink-0" />}
        <DialogFooter className="shrink-0 border-t pt-3">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          <Button type="button" disabled={!target || blocked || busy} onClick={() => void run(() => bind.mutateAsync(target))}>
            {bind.isPending && <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden />}
            Bind Cluster
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
