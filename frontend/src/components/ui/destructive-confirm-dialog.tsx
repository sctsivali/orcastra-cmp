"use client";

import * as React from "react";
import { Loader2 } from "lucide-react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { DialogSubmitError } from "@/components/ui/dialog-submit-error";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

export interface ConfirmDependents {
  /** Heading above the list, ex; `2 instances were created from this image`. */
  summary: string;
  /** Names, already resolved. Never raw API references. */
  names: string[];
}

interface DestructiveConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: React.ReactNode;
  /** What the confirm button says at rest, ex; `Delete image`. */
  confirmLabel: string;
  /** What it says while the request is in flight. */
  pendingLabel: string;
  pending: boolean;
  /**
   * A preflight is still running, so what depends on the target is not known yet. The confirm
   * stays blocked until it settles: without this an operator can confirm inside that window
   * and be answered by a refusal that the dialog was about to explain to them anyway.
   */
  preparing?: boolean;
  /** What to say while `preparing`. */
  preparingLabel?: string;
  onConfirm: (reason?: string) => void;
  /** Present when something depends on the target. Rendered above the acknowledgement. */
  dependents?: ConfirmDependents;
  /**
   * When set, the confirm button stays disabled until the operator ticks this. Use it only
   * where proceeding overrides a real refusal, never as generic friction.
   */
  acknowledgeLabel?: string;
  /**
   * When set, the operator must type a justification and it travels to `onConfirm`. Use it with
   * `acknowledgeLabel` where the action is recorded as a deliberate, attributable override: a tick
   * box says someone accepted a consequence, and only words say why. A canned reason written by
   * the caller is worse than none, because it fills an audit field with something no human meant.
   */
  reasonLabel?: string;
  reasonPlaceholder?: string;
  /**
   * When set, the operator must type `value` exactly before the confirm unblocks. Use it where
   * the operator has to prove WHICH row they are on, not merely that they accepted something.
   */
  // note: `prompt`, not `label`. It is an instruction to the operator and reads as a sentence,
  // and check-heading-case treats every `label:` literal as a title that must be Title Case.
  confirmPhrase?: { prompt: React.ReactNode; value: string };
  /** Surfaced above the footer when an attempt failed but the dialog stayed open. */
  error?: string | null;
}

/** How many dependents are listed before the rest are counted. */
const NAMES_SHOWN = 8;

/**
 * One confirm-and-mutate dialog for a destructive row action.
 *
 * Three details here are load-bearing and were each learned from a defect in this repo, so
 * they are not restated at call sites. The action's preventDefault keeps the dialog open and
 * populated for the length of the request rather than letting Radix close it mid-flight. The
 * dialog refuses Escape and an outside click while pending, so a half-finished delete cannot
 * be dismissed into an unknown state. And the caller holds its display target one render
 * longer than its real target, so the body does not blank during the exit fade.
 *
 * Three affordances, three different proofs, and they are not interchangeable. A typed
 * confirmation proves the operator read WHICH target; a checkbox proves they accepted a
 * consequence; a reason is the only one that says WHY.
 *
 * note: this docblock used to argue that typing belongs only to cascading deletions, on the
 * grounds that an image is re-uploadable and a 64-character fingerprint produces copy-paste
 * without reading. Both of those are true of the case it was written about and neither
 * generalises. A snapshot taken by hand on the host is not re-uploadable, nothing in this
 * console will make another one, and its name is short and human. The rule was drawn too wide
 * from one example, so it is corrected here rather than quietly dropped: type when the mistake
 * is WHICH ONE, and the target is short enough to be read rather than pasted.
 *
 * `reasonLabel` is a third thing again, and only for actions recorded as an override. A typed
 * confirmation proves the operator read WHICH target; a checkbox proves they accepted a
 * consequence; a reason is the only one that says WHY, and it is the one an audit trail is read
 * for. It is never composed by the caller: a canned sentence fills that field with something no
 * human meant, which is worse than leaving it empty.
 */
