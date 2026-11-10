"use client";

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Eye, EyeOff, Loader2, ShieldAlert } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { SEAL_STATUS_KEY, newerSealStatus, useUnsealVault, useVaultSealStatus } from "@/hooks/use-host-health";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { cn, plural } from "@/lib/utils";
import { APIError } from "@/types/api";
import type { VaultSealStatus } from "@/types/host-health";

// The same bounds the backend enforces (services/vault_unseal.py): a Shamir share is base64 (44
// characters) or hex (66). Checked here only so a paste mistake is named before a request.
const KEY_PATTERN = /^(?:[A-Za-z0-9+/]+={0,2}|[0-9a-fA-F]+)$/;
const KEY_MIN = 16;
const KEY_MAX = 256;

const OUTCOME_COPY = {
  accepted: (s: VaultSealStatus) => `Key accepted. ${s.progress ?? 0} of ${s.threshold ?? "?"} ${s.threshold === 1 ? "key" : "keys"} entered.`,
  duplicate: () => "Vault already holds this key for the current attempt. Enter a different key share.",
  reset: () => "Progress reset. Vault discarded the keys entered so far.",
} as const;

interface UnsealVaultDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** The refusal in words. A 429's message is rewritten generically by the API client. */
function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof APIError && error.status === 429) {
    const minutes = Math.max(1, Math.round((error.retryAfter ?? 600) / 60));
    return `Too many unseal submissions from this account. Try again in ${minutes}m.`;
  }
  return error instanceof Error ? error.message : fallback;
}

function errorStatus(error: unknown): VaultSealStatus | null {
  if (!(error instanceof APIError)) return null;
  const detail = error.detail as { status?: VaultSealStatus } | undefined;
  return detail && typeof detail === "object" && detail.status ? detail.status : null;
}

/**
 * Enter Vault unseal key shares one at a time. Several administrators can each enter their own
 * share from their own browser: the progress is Vault's, read every two seconds while this is open.
 *
 * important: the share lives only in this input's state. It is cleared after every submit,
 * successful or not, and whenever the dialog closes; it is never written to the query cache,
 * storage or a log.
 */
