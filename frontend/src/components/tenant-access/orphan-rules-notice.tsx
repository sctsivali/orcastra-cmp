"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Trash2 } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { toast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { formatDateTime, plural } from "@/lib/utils";
import { APIError, type OrphanRule } from "@/types/api";

// A rule naming a project or instance deleted outside Orcastra (lxc delete) grants its tenants
// whatever is created under that name next. The backend audit only reports such rules
// (services/tenant_rule_audit.py); removing one is a partner's call, and the backend reads the
// cluster again before it does.

const ORPHAN_KEY = ["tenant-access-orphan-rules"] as const;

function target(rule: OrphanRule): string {
  const where = `${rule.project_name} on ${rule.cluster_id}`;
  return rule.instance_name === "*" ? `project ${where}` : `instance ${rule.instance_name} in ${where}`;
}

export function OrphanRulesNotice() {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ORPHAN_KEY,
    queryFn: () => api.tenantAccess.listOrphanRules(),
    staleTime: 60_000,
  });
  const [pendingRule, setPendingRule] = React.useState<OrphanRule | null>(null);
  const [shownRule, setShownRule] = React.useState<OrphanRule | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const remove = useMutation({
    mutationFn: (rule: OrphanRule) => api.tenantAccess.removeOrphanRule(rule.rule_id),
    onSuccess: (_result, rule) => {
      toast({ title: "Access rule removed", description: `The rule for ${target(rule)} no longer grants anything.` });
      setPendingRule(null);
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
    },
    onError: (failure) => {
      // 409: what the rule names exists again; 503: the cluster did not answer. Both keep the rule.
      setError(failure instanceof APIError ? failure.message : "The rule could not be removed. Try again.");
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
    },
  });

  const rules = data?.rules ?? [];
  if (rules.length === 0) return null;

  return (
    <Alert variant="warning" role="status" data-testid="orphan-rules">
      <AlertTriangle className="h-4 w-4" aria-hidden="true" />
      <AlertDescription className="space-y-3">
        <div className="space-y-1">
          <p className="font-medium">
            {plural(rules.length, "access rule names", "access rules name")} something that no longer exists
          </p>
          <p className="text-muted-foreground">
            It was deleted outside Orcastra. Anything created later under the same name would be given to the
            tenants on these rules, so remove a rule unless that is intended.
          </p>
        </div>
        <ul className="space-y-2">
          {rules.map((rule) => (
            <li key={rule.rule_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-card px-3 py-2">
              <div className="min-w-0">
                <p className="font-medium break-words">{rule.policy_name}</p>
                <p className="text-xs text-muted-foreground break-words">
                  {target(rule)}, {rule.organization_name}. Missing since {formatDateTime(rule.first_missing_at)}.
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                aria-label={`Remove the rule for ${target(rule)} from ${rule.policy_name}`}
                onClick={() => {
                  setError(null);
                  setPendingRule(rule);
                  setShownRule(rule);
                }}
              >
                <Trash2 className="mr-2 h-4 w-4" aria-hidden="true" />
                Remove Rule
              </Button>
            </li>
          ))}
        </ul>
      </AlertDescription>
      <DestructiveConfirmDialog
        open={pendingRule !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRule(null);
        }}
        title="Remove this access rule?"
        description={
          shownRule
            ? `The rule in ${shownRule.policy_name} for ${target(shownRule)} will be removed. The cluster is checked again first, and the rule is kept if what it names exists again.`
            : ""
        }
        confirmLabel="Remove Rule"
        pendingLabel="Removing…"
        pending={remove.isPending}
        onConfirm={() => pendingRule && remove.mutate(pendingRule)}
        error={error}
      />
    </Alert>
  );
}
