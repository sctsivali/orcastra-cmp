"use client";

import * as React from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useCredentialChange } from "@/hooks/use-guest-credentials";
import { credentialErrorCode, type CreateUserResult, type GuestOverview, type GuestTarget } from "@/lib/guest-credentials/api";
import { credentialErrorMessage } from "@/lib/guest-credentials/copy";
import { passwordProblem } from "@/lib/guest-credentials/password";
import { cn } from "@/lib/utils";
import { CreateUserResults } from "./create-user-results";
import { ChangeError } from "./credentials-states";
import { KeyPasteField } from "./key-paste-field";
import { isPasswordRefusal, policyOf } from "./password-dialog";
import { MustChangeCheckbox, PasswordField } from "./password-field";

// The backend's rule (services/guest_credentials/accounts.py NEW_NAME), checked here for the hint.
const NAME_PATTERN = /^[a-z_][a-z0-9_-]{0,31}$/;
const NAME_CODES = new Set(["invalid_username", "reserved_username", "user_exists"]);
const KEY_CODES = new Set(["invalid_public_key", "private_key_pasted"]);

function nameProblem(name: string, taken: readonly string[]): string | null {
  if (!name) return null;
  if (name.length > 32) return "At most 32 characters.";
  if (/[A-Z]/.test(name)) return "Use lowercase letters only.";
  if (!/^[a-z_]/.test(name)) return "Start with a lowercase letter or _.";
  if (!NAME_PATTERN.test(name)) return "Only lowercase letters, digits, - and _ are allowed.";
  if (name.endsWith("-")) return "It cannot end with -.";
  if (taken.includes(name)) return "An account with this name already exists.";
  return null;
}

interface CreateUserDialogProps {
  open: boolean;
  overview: GuestOverview;
  target: GuestTarget;
  writeReason?: string;
  onClose: () => void;
}

/**
 * Create a local account, with sudo, a password and a key as optional steps. Each step reports on
 * its own: the account can exist while a later step did not take.
 *
 * important: the password and the key live only in this dialog's state and are cleared once the
 * guest answers, when the dialog closes and when it unmounts.
 */
