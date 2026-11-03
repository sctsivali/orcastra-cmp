"use client";

import * as React from "react";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { useCredentialChange } from "@/hooks/use-guest-credentials";
import { isOutcomeUnknown, type GuestAccount, type GuestOverview, type GuestTarget, type SudoVerdict } from "@/lib/guest-credentials/api";
import { credentialErrorMessage, lockOutcomeText, OUTCOME_UNKNOWN } from "@/lib/guest-credentials/copy";

export interface SectionNotice {
  tone: "success" | "warning";
  text: string;
}

interface ConfirmProps {
  account: GuestAccount | null;
  target: GuestTarget;
  onClose: () => void;
  /** What the section says once the dialog has closed on an answer. */
  onDone: (notice: SectionNotice) => void;
}

/** The account to describe: the open one, or the last one while the dialog fades out. */
export function useLastAccount(account: GuestAccount | null): GuestAccount | null {
  const [last, setLast] = React.useState(account);
  React.useEffect(() => {
    if (account) setLast(account);
  }, [account]);
  return account ?? last;
}

/** Runs one confirmed change; an unconfirmed answer closes the dialog and is reported as such. */
function useConfirmedChange(onClose: () => void, onDone: (notice: SectionNotice) => void) {
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const inFlightRef = React.useRef(false);
  const run = async (send: () => Promise<SectionNotice | undefined>, fallback: string) => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setPending(true);
    setError(null);
    try {
      const notice = await send();
      onClose();
      if (notice) onDone(notice);
    } catch (failure) {
      if (isOutcomeUnknown(failure)) {
        onClose();
        onDone({ tone: "warning", text: OUTCOME_UNKNOWN });
      } else {
        setError(credentialErrorMessage(failure, fallback));
      }
    } finally {
      inFlightRef.current = false;
      setPending(false);
    }
  };
  return { pending, error, setError, run };
}

export function LockPasswordDialog({ account, target, onClose, onDone }: ConfirmProps) {
  const change = useCredentialChange(target);
  const { pending, error, setError, run } = useConfirmedChange(onClose, onDone);
  const shown = useLastAccount(account);
  const name = shown?.name ?? "";

  const confirm = () =>
    void run(async () => {
      const answer = await change({ kind: "lock-password", user: name });
      if (!answer) return undefined;
      const lines = [`The password of ${answer.user} is locked.`, ...lockOutcomeText(answer.warnings)];
      if (shown?.must_change && answer.warnings.includes("key_logins_still_work")) {
        lines.push(
          "A password change is still required at its next login, so an SSH key login asks for a new password first, and logins without a terminal fail until then.",
        );
      }
      const surprising = answer.warnings.some((code) => code === "key_logins_blocked_too" || code === "key_logins_may_still_work");
      return { tone: surprising ? "warning" : "success", text: lines.join(" ") };
    }, "The password could not be locked.");

  return (
    <DestructiveConfirmDialog
      open={account !== null}
      onOpenChange={(open) => {
        if (open) return;
        setError(null);
        onClose();
      }}
      title="Lock Password"
      description={`${name} can no longer log in with a password, at the console or over SSH. What this does to its SSH key logins depends on the guest's SSH server, and is reported once the password is locked.`}
      confirmLabel="Lock Password"
      pendingLabel="Locking…"
      pending={pending}
      onConfirm={confirm}
      error={error}
    />
  );
}

function grantNotice(user: string, group: string, verdict: SudoVerdict | null): SectionNotice {
  switch (verdict) {
    case "yes": return { tone: "success", text: `${user} is now an administrator of this guest: sudo works for it.` };
    case "rootpw": return { tone: "warning", text: `${user} joined ${group}, but sudo on this guest asks for root's password, so it cannot use sudo without knowing that password.` };
    case "no": return { tone: "warning", text: `${user} joined ${group}, but this guest's sudo rules do not let it run commands yet.` };
    case "absent": return { tone: "warning", text: `${user} joined ${group}. sudo is not installed, so this has no effect until it is.` };
    default: return { tone: "success", text: `${user} joined ${group}.` };
  }
}

function revokeNotice(user: string, group: string, verdict: SudoVerdict | null): SectionNotice {
  return verdict === "yes" || verdict === "rootpw"
    ? { tone: "warning", text: `${user} left ${group}, but sudo still lets it run commands through another rule.` }
    : { tone: "success", text: `${user} left ${group} and can no longer use sudo.` };
}

export function SudoDialog({ account, target, overview, grant, onClose, onDone }: ConfirmProps & { overview: GuestOverview; grant: boolean }) {
  const change = useCredentialChange(target);
  const { pending, error, setError, run } = useConfirmedChange(onClose, onDone);
  const name = useLastAccount(account)?.name ?? "";
  const group = overview.capabilities.sudo_group ?? "sudo";
  const asksRootPassword = overview.accounts.some((entry) => entry.sudo === "rootpw");

  const description = grant
    ? [
        `${name} becomes an administrator of this guest. It joins the ${group} group and can then run any command as root with sudo.`,
        asksRootPassword ? "On this guest sudo asks for root's password rather than the account's own, so the group alone may not be enough." : "",
        overview.capabilities.sudo_installed ? "" : "sudo is not installed on this guest, so this takes effect only once it is.",
      ].filter(Boolean).join(" ")
    : `${name} leaves the ${group} group and can no longer run commands as root through it.`;

  const confirm = () =>
    void run(async () => {
      const answer = await change({ kind: "sudo", user: name, enabled: grant });
      if (!answer) return undefined;
      return grant ? grantNotice(answer.user, group, answer.sudo) : revokeNotice(answer.user, group, answer.sudo);
    }, grant ? "Sudo could not be granted." : "Sudo could not be revoked.");

  return (
    <DestructiveConfirmDialog
      open={account !== null}
      onOpenChange={(open) => {
        if (open) return;
        setError(null);
        onClose();
      }}
      title={grant ? "Grant Sudo" : "Revoke Sudo"}
      description={description}
      confirmLabel={grant ? "Make Administrator" : "Revoke Sudo"}
      pendingLabel={grant ? "Granting…" : "Revoking…"}
      pending={pending}
      onConfirm={confirm}
      error={error}
    />
  );
}
