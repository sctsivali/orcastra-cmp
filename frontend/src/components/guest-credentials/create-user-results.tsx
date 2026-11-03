"use client";

import { AlertTriangle, CheckCircle2, XCircle } from "lucide-react";
import type { CreateUserResult, CreateUserStep } from "@/lib/guest-credentials/api";

const STEP_FAILURES: Record<string, string> = {
  tool_missing: "This guest lacks a tool this step needs.",
  sudo_group_missing: "This guest has neither a sudo nor a wheel group.",
  expire_unsupported: "This guest cannot require a password change.",
  home_not_owned: "The home folder is missing or belongs to someone else.",
  ssh_dir_not_owned: "The .ssh folder is not one this account owns.",
  key_file_is_link: "authorized_keys is a link.",
  key_file_not_regular: "authorized_keys is not a regular file.",
  edit_conflict: "authorized_keys changed at the same moment. Add the key again from SSH Keys.",
  credential_outcome_unknown: "It was sent but not confirmed. Check the account before relying on it.",
  guest_timeout: "The guest did not answer in time.",
};

type Tone = "ok" | "warn" | "failed";

function failure(step: CreateUserStep): string {
  return STEP_FAILURES[step.code ?? ""] ?? "The guest could not complete this step.";
}

function describe(step: CreateUserStep, group: string): { tone: Tone; text: string } {
  const failed = step.result === "failed";
  switch (step.step) {
    case "create":
      return { tone: "ok", text: "Account created." };
    case "read_back":
      return {
        tone: "warn",
        text: "The account was created, but this panel does not manage it: it has no usable home folder or an unusual user ID.",
      };
    case "sudo":
      if (step.result === "not_effective") {
        return {
          tone: "warn",
          text: `Joined ${group}, but sudo does not let it run commands. On this guest sudo may ask for root's password, as openSUSE does, or its rules leave the group out.`,
        };
      }
      return failed ? { tone: "failed", text: `Not added to ${group}. ${failure(step)}` } : { tone: "ok", text: `Joined ${group}: sudo works.` };
    case "password":
      return failed ? { tone: "failed", text: `Password not set. ${failure(step)}` } : { tone: "ok", text: "Password set." };
    case "expire":
      return failed
        ? { tone: "failed", text: `A new password at the first login could not be required. ${failure(step)}` }
        : { tone: "ok", text: "A new password is required at the first login, SSH key logins included." };
    case "key":
      return failed ? { tone: "failed", text: `SSH key not added. ${failure(step)}` } : { tone: "ok", text: "SSH key added." };
  }
}

const ICONS = {
  ok: <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-status-online" aria-hidden="true" />,
  warn: <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-status-pending" aria-hidden="true" />,
  failed: <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />,
} as const;

const TONE_WORD: Record<Tone, string> = { ok: "Done", warn: "Needs a look", failed: "Did not complete" };

/** What each step of a create did, in order. A later step can fail while the account still exists. */
export function CreateUserResults({ result, group }: { result: CreateUserResult; group: string }) {
  const passwordSet = result.steps.some((step) => step.step === "password" && step.result === "ok");
  return (
    <div className="space-y-3" role="status" aria-live="polite">
      <ul className="space-y-2 text-sm">
        {result.steps.map((step, index) => {
          const { tone, text } = describe(step, group);
          return (
            <li key={`${step.step}-${index}`} className="flex items-start gap-2">
              {ICONS[tone]}
              <span>
                <span className="sr-only">{TONE_WORD[tone]}: </span>
                {text}
              </span>
            </li>
          );
        })}
      </ul>
      {result.account && (
        <p className="text-sm text-muted-foreground">
          <span className="font-mono text-foreground" translate="no">{result.account.name}</span> has user ID{" "}
          {result.account.uid} and its home folder at{" "}
          <span className="break-all font-mono" translate="no">{result.account.home}</span>.
        </p>
      )}
      {passwordSet && (
        <p className="text-sm text-muted-foreground">Orcastra does not keep a copy of this password. Keep yours in a password manager.</p>
      )}
    </div>
  );
}
