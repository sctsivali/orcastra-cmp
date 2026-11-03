"use client";

import * as React from "react";
import { Check, Copy, Dices, Eye, EyeOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { copyText } from "@/lib/clipboard";
import { generatePassword, passwordGroups, passwordProblem, type PasswordPolicy } from "@/lib/guest-credentials/password";
import { cn, plural } from "@/lib/utils";

export interface PasswordFieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  policy: PasswordPolicy;
  /** The server's refusal of the last submit, shown in place of the live check. */
  error?: string | null;
  /** An optional password may be left empty, and then no live check runs. */
  optional?: boolean;
  disabled?: boolean;
  inputRef?: React.Ref<HTMLInputElement>;
}

/**
 * The one password input of the Credentials panel.
 *
 * important: a text field masked with CSS, not type="password", for the reason the unseal dialog
 * gives (components/host-health/unseal-vault-dialog.tsx): browsers offer to save a password field
 * into a synced store whatever autocomplete says, and a guest's root password does not belong in
 * the operator's browser profile. The value lives only in the caller's state; nothing here copies
 * it anywhere but the clipboard, and only when asked.
 */
export function PasswordField({ id, label, value, onChange, policy, error, optional, disabled, inputRef }: PasswordFieldProps) {
  const [reveal, setReveal] = React.useState(false);
  const [generated, setGenerated] = React.useState(false);
  const [copied, setCopied] = React.useState<"copied" | "failed" | null>(null);
  const hintId = `${id}-hint`;
  const statusId = `${id}-status`;

  React.useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(null), 2500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const problem = value ? passwordProblem(value, policy) : null;
  const status = error
    ? error
    : !value
      ? optional ? "Optional. Leave it empty to log in with an SSH key only." : `${policy.min} characters at least.`
      : problem ?? "Long enough. The guest checks the rest when you submit.";

  const generate = () => {
    onChange(generatePassword());
    setGenerated(true);
    // Shown at once, so the person can read it and save it before it is set.
    setReveal(true);
  };

  const copy = async () => setCopied((await copyText(value)) ? "copied" : "failed");

  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <div className="flex gap-2">
        <Input
          ref={inputRef}
          id={id}
          name={id}
          type="text"
          value={value}
          onChange={(event) => {
            onChange(event.target.value);
            setGenerated(false);
          }}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
          spellCheck={false}
          data-1p-ignore="true"
          data-lpignore="true"
          data-bwignore="true"
          aria-invalid={error || problem ? true : undefined}
          aria-describedby={`${statusId} ${hintId}`}
          className={cn("min-w-0 flex-1 font-mono", !reveal && "[-webkit-text-security:disc]")}
          data-masked={reveal ? undefined : "true"}
          disabled={disabled}
        />
        <Button
          type="button"
          variant="outline"
          size="icon"
          onClick={() => setReveal((shown) => !shown)}
          aria-label={reveal ? "Hide password" : "Show password"}
          aria-pressed={reveal}
          disabled={disabled}
        >
          {reveal ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="sm" onClick={generate} disabled={disabled}>
          <Dices className="mr-1.5 h-4 w-4" aria-hidden="true" />
          Generate
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => void copy()} disabled={disabled || !value}>
          {copied === "copied" ? <Check className="mr-1.5 h-4 w-4" aria-hidden="true" /> : <Copy className="mr-1.5 h-4 w-4" aria-hidden="true" />}
          {copied === "copied" ? "Copied" : "Copy"}
        </Button>
        <span className="ml-auto text-xs tabular-nums text-muted-foreground">
          {plural(value.length, "character")} ({policy.min} to {policy.max} allowed)
        </span>
      </div>

      {reveal && generated && value && (
        // note: groups are drawn with margins, not spaces, so selecting and copying them by hand
        // still gives the password exactly as it will be set.
        <p className="text-sm">
          <span className="text-muted-foreground">Reads as </span>
          <span className="font-mono tracking-wide" translate="no">
            {passwordGroups(value).map((group, index) => (
              <span key={index} className="mr-2 inline-block last:mr-0">{group}</span>
            ))}
          </span>
        </p>
      )}

      <p id={statusId} aria-live="polite" className={cn("text-xs", error || problem ? "text-destructive" : "text-muted-foreground")}>
        {status}
        {copied === "copied" && " Copied to the clipboard."}
        {copied === "failed" && " The clipboard refused the copy: select the field and copy it by hand."}
      </p>
      <p id={hintId} className="text-xs text-muted-foreground">
        A guest login is a single factor, so it needs at least {policy.min} characters (NIST SP 800-63B). Length
        matters more than symbols, and there are no other rules: letters, digits, spaces and US keyboard symbols all
        work.
      </p>
    </div>
  );
}

export interface MustChangeCheckboxProps {
  id: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
}

/**
 * "Change at next login", with what it does to SSH spelled out.
 *
 * note: measured on real guests. Under PAM the expiry applies to key logins too: the first SSH
 * login asks for a new password, and a login with no terminal (a script, scp, `ssh host command`)
 * fails until one is chosen. A person who reads only the label would not expect that.
 */
export function MustChangeCheckbox({ id, checked, onCheckedChange, disabled }: MustChangeCheckboxProps) {
  return (
    <div className="flex items-start gap-2 rounded-md border p-3">
      <Checkbox
        id={id}
        checked={checked}
        onCheckedChange={(next) => onCheckedChange(next === true)}
        disabled={disabled}
        aria-describedby={`${id}-hint`}
        className="mt-0.5"
      />
      <div className="space-y-1">
        <Label htmlFor={id} className="cursor-pointer">Require a new password at the next login</Label>
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          This applies to SSH key logins as well. The first SSH login asks for a new password, and logins without a
          terminal (scripts, scp, ssh with a command) fail with &quot;Password change required but no TTY
          available&quot; until one has been chosen.
        </p>
      </div>
    </div>
  );
}