export function CreateUserDialog({ open, overview, target, writeReason, onClose }: CreateUserDialogProps) {
  const change = useCredentialChange(target);
  const [name, setName] = React.useState("");
  const [sudo, setSudo] = React.useState(false);
  const [password, setPassword] = React.useState("");
  const [mustChange, setMustChange] = React.useState(false);
  const [publicKey, setPublicKey] = React.useState("");
  const [fieldErrors, setFieldErrors] = React.useState<{ name?: string; password?: string; key?: string }>({});
  const [error, setError] = React.useState<unknown>(null);
  const [result, setResult] = React.useState<CreateUserResult | null>(null);
  const [pending, setPending] = React.useState(false);
  const submitInFlightRef = React.useRef(false);
  const doneRef = React.useRef<HTMLButtonElement>(null);
  const nameRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    setName("");
    setSudo(false);
    setPassword("");
    setMustChange(false);
    setPublicKey("");
    setFieldErrors({});
    setError(null);
    setResult(null);
  }, [open]);

  const { capabilities } = overview;
  // The form and its submit button are gone once the guest answers, so focus moves to Done.
  React.useEffect(() => {
    if (result) doneRef.current?.focus();
  }, [result]);

  const policy = policyOf(overview);
  const group = capabilities.sudo_group ?? "sudo";
  const taken = React.useMemo(() => overview.accounts.map((account) => account.name), [overview.accounts]);
  const liveNameProblem = nameProblem(name, taken);
  const asksRootPassword = overview.accounts.some((account) => account.sudo === "rootpw");

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitInFlightRef.current || writeReason) return;
    const errors = {
      name: name ? liveNameProblem ?? undefined : "Choose a name for the account.",
      password: password ? passwordProblem(password, policy) ?? undefined : undefined,
    };
    if (errors.name || errors.password) {
      setFieldErrors(errors);
      if (errors.name) nameRef.current?.focus();
      return;
    }
    submitInFlightRef.current = true;
    setPending(true);
    setError(null);
    setFieldErrors({});
    try {
      const answer = await change({
        kind: "create-user",
        body: {
          name,
          sudo: sudo && capabilities.sudo_group !== null,
          password: password || undefined,
          must_change: Boolean(password) && mustChange && capabilities.expire,
          public_key: publicKey.trim() || undefined,
        },
      });
      if (answer) {
        setResult(answer);
        setPassword("");
        setPublicKey("");
      }
    } catch (failure) {
      const code = credentialErrorCode(failure) ?? "";
      const message = credentialErrorMessage(failure, "The account could not be created.");
      if (NAME_CODES.has(code)) setFieldErrors({ name: message });
      else if (isPasswordRefusal(failure)) setFieldErrors({ password: message });
      else if (KEY_CODES.has(code)) {
        if (code === "private_key_pasted") setPublicKey("");
        setFieldErrors({ key: message });
      } else setError(failure);
    } finally {
      submitInFlightRef.current = false;
      setPending(false);
    }
  };

  const sudoHelp = !capabilities.sudo_group
    ? "This guest has neither a sudo nor a wheel group, so administrator rights cannot be given here."
    : [
        `Adds the account to the ${group} group, which lets it run any command as root with sudo.`,
        asksRootPassword ? "On this guest sudo asks for root's password rather than the account's own, so the group alone may not be enough." : "",
        capabilities.sudo_installed ? "" : "sudo is not installed on this guest, so this has no effect until it is.",
      ].filter(Boolean).join(" ");
  const nameStatus = fieldErrors.name ?? liveNameProblem;

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !pending && onClose()}>
      <DialogContent className="flex flex-col max-h-[calc(100dvh-2rem)] overflow-hidden sm:max-w-lg">
        <DialogHeader className="pb-4 pr-8">
          <DialogTitle>Create User</DialogTitle>
          <DialogDescription>A local account in {target.instanceName}, with its own home folder.</DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {result ? (
            <CreateUserResults result={result} group={group} />
          ) : (
            <form id="guest-create-user-form" className="space-y-5" onSubmit={submit} noValidate>
              <div className="space-y-2">
                <Label htmlFor="guest-user-name">Account Name</Label>
                <Input
                  ref={nameRef}
                  id="guest-user-name"
                  name="guest-user-name"
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value);
                    if (fieldErrors.name) setFieldErrors((current) => ({ ...current, name: undefined }));
                  }}
                  autoComplete="off"
                  autoCapitalize="off"
                  autoCorrect="off"
                  spellCheck={false}
                  placeholder="for example, deploy…"
                  aria-invalid={nameStatus ? true : undefined}
                  aria-describedby="guest-user-name-hint"
                  className="font-mono"
                  disabled={pending}
                />
                <p id="guest-user-name-hint" aria-live="polite" className={cn("text-xs", nameStatus ? "text-destructive" : "text-muted-foreground")}>
                  {nameStatus ?? "1 to 32 characters: lowercase letters, digits, - or _, starting with a letter or _."}
                </p>
              </div>

              <div className="flex items-start gap-2 rounded-md border p-3">
                <Checkbox
                  id="guest-user-sudo"
                  checked={sudo}
                  onCheckedChange={(next) => setSudo(next === true)}
                  disabled={pending || !capabilities.sudo_group}
                  aria-describedby="guest-user-sudo-hint"
                  className="mt-0.5"
                />
                <div className="space-y-1">
                  <Label htmlFor="guest-user-sudo" className="cursor-pointer">Make this account an administrator (sudo)</Label>
                  <p id="guest-user-sudo-hint" className="text-xs text-muted-foreground">{sudoHelp}</p>
                </div>
              </div>

              {capabilities.set_password && (
                <PasswordField
                  id="guest-user-password"
                  label="Password (Optional)"
                  value={password}
                  onChange={(next) => {
                    setPassword(next);
                    if (fieldErrors.password) setFieldErrors((current) => ({ ...current, password: undefined }));
                  }}
                  policy={policy}
                  error={fieldErrors.password}
                  optional
                  disabled={pending}
                />
              )}
              {capabilities.expire && password && (
                <MustChangeCheckbox id="guest-user-expire" checked={mustChange} onCheckedChange={setMustChange} disabled={pending} />
              )}

              <KeyPasteField
                id="guest-user-key"
                label="SSH Public Key (Optional)"
                value={publicKey}
                onChange={(next) => {
                  setPublicKey(next);
                  if (fieldErrors.key) setFieldErrors((current) => ({ ...current, key: undefined }));
                }}
                error={fieldErrors.key}
                optional
                disabled={pending}
              />

              {!password && !publicKey.trim() && (
                <p className="text-xs text-muted-foreground">
                  Without a password or a key, nobody can log in as this account yet. Either can be added later.
                </p>
              )}
            </form>
          )}
        </DialogBody>

        {writeReason && !result && <p className="mt-4 text-sm text-muted-foreground">{writeReason}</p>}
        <ChangeError error={error} fallback="The account could not be created." />

        <DialogFooter className="gap-2 pt-4 sm:gap-0">
          <Button ref={doneRef} variant="outline" onClick={onClose} disabled={pending}>{result ? "Done" : "Cancel"}</Button>
          {!result && (
            <Button type="submit" form="guest-create-user-form" disabled={pending || !!writeReason || !capabilities.create_user}>
              {pending && <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />}
              {pending ? "Creating…" : "Create User"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
