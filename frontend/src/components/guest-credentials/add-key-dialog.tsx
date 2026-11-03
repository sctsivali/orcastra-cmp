"use client";

import * as React from "react";
import { ChevronDown, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useCredentialChange } from "@/hooks/use-guest-credentials";
import { credentialErrorCode, isOutcomeUnknown, type GuestTarget } from "@/lib/guest-credentials/api";
import { credentialErrorMessage, OUTCOME_UNKNOWN, warningText } from "@/lib/guest-credentials/copy";
import { precheckPublicKey } from "@/lib/guest-credentials/public-key";
import { cn } from "@/lib/utils";
import type { SectionNotice } from "./account-dialogs";
import { ChangeError } from "./credentials-states";
import { KeyGuides } from "./key-guides";
import { KeyPasteField } from "./key-paste-field";

const FIELD_CODES = new Set(["invalid_public_key", "private_key_pasted"]);

interface AddKeyDialogProps {
  open: boolean;
  user: string;
  target: GuestTarget;
  writeReason?: string;
  onClose: () => void;
  /** Called with what to say once the guest answered; the dialog closes itself first. */
  onDone: (notice: SectionNotice) => void;
}

/** Paste one public key into an account's authorized_keys. The key is made elsewhere, never here. */
export function AddKeyDialog({ open, user, target, writeReason, onClose, onDone }: AddKeyDialogProps) {
  const change = useCredentialChange(target);
  const [publicKey, setPublicKey] = React.useState("");
  const [fieldError, setFieldError] = React.useState<string | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [pending, setPending] = React.useState(false);
  const [guideOpen, setGuideOpen] = React.useState(false);
  const submitInFlightRef = React.useRef(false);

  React.useEffect(() => {
    setPublicKey("");
    setFieldError(null);
    setError(null);
  }, [open]);

  const check = precheckPublicKey(publicKey);
  const blocked = check.state === "empty" || check.state === "private";

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitInFlightRef.current || blocked || writeReason) return;
    submitInFlightRef.current = true;
    setPending(true);
    setError(null);
    setFieldError(null);
    try {
      const answer = await change({ kind: "add-key", user, publicKey: publicKey.trim() });
      if (!answer) return;
      setPublicKey("");
      onClose();
      const lead = answer.already_present ? `This key was already installed for ${user}.` : `The key was added for ${user}.`;
      onDone({
        tone: answer.warnings.length ? "warning" : "success",
        text: [lead, ...answer.warnings.map(warningText)].join(" "),
      });
    } catch (failure) {
      const code = credentialErrorCode(failure) ?? "";
      if (isOutcomeUnknown(failure)) {
        setPublicKey("");
        onClose();
        onDone({ tone: "warning", text: OUTCOME_UNKNOWN });
      } else if (FIELD_CODES.has(code)) {
        if (code === "private_key_pasted") setPublicKey("");
        setFieldError(credentialErrorMessage(failure, "This key cannot be added."));
      } else {
        setError(failure);
      }
    } finally {
      submitInFlightRef.current = false;
      setPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !pending && onClose()}>
      <DialogContent className="flex flex-col max-h-[calc(100dvh-2rem)] overflow-hidden sm:max-w-xl">
        <DialogHeader className="pb-4 pr-8">
          <DialogTitle>Add SSH Key</DialogTitle>
          <DialogDescription>
            For <span className="font-mono" translate="no">{user}</span> in {target.instanceName}. Only the public half
            of a key belongs here.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <form id="guest-add-key-form" className="space-y-4" onSubmit={submit} noValidate>
            <KeyPasteField
              id="guest-add-key"
              label="Public Key"
              value={publicKey}
              onChange={(next) => {
                setPublicKey(next);
                if (fieldError) setFieldError(null);
              }}
              error={fieldError}
              disabled={pending}
            />
          </form>
          <Collapsible open={guideOpen} onOpenChange={setGuideOpen} className="rounded-md border">
            <CollapsibleTrigger asChild>
              <Button type="button" variant="ghost" className="w-full justify-between px-3">
                No key yet? How to make one
                <ChevronDown className={cn("h-4 w-4 transition-transform motion-reduce:transition-none", guideOpen && "rotate-180")} aria-hidden="true" />
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="border-t p-3">
              <KeyGuides />
            </CollapsibleContent>
          </Collapsible>
        </DialogBody>

        {writeReason && <p className="mt-4 text-sm text-muted-foreground">{writeReason}</p>}
        <ChangeError error={error} fallback="The key could not be added." />

        <DialogFooter className="gap-2 pt-4 sm:gap-0">
          <Button variant="outline" onClick={onClose} disabled={pending}>Cancel</Button>
          <Button type="submit" form="guest-add-key-form" disabled={pending || blocked || !!writeReason}>
            {pending && <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />}
            {pending ? "Adding…" : "Add Key"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
