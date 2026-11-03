"use client";

import * as React from "react";
import { ShieldAlert } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { looksLikePrivateKey, precheckPublicKey } from "@/lib/guest-credentials/public-key";
import { cn } from "@/lib/utils";

export interface KeyPasteFieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  /** The server's refusal of the last submit, shown in place of the live hint. */
  error?: string | null;
  optional?: boolean;
  disabled?: boolean;
  /** Told when a private key was caught and thrown away, so the caller can drop it everywhere. */
  onPrivateKey?: () => void;
}

/**
 * Where a public key is pasted. A private key never reaches the field: it is caught on paste, the
 * field is emptied, and nothing is sent anywhere.
 */
export function KeyPasteField({ id, label, value, onChange, error, optional, disabled, onPrivateKey }: KeyPasteFieldProps) {
  const [caughtPrivate, setCaughtPrivate] = React.useState(false);
  const hintId = `${id}-hint`;
  const check = precheckPublicKey(value);

  const reject = () => {
    onChange("");
    setCaughtPrivate(true);
    onPrivateKey?.();
  };

  const hint = error
    ? error
    : check.state === "hint"
      ? check.hint
      : check.state === "plausible"
        ? "Looks like a public key. The guest's parser checks it when you submit."
        : optional
          ? "Optional. Paste the one line from a .pub file."
          : "Paste the one line from a .pub file, starting with its type, such as ssh-ed25519.";

  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Textarea
        id={id}
        name={id}
        value={value}
        onPaste={(event) => {
          // important: decided on the clipboard text, before the browser puts it in the field, so a
          // private key never sits in the DOM or in the field's undo history.
          if (looksLikePrivateKey(event.clipboardData.getData("text"))) {
            event.preventDefault();
            reject();
          }
        }}
        onChange={(event) => {
          const next = event.target.value;
          // Typing or a drop reaches here without a paste event.
          if (looksLikePrivateKey(next)) return reject();
          if (next.trim()) setCaughtPrivate(false);
          onChange(next);
        }}
        rows={4}
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        spellCheck={false}
        data-1p-ignore="true"
        data-lpignore="true"
        placeholder="ssh-ed25519 AAAAC3Nza… you@laptop"
        aria-invalid={error || check.state === "hint" ? true : undefined}
        aria-describedby={hintId}
        className="break-all font-mono text-xs"
        disabled={disabled}
      />
      <p id={hintId} aria-live="polite" className={cn("text-xs", error ? "text-destructive" : "text-muted-foreground")}>
        {hint}
      </p>
      {caughtPrivate && (
        <Alert variant="destructive">
          <ShieldAlert className="h-4 w-4" aria-hidden="true" />
          <AlertTitle>That was a private key</AlertTitle>
          <AlertDescription className="space-y-1">
            <p>It was not saved and not sent anywhere, and the field was cleared.</p>
            <p>
              Paste the public key instead: the file ending in .pub. If you pasted this private key anywhere else,
              treat it as exposed and create a new one.
            </p>
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}
