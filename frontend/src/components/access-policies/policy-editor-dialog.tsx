"use client";

import { useId, useRef, useState } from "react";
import { Loader2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import type {
  AccessPolicy, IntegrationOrganization, PolicyCreateRequest, PolicyRule, PolicyUpdateRequest,
} from "@/types";
import { RuleEditor, type DraftRule, type PolicyCluster } from "./rule-editor";

interface PolicyEditorDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Absent for a new policy. */
  editPolicy?: AccessPolicy | null;
  clusters: PolicyCluster[];
  organizations: IntegrationOrganization[];
  /** The organization a new policy starts on, ex; the tenant's own when created from its Access dialog. */
  defaultOrganizationId?: number;
  /** What "Default Policy" does, worded for where the dialog opens. */
  defaultPolicyHint: string;
  onSubmit: (data: PolicyCreateRequest | PolicyUpdateRequest, isEdit: boolean) => void;
  isPending: boolean;
}

/**
 * Create or edit an access policy.
 *
 * important: keep the form state inside DialogContent, which Radix unmounts on close, so every
 * open starts from the policy (or the empty template). A parent opening the dialog through its
 * `open` prop never calls onOpenChange, so a reset there would not run.
 */
export function PolicyEditorDialog(props: PolicyEditorDialogProps) {
  const { open, onOpenChange } = props;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <PolicyEditorForm {...props} />
      </DialogContent>
    </Dialog>
  );
}

const NEW_RULE: Omit<PolicyRule, "id"> = {
  cluster_id: "*", project_name: "*", instance_name: "*", permissions: ["view"],
};

function PolicyEditorForm({
  onOpenChange, editPolicy, clusters, organizations, defaultOrganizationId, defaultPolicyHint,
  onSubmit, isPending,
}: PolicyEditorDialogProps) {
  const isEdit = !!editPolicy;
  const fieldId = useId();
  const nextKey = useRef(0);
  const draft = (rule: Omit<PolicyRule, "id">): DraftRule => ({
    cluster_id: rule.cluster_id,
    project_name: rule.project_name,
    instance_name: rule.instance_name,
    permissions: rule.permissions,
    key: `rule-${nextKey.current++}`,
  });

  const [name, setName] = useState(editPolicy?.name ?? "");
  const [description, setDescription] = useState(editPolicy?.description ?? "");
  const [orgId, setOrgId] = useState<string>(
    editPolicy ? String(editPolicy.organization_id)
    : defaultOrganizationId ? String(defaultOrganizationId)
    : "",
  );
  const [isDefault, setIsDefault] = useState(editPolicy?.is_default ?? false);
  const [rules, setRules] = useState<DraftRule[]>(() =>
    editPolicy?.rules?.length ? editPolicy.rules.map(draft) : [draft(NEW_RULE)],
  );

  const canSubmit = !!name.trim() && (isEdit || !!orgId) && rules.length > 0;

  const handleSubmit = () => {
    if (!canSubmit || isPending) return;
    const payloadRules = rules.map(({ cluster_id, project_name, instance_name, permissions }) => ({
      cluster_id, project_name, instance_name, permissions,
    }));
    if (isEdit) {
      onSubmit(
        { name: name.trim(), description: description.trim() || undefined, is_default: isDefault, rules: payloadRules },
        true,
      );
    } else {
      onSubmit(
        {
          name: name.trim(),
          description: description.trim() || undefined,
          organization_id: Number(orgId),
          is_default: isDefault,
          rules: payloadRules,
        },
        false,
      );
    }
  };

  return (
    <>
      <DialogHeader className="shrink-0">
        <DialogTitle>{isEdit ? "Edit Policy" : "Create Policy"}</DialogTitle>
        <DialogDescription>
          {isEdit
            ? "Update the policy rules and settings."
            : "Define cluster, project, and instance-level access rules for tenants."}
        </DialogDescription>
      </DialogHeader>

      <DialogBody className="-mr-2 pr-2">
        <div className="space-y-4 py-2">
          <div className="space-y-1.5">
            <Label htmlFor={`${fieldId}-name`} className="text-xs">Policy Name</Label>
            <Input
              id={`${fieldId}-name`}
              placeholder="for example, Production Read-Only"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={100}
              className="h-8 text-sm"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor={`${fieldId}-desc`} className="text-xs">Description (Optional)</Label>
            <Textarea
              id={`${fieldId}-desc`}
              placeholder="Describe what this policy allows…"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              maxLength={500}
              className="resize-none h-14 text-sm"
            />
          </div>

          {!isEdit && (
            <div className="space-y-1.5">
              <Label htmlFor={`${fieldId}-org`} className="text-xs">
                Organization <span className="text-destructive">(required)</span>
              </Label>
              <Select value={orgId || "__none__"} onValueChange={(v) => setOrgId(v === "__none__" ? "" : v)}>
                <SelectTrigger id={`${fieldId}-org`} className="h-8 text-sm">
                  <SelectValue placeholder="Select organization…" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__" disabled>Select organization…</SelectItem>
                  {organizations.map((org) => (
                    <SelectItem key={org.id} value={String(org.id)}>{org.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          <div className="flex items-center gap-3">
            <Switch
              id={`${fieldId}-default`}
              checked={isDefault}
              onCheckedChange={setIsDefault}
              aria-describedby={`${fieldId}-default-hint`}
            />
            <div>
              <Label htmlFor={`${fieldId}-default`} className="text-xs">Default Policy</Label>
              <p id={`${fieldId}-default-hint`} className="text-xs text-muted-foreground">{defaultPolicyHint}</p>
            </div>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-medium">Access Rules ({rules.length})</h3>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setRules((current) => [...current, draft(NEW_RULE)])}
                className="h-7 text-xs px-2"
              >
                <Plus className="h-3 w-3 mr-1" aria-hidden="true" />
                Add Rule
              </Button>
            </div>
            <div className="space-y-2">
              {rules.map((rule, idx) => (
                <RuleEditor
                  key={rule.key}
                  rule={rule}
                  index={idx}
                  clusters={clusters}
                  canRemove={rules.length > 1}
                  onUpdate={(next) => setRules((current) => current.map((r) => (r.key === rule.key ? next : r)))}
                  onRemove={() => setRules((current) =>
                    current.length > 1 ? current.filter((r) => r.key !== rule.key) : current)}
                />
              ))}
            </div>
          </div>
        </div>
      </DialogBody>

      <DialogFooter className="shrink-0 pt-3 border-t">
        <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Cancel</Button>
        <Button size="sm" onClick={handleSubmit} disabled={!canSubmit || isPending}>
          {isPending && <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" aria-hidden="true" />}
          {isEdit ? "Save Changes" : "Create Policy"}
        </Button>
      </DialogFooter>
    </>
  );
}