export function UnsealVaultDialog({ open, onOpenChange }: UnsealVaultDialogProps) {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  const statusQuery = useVaultSealStatus(open);
  const unseal = useUnsealVault();
  const [key, setKey] = React.useState("");
  const [reveal, setReveal] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [fieldError, setFieldError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  const [unsealedHere, setUnsealedHere] = React.useState(false);
  const [confirmReset, setConfirmReset] = React.useState(false);
  const [resetError, setResetError] = React.useState<string | null>(null);
  const [secureContext, setSecureContext] = React.useState(true);
  const submitInFlightRef = React.useRef(false);
  const resetInFlightRef = React.useRef(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    setKey("");
    setReveal(false);
    setError(null);
    setFieldError(null);
    setNotice(null);
    setResetError(null);
    setConfirmReset(false);
    if (open) {
      setUnsealedHere(false);
      setSecureContext(typeof window === "undefined" ? true : window.isSecureContext);
    }
  }, [open]);

  const status = statusQuery.data ?? null;
  const threshold = status?.threshold ?? null;
  const progress = status?.progress ?? 0;
  const unsealed = status?.state === "ok";

  const adopt = (next: VaultSealStatus | null) => {
    if (next) {
      queryClient.setQueryData<VaultSealStatus>([SEAL_STATUS_KEY, scopeKey], (current) => newerSealStatus(current, next));
    }
  };

  const validate = (value: string): string | null => {
    if (!value) return "Paste a key share first.";
    if (value.length < KEY_MIN || value.length > KEY_MAX || !KEY_PATTERN.test(value)) {
      return "That is not an unseal key. Paste one whole key share, with nothing before or after it.";
    }
    return null;
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitInFlightRef.current) return;
    const value = key.trim();
    const invalid = validate(value);
    if (invalid) {
      setFieldError(invalid);
      inputRef.current?.focus();
      return;
    }
    submitInFlightRef.current = true;
    setError(null);
    setFieldError(null);
    setNotice(null);
    try {
      const result = await unseal.mutateAsync({ key: value, nonce: status?.nonce ?? "" });
      if (result.outcome === "unsealed") {
        setUnsealedHere(true);
      } else if (result.outcome === "accepted" || result.outcome === "duplicate") {
        setNotice(OUTCOME_COPY[result.outcome](result));
      }
    } catch (err) {
      adopt(errorStatus(err));
      setError(errorMessage(err, "The key could not be submitted."));
    } finally {
      // important: cleared whatever the answer, in the field and in the mutation's own record of
      // what it sent. A retry is a re-paste, never a share left behind.
      setKey("");
      setReveal(false);
      unseal.reset();
      submitInFlightRef.current = false;
      inputRef.current?.focus();
    }
  };

  const handleReset = async () => {
    if (resetInFlightRef.current) return;
    resetInFlightRef.current = true;
    setResetError(null);
    try {
      await unseal.mutateAsync({ reset: true, nonce: status?.nonce ?? "" });
      setNotice(OUTCOME_COPY.reset());
      setError(null);
      setConfirmReset(false);
    } catch (err) {
      adopt(errorStatus(err));
      setResetError(errorMessage(err, "The attempt could not be reset."));
    } finally {
      resetInFlightRef.current = false;
    }
  };

  const percent = threshold ? Math.min(100, Math.round((progress / threshold) * 100)) : 0;
  const pending = unseal.isPending;

  return (
    <>
      <Dialog open={open} onOpenChange={(next) => !pending && onOpenChange(next)}>
        <DialogContent className="flex flex-col max-h-[calc(100dvh-2rem)] overflow-hidden sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Unseal Vault</DialogTitle>
            <DialogDescription>
              Enter one key share at a time. Vault unseals once it holds {threshold ?? "its threshold of"} key
              shares. Each share goes to Vault once and is never stored or logged by the dashboard.
            </DialogDescription>
          </DialogHeader>

          <DialogBody className="space-y-4">
            {statusQuery.isLoading && (
              <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                Reading Vault&apos;s status…
              </p>
            )}

            {!status && statusQuery.isError && (
              <DialogSubmitError
                title="Vault's status could not be read"
                message={statusQuery.error instanceof Error ? statusQuery.error.message : "Try again in a moment."}
              />
            )}

            {unsealed && (
              <Alert role="status">
                <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                <AlertDescription>
                  {unsealedHere
                    ? "Vault accepted the last key and is unsealed. Cluster data comes back within about a minute."
                    : "Vault is unsealed. Another administrator or the Vault host finished the unseal."}
                </AlertDescription>
              </Alert>
            )}

            {status && !unsealed && !status.unseal_available && (
              <Alert role="note">
                <ShieldAlert className="h-4 w-4" aria-hidden="true" />
                <AlertDescription>
                  {status.unseal_unavailable_reason ?? "Unsealing is not available right now."}
                </AlertDescription>
              </Alert>
            )}

            {status && !unsealed && status.unseal_available && (
              <form id="unseal-vault-form" className="space-y-4" onSubmit={handleSubmit} noValidate>
                {(!secureContext || status.insecure_transport) && (
                  <Alert role="note">
                    <ShieldAlert className="h-4 w-4" aria-hidden="true" />
                    <AlertDescription className="space-y-1">
                      {!secureContext && (
                        <p>This page is not served over HTTPS, so the key crosses the network unencrypted on its way to the dashboard.</p>
                      )}
                      {status.insecure_transport && (
                        <p>The dashboard reaches Vault over plain HTTP, so the key crosses that link unencrypted.</p>
                      )}
                    </AlertDescription>
                  </Alert>
                )}

                <div className="space-y-2">
                  <div className="flex items-baseline justify-between gap-3 text-sm">
                    <span className="font-medium">Unseal Progress</span>
                    <span className="tabular-nums text-muted-foreground">
                      {progress} of {threshold ?? "?"} {threshold === 1 ? "key" : "keys"} entered
                      {status.shares ? ` (${status.shares} exist)` : ""}
                    </span>
                  </div>
                  <Progress value={percent} aria-label={`Unseal progress: ${progress} of ${threshold ?? "unknown"} ${threshold === 1 ? "key" : "keys"}`} />
                  {status.nonce && (
                    <p className="text-xs text-muted-foreground">
                      Attempt <span className="font-mono break-all" translate="no">{status.nonce}</span>. Key holders
                      adding to the same attempt see the same ID.
                    </p>
                  )}
                </div>

                <div className="space-y-2">
                  <Label htmlFor="vault-unseal-key">Unseal Key Share</Label>
                  <div className="flex gap-2">
                    <Input
                      ref={inputRef}
                      id="vault-unseal-key"
                      name="vault-unseal-key"
                      // important: text masked with CSS rather than type="password". Browsers offer
                      // to save a password field into the account's synced password store whatever
                      // autocomplete says, and a key share must never end up there.
                      type="text"
                      value={key}
                      onChange={(e) => {
                        setKey(e.target.value);
                        if (fieldError) setFieldError(null);
                      }}
                      autoComplete="off"
                      autoCorrect="off"
                      autoCapitalize="off"
                      spellCheck={false}
                      data-1p-ignore="true"
                      data-lpignore="true"
                      placeholder="Paste one key share…"
                      aria-invalid={fieldError ? true : undefined}
                      aria-describedby="vault-unseal-key-hint"
                      className={cn("font-mono", !reveal && "[-webkit-text-security:disc]")}
                      data-masked={reveal ? undefined : "true"}
                      disabled={pending}
                    />
                    <Button
                      type="button"
                      variant="outline"
                      size="icon"
                      onClick={() => setReveal((r) => !r)}
                      aria-label={reveal ? "Hide key share" : "Show key share"}
                      aria-pressed={reveal}
                      disabled={pending}
                    >
                      {reveal ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
                    </Button>
                  </div>
                  <p
                    id="vault-unseal-key-hint"
                    aria-live="polite"
                    className={cn("text-xs", fieldError ? "text-destructive" : "text-muted-foreground")}
                  >
                    {fieldError ?? (
                      <>
                        One share as <span className="font-mono" translate="no">vault operator init</span> printed it.
                        The field clears after every submit.
                      </>
                    )}
                  </p>
                </div>

                <p className="text-sm text-muted-foreground" role="status" aria-live="polite">
                  {notice ?? ""}
                </p>
              </form>
            )}
          </DialogBody>

          <DialogSubmitError message={error} />

          <DialogFooter className="gap-2 sm:gap-0">
            {status && !unsealed && status.unseal_available && progress > 0 && (
              <Button variant="outline" onClick={() => setConfirmReset(true)} disabled={pending} className="sm:mr-auto">
                Reset Progress
              </Button>
            )}
            <Button variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>
              {/* Not "Close": the dialog's corner button already carries that name. */}
              {unsealed ? "Done" : "Cancel"}
            </Button>
            {status && !unsealed && status.unseal_available && (
              <Button type="submit" form="unseal-vault-form" disabled={pending}>
                {pending ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                    Submitting…
                  </>
                ) : (
                  "Submit Key"
                )}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <DestructiveConfirmDialog
        open={confirmReset}
        onOpenChange={(next) => {
          setConfirmReset(next);
          if (!next) setResetError(null);
        }}
        title="Reset Unseal Progress"
        description={`Vault discards the ${plural(progress, "key")} entered so far in this attempt, including shares other administrators entered. Everyone starts again from the first key.`}
        confirmLabel="Reset Progress"
        pendingLabel="Resetting…"
        pending={pending}
        onConfirm={() => void handleReset()}
        error={resetError}
      />
    </>
  );
}
