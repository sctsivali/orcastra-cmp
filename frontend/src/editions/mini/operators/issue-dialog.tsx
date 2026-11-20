"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api } from "@/lib/api";
import { miniApi, type IssuedBundle, type OperatorRole } from "../api";
import { BundleReveal } from "./bundle-reveal";

const NO_ORG = "none";

/** Issue an operator's first certificate. Partners issue Tenants of their own organizations. */
export function IssueDialog({ open, onOpenChange, isAdmin, onIssued }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  isAdmin: boolean;
  onIssued: () => void;
}) {
  const [role, setRole] = useState<OperatorRole>("tenant");
  const [orgId, setOrgId] = useState<string>(NO_ORG);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [issued, setIssued] = useState<{ bundle: IssuedBundle; email: string } | null>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const orgs = useQuery({ queryKey: ["mini", "organizations"], queryFn: () => api.organizations.list(), enabled: open });

  useEffect(() => {
    if (open) {
      setRole("tenant");
      setOrgId(NO_ORG);
      setError(null);
      setIssued(null);
      setBusy(false);
    }
  }, [open]);

  const needsOrg = !isAdmin;
  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const email = String(form.get("email") || "").trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      setError("Enter the operator's email address, for example name@example.com.");
      emailRef.current?.focus();
      return;
    }
    if (needsOrg && orgId === NO_ORG) {
      setError("Choose the organization the Tenant belongs to.");
      return;
    }
    const days = Number(form.get("ttl_days") || 0);
    setBusy(true);
    setError(null);
    try {
      const bundle = await miniApi.issueOperator({
        email,
        display_name: String(form.get("display_name") || "").trim() || undefined,
        role: isAdmin ? role : "tenant",
        organization_id: orgId === NO_ORG ? null : Number(orgId),
        ttl_days: days > 0 ? days : undefined,
      });
      setIssued({ bundle, email });
      onIssued();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The certificate could not be issued.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next); }}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{issued ? "Certificate Issued" : "Issue Certificate"}</DialogTitle>
          <DialogDescription>
            {issued
              ? `The first certificate for ${issued.email}.`
              : "Creates an operator and their first sign-in certificate as a password-protected .p12 file."}
          </DialogDescription>
        </DialogHeader>
        {issued ? (
          <>
            <BundleReveal bundle={issued.bundle} holder={issued.email} />
            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>Done</Button>
            </DialogFooter>
          </>
        ) : (
          <form onSubmit={submit} className="space-y-4" noValidate>
            <div className="space-y-2">
              <Label htmlFor="issue-email">Email</Label>
              <Input ref={emailRef} id="issue-email" name="email" type="email" autoComplete="off" spellCheck={false}
                required placeholder="name@example.com…" />
              <p className="text-xs text-muted-foreground">How organizations, keys and the audit log know this person. It cannot be changed later.</p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="issue-name">Display Name (Optional)</Label>
              <Input id="issue-name" name="display_name" type="text" autoComplete="off" maxLength={255} placeholder="Jane Doe…" />
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="issue-role">Role</Label>
                {isAdmin ? (
                  <Select value={role} onValueChange={(v) => setRole(v as OperatorRole)}>
                    <SelectTrigger id="issue-role"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="tenant">Tenant</SelectItem>
                      <SelectItem value="partner">Partner</SelectItem>
                      <SelectItem value="admin">Administrator</SelectItem>
                    </SelectContent>
                  </Select>
                ) : (
                  <Input id="issue-role" value="Tenant" readOnly aria-readonly="true" />
                )}
              </div>
              <div className="space-y-2">
                <Label htmlFor="issue-days">Valid For (Days, Optional)</Label>
                <Input id="issue-days" name="ttl_days" type="number" inputMode="numeric" min={1} max={3650}
                  autoComplete="off" placeholder="365…" />
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="issue-org">Organization{needsOrg ? "" : " (Optional)"}</Label>
              <Select value={orgId} onValueChange={setOrgId}>
                <SelectTrigger id="issue-org"><SelectValue placeholder="Choose an organization…" /></SelectTrigger>
                <SelectContent>
                  {!needsOrg && <SelectItem value={NO_ORG}>No organization</SelectItem>}
                  {(orgs.data ?? []).filter((o) => o.is_active).map((o) => (
                    <SelectItem key={o.id} value={String(o.id)}>{o.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                The operator joins it as a member. A Tenant issued for an organization is managed by its Partners.
              </p>
            </div>
            {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>Cancel</Button>
              <Button type="submit" disabled={busy}>
                {busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />}
                {busy ? "Issuing…" : "Issue Certificate"}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
