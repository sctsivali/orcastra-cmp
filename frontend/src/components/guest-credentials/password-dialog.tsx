"use client";

import * as React from "react";
import { CheckCircle2, Loader2, ShieldAlert } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useCredentialChange } from "@/hooks/use-guest-credentials";
import { credentialErrorCode, type GuestAccount, type GuestOverview, type GuestTarget, type PasswordResult } from "@/lib/guest-credentials/api";
import { credentialErrorMessage } from "@/lib/guest-credentials/copy";
import { passwordProblem, type PasswordPolicy } from "@/lib/guest-credentials/password";
import { ChangeError } from "./credentials-states";
import { MustChangeCheckbox, PasswordField } from "./password-field";

export function policyOf(overview: GuestOverview): PasswordPolicy {
  return { min: overview.password_policy.min_length, max: overview.password_policy.max_length, scheme: overview.guest.hash_default };
}

/** A refusal about the password itself belongs next to the field, not beside the buttons. */
export function isPasswordRefusal(error: unknown): boolean {
  const code = credentialErrorCode(error) ?? "";
  return code.startsWith("password_") || code === "guest_hash_unsupported";
}

interface PasswordDialogProps {
  /** Open while set. */
  account: GuestAccount | null;
  overview: GuestOverview;
  target: GuestTarget;
  writeReason?: string;
  onClose: () => void;
}

/**
 * Set or reset one account's password.
 *
 * important: the password lives only in this dialog's state. It is cleared when the guest confirms
 * it, when the dialog closes and when it unmounts, and it is never put in a toast, a query or a log.
 * An unconfirmed change keeps it, because it may now be the account's password.
 */
export function PasswordDialog({ account, overview, target, writeReason, onClose }: PasswordDialogProps) {
  const change = useCredentialChange(target);
  const [shown, setShown] = React.useState<GuestAccount | null>(account);
  const [password, setPassword] = React.useState("");
  const [mustChange, setMustChange] = React.useState(false);
  const [typed, setTyped] = React.useState("");
  const [fieldError, setFieldError] = React.useState<string | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [result, setResult] = React.useState<PasswordResult | null>(null);
  const [pending, setPending] = React.useState(false);
  const submitInFlightRef = React.useRef(false);
  const doneRef = React.useRef<HTMLButtonElement>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);

  // Keyed on the name: a background re-read hands over a new object for the same account, and that
  // must not wipe an answer the person is reading.
  const openName = account?.name ?? null;
  React.useEffect(() => {
    // Keep the last account through the close animation, and start every opening clean.
    if (account) setShown(account);
    setPassword("");
    setMustChange(false);
    setTyped("");
    setFieldError(null);
    setError(null);
    setResult(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openName]);

  // The form and its submit button are gone once the guest answers, so focus moves to Done.
  React.useEffect(() => {
    if (result) doneRef.current?.focus();
  }, [result]);

  const policy = policyOf(overview);
  const root = shown?.uid === 0;
  const confirmed = !root || typed.trim() === shown?.name;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitInFlightRef.current || !shown || writeReason) return;
    const problem = passwordProblem(password, policy);
    if (problem) {
      setFieldError(problem);
      inputRef.current?.focus();
      return;
    }
    if (!confirmed) return;
    submitInFlightRef.current = true;
    setPending(true);
    setError(null);
    setFieldError(null);
    try {
      const answer = await change({
        kind: "set-password", user: shown.name, password, mustChange: mustChange && overview.capabilities.expire,
      });
      if (answer) {
        setResult(answer);
        setPassword("");
      }
    } catch (failure) {
      if (isPasswordRefusal(failure)) {
        setFieldError(credentialErrorMessage(failure, "This password cannot be used."));
        inputRef.current?.focus();
      } else {
        setError(failure);
      }
    } finally {
      submitInFlightRef.current = false;
      setPending(false);
    }
  };

  return (
    <Dialog open={account !== null} onOpenChange={(open) => !open && !pending && onClose()}>
      <DialogContent className="flex flex-col max-h-[calc(100dvh-2rem)] overflow-hidden sm:max-w-lg">
        <DialogHeader className="pb-4 pr-8">
          <DialogTitle>Set Password</DialogTitle>
          <DialogDescription>
            For <span className="font-mono" translate="no">{shown?.name}</span> in {target.instanceName}.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {result ? (
            <Alert role="status">
              <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
              <AlertDescription className="space-y-1">
                <p>The password of {result.user} is set.</p>
                <p>Orcastra does not keep a copy. Keep yours in a password manager.</p>
                {result.must_change_applied && (
                  <p>{result.user} has to choose a new password at the next login, SSH key logins included.</p>
                )}
              </AlertDescription>
            </Alert>
          ) : (
            <form id="guest-password-form" className="space-y-4" onSubmit={submit} noValidate>
              {shown?.password === "empty" && (
                <p className="text-sm text-destructive">
                  This account has no password today, so anyone at the Serial or Graphics console can log in as it.
                </p>
              )}
              {root && (
                <Alert variant="warning" role="note">
                  <ShieldAlert className="h-4 w-4" aria-hidden="true" />
                  <AlertDescription>
                    With this password, {shown?.name} can log in on the Serial and Graphics consoles, and over SSH only
                    where the SSH server lets root log in with a password. Anyone who knows it controls this guest.
                  </AlertDescription>
                </Alert>
              )}
              <PasswordField
                id="guest-password"
                label="New Password"
                value={password}
                onChange={(next) => {
                  setPassword(next);
                  if (fieldError) setFieldError(null);
                }}
                policy={policy}
                error={fieldError}
                disabled={pending}
                inputRef={inputRef}
              />
              <p className="text-xs text-muted-foreground">Copy or save it before you set it: it is not shown again.</p>
              {overview.capabilities.expire && (
                <MustChangeCheckbox id="guest-password-expire" checked={mustChange} onCheckedChange={setMustChange} disabled={pending} />
              )}
              {root && (
                <div className="space-y-2">
                  <Label htmlFor="guest-root-confirm">Type {shown?.name} to confirm</Label>
                  <Input
                    id="guest-root-confirm"
                    value={typed}
                    onChange={(event) => setTyped(event.target.value)}
                    placeholder={shown?.name}
                    autoComplete="off"
                    spellCheck={false}
                    className="font-mono"
                    disabled={pending}
                  />
                </div>
              )}
            </form>
          )}
        </DialogBody>

        {writeReason && !result && <p className="mt-4 text-sm text-muted-foreground">{writeReason}</p>}
        <ChangeError error={error} fallback="The password could not be set." />

        <DialogFooter className="gap-2 pt-4 sm:gap-0">
          <Button ref={doneRef} variant="outline" onClick={onClose} disabled={pending}>{result ? "Done" : "Cancel"}</Button>
          {!result && (
            <Button type="submit" form="guest-password-form" disabled={pending || !confirmed || !!writeReason || !password}>
              {pending && <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />}
              {pending ? "Setting…" : "Set Password"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