export function DestructiveConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  pendingLabel,
  pending,
  preparing,
  preparingLabel,
  onConfirm,
  dependents,
  acknowledgeLabel,
  reasonLabel,
  reasonPlaceholder,
  confirmPhrase,
  error,
}: DestructiveConfirmDialogProps) {
  const [acknowledged, setAcknowledged] = React.useState(false);
  const [reason, setReason] = React.useState("");
  const [typed, setTyped] = React.useState("");
  const acknowledgeId = React.useId();
  const reasonId = React.useId();
  const confirmId = React.useId();

  // Reset on close so the next row does not inherit the previous row's tick. Tied to `open`
  // rather than to the target, because the caller keeps the target alive through the fade.
  React.useEffect(() => {
    if (!open) {
      setAcknowledged(false);
      setReason("");
      setTyped("");
    }
  }, [open]);

  const blocked =
    (!!acknowledgeLabel && !acknowledged) ||
    (!!reasonLabel && reason.trim().length === 0) ||
    // Exact, and trimmed only at the ends. A near miss is a different snapshot.
    (!!confirmPhrase && typed.trim() !== confirmPhrase.value) ||
    pending ||
    !!preparing;
  const shown = dependents?.names.slice(0, NAMES_SHOWN) ?? [];
  const hidden = (dependents?.names.length ?? 0) - shown.length;

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        // A request is in flight. Dismissing now would leave the operator guessing whether it
        // landed, so the handler closes the dialog when it resolves instead.
        if (pending && !next) return;
        onOpenChange(next);
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>

        {dependents && dependents.names.length > 0 && (
          <div className="rounded-md border border-warning/40 bg-warning/10 p-3">
            <p className="text-sm font-medium text-foreground">{dependents.summary}</p>
            <ul className="mt-2 space-y-0.5">
              {shown.map((name) => (
                // note: wraps rather than clips. This is the list the operator is being asked
                // to accept losing, so a name cut off with no reveal is the one case where
                // truncation costs the decision itself.
                <li key={name} className="break-all font-mono text-xs text-muted-foreground">
                  {name}
                </li>
              ))}
              {hidden > 0 && (
                <li className="text-xs text-muted-foreground">and {hidden} more</li>
              )}
            </ul>
          </div>
        )}

        {confirmPhrase && (
          <div className="space-y-2">
            <Label htmlFor={confirmId}>{confirmPhrase.prompt}</Label>
            <Input
              id={confirmId}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={confirmPhrase.value}
              autoComplete="off"
              spellCheck={false}
              className="font-mono"
              disabled={pending}
            />
          </div>
        )}

        {reasonLabel && (
          <div className="space-y-2">
            <Label htmlFor={reasonId}>{reasonLabel}</Label>
            <Textarea
              id={reasonId}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={reasonPlaceholder}
              maxLength={280}
              rows={3}
              disabled={pending}
            />
          </div>
        )}

        {acknowledgeLabel && (
          <label
            htmlFor={acknowledgeId}
            className="flex cursor-pointer items-start gap-2 rounded-md border p-3"
          >
            <Checkbox
              id={acknowledgeId}
              checked={acknowledged}
              onCheckedChange={(next) => setAcknowledged(next === true)}
              disabled={pending}
              className="mt-0.5"
            />
            <span className="text-sm">{acknowledgeLabel}</span>
          </label>
        )}

        {preparing && preparingLabel && (
          <p role="status" className="text-sm text-muted-foreground">
            {preparingLabel}
          </p>
        )}

        <DialogSubmitError message={error} className="mt-0" />

        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            // preventDefault blocks Radix's built-in close so the dialog stays open, and
            // populated, for the whole request. The caller closes it when the request settles.
            onClick={(e) => {
              e.preventDefault();
              if (blocked) return;
              onConfirm(reasonLabel ? reason.trim() : undefined);
            }}
            disabled={blocked}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {pending && <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />}
            {pending ? pendingLabel : confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
