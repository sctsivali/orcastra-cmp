"use client";

import { useRef, useState } from "react";
import { Loader2 } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { ConsentCapabilityBlock } from "@/components/extensions/consent-capability-block";
import { ConsentScopeBlock } from "@/components/extensions/consent-scope-block";
import type { ExtensionInstallPreview } from "@/types/api";

/**
 * The screen where an operator hands a third party access to their infrastructure.
 *
 * Six things are shown before the confirm enables, and the order is the argument: who wrote it,
 * what it may call, where it may reach, what it will be told, what it can never do, and that the
 * credential appears once.
 *
 * The consent digest and version travel back with the request. The server recomputes the digest
 * from what is stored and refuses if it moved, which closes publish-something-benign, wait for
 * this dialog to render, then change the manifest.
 */
export function InstallConsentDialog({
  preview,
  open,
  onOpenChange,
  onConfirm,
  pending,
  error,
}: {
  preview: ExtensionInstallPreview | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  pending: boolean;
  error?: string | null;
}) {
  const [acknowledged, setAcknowledged] = useState(false);
  // important: a ref, claimed before any await. `disabled` from state does not reach the DOM
  // until the next render, so two clicks in one tick both pass it. Eight forms in this repo
  // were fixed for exactly this.
  const submitting = useRef(false);

  if (!preview) return null;

  const guest = preview.capabilities.filter((c) => c.reaches_inside_a_guest);
  const needsAcknowledgement = guest.length > 0;
  const nothingToGrant = preview.grantable_cluster_ids.length === 0;
  const blocked = pending || nothingToGrant || (needsAcknowledgement && !acknowledged);

  const handleConfirm = () => {
    if (submitting.current) return;
    submitting.current = true;
    try {
      onConfirm();
    } finally {
      // Released on the next tick rather than never, so a failed attempt can be retried.
      setTimeout(() => {
        submitting.current = false;
      }, 0);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (pending) return;
        if (!next) setAcknowledged(false);
        onOpenChange(next);
      }}
    >
      <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="pr-8">
          <DialogTitle>Install {preview.extension.name}</DialogTitle>
          <DialogDescription>
            Version {preview.version}, published by {preview.extension.publisher}, into{" "}
            {preview.organization_slug}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          <p className="text-sm text-muted-foreground">
            This add-on runs on its author&apos;s own infrastructure. It calls the Orcastra API
            with the credential issued below. No add-on code runs inside this dashboard.
          </p>

          <ConsentCapabilityBlock capabilities={preview.capabilities} />

          <ConsentScopeBlock
            requested={String(preview.requested_scope?.permission ?? "read")}
            granted={preview.grantable_cluster_ids}
            refused={preview.refused_cluster_ids}
          />

          <section className="space-y-1">
            <h4 className="text-sm font-semibold">What It Can Never Do</h4>
            <p className="text-sm text-muted-foreground">
              Run code in this dashboard, read your browser session, open a panel inside these
              pages, reach a cluster outside the granted list, or hold more access than you hold
              yourself.
            </p>
          </section>

          <Alert>
            <AlertDescription>
              The credential and the webhook signing secret appear once, on the next screen. Have
              somewhere ready to paste them.
            </AlertDescription>
          </Alert>

          {nothingToGrant ? (
            <Alert variant="destructive">
              <AlertDescription>
                None of your clusters are bound to this organization, so there is nothing to
                grant. Bind a cluster first.
              </AlertDescription>
            </Alert>
          ) : null}

          {needsAcknowledgement ? (
            <div className="flex items-start gap-2 rounded-lg border border-destructive/40 p-3">
              <Checkbox
                id="guest-acknowledgement"
                checked={acknowledged}
                onCheckedChange={(value) => setAcknowledged(value === true)}
                className="mt-0.5"
              />
              <Label
                htmlFor="guest-acknowledgement"
                className="text-sm font-normal leading-snug text-muted-foreground"
              >
                I understand this add-on will be able to act inside running instances in{" "}
                {preview.organization_slug}.
              </Label>
            </div>
          ) : null}

          {error ? (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancel
          </Button>
          <Button onClick={handleConfirm} disabled={blocked}>
            {pending ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                Installing
              </>
            ) : (
              "Install"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
