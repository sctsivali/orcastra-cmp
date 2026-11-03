"use client";

import * as React from "react";
import { AlertTriangle, Plus, Trash2 } from "lucide-react";
import { LoadingLottieSmall } from "@/components/loading-lottie";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { Label } from "@/components/ui/label";
import { OverflowText } from "@/components/ui/overflow-text";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { EMPTY_VALUE, TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import { useAuthorizedKeys, useCredentialChange } from "@/hooks/use-guest-credentials";
import { isOutcomeUnknown, type AuthorizedKey, type GuestOverview, type GuestTarget, type KeyList } from "@/lib/guest-credentials/api";
import { credentialErrorMessage, KIND_TEXT, OUTCOME_UNKNOWN, warningText } from "@/lib/guest-credentials/copy";
import { defineTable } from "@/lib/table-filter";
import type { SectionNotice } from "./account-dialogs";
import { AddKeyDialog } from "./add-key-dialog";
import { CredentialsStates, NoticeAlert } from "./credentials-states";

export const KEY_TABLE = defineTable<AuthorizedKey>()({
  id: "guestkeys",
  searchPlaceholder: "Filter keys",
  defaultSort: { field: "type" },
  columns: [
    { field: "type", label: "Type" },
    { field: "fingerprint", label: "Fingerprint", search: (key) => key.fingerprint },
    { field: "comment", label: "Comment", search: (key) => key.comment },
    { field: "notes", label: "Notes" },
  ],
});

const KEY_TYPE_TEXT: Record<string, string> = {
  "ssh-ed25519": "Ed25519",
  "ssh-rsa": "RSA",
  "ecdsa-sha2-nistp256": "ECDSA P-256",
  "ecdsa-sha2-nistp384": "ECDSA P-384",
  "ecdsa-sha2-nistp521": "ECDSA P-521",
  "sk-ssh-ed25519@openssh.com": "Ed25519 security key",
  "sk-ecdsa-sha2-nistp256@openssh.com": "ECDSA security key",
  "ssh-dss": "DSA",
};

const FILE_STATE_TEXT: Partial<Record<KeyList["state"], string>> = {
  link: "authorized_keys of this account is a link, so this panel does not change it. Replace it with a regular file to manage keys here.",
  not_regular: "authorized_keys of this account is not a regular file, so this panel does not change it.",
  unreadable: "authorized_keys of this account could not be read.",
};

/** The account a key list opens on: the first person's, else root's. */
export function defaultKeyAccount(overview: GuestOverview): string | null {
  return (
    overview.accounts.find((account) => account.kind === "human")?.name ??
    overview.accounts.find((account) => account.kind === "root")?.name ??
    overview.accounts[0]?.name ??
    null
  );
}

function KeyNotes({ entry }: { entry: AuthorizedKey }) {
  const notes: string[] = [];
  if (!entry.fingerprint) notes.push("This line could not be read as a key");
  if (entry.options.length) notes.push(`Options: ${entry.options.join(", ")}`);
  if (entry.key_type === "ssh-rsa" && entry.bits !== null && entry.bits >= 2048 && entry.bits < 3072) notes.push("Below 3072 bits");
  return (
    <TableCell>
      {entry.weak && (
        <span className="inline-flex items-center gap-1.5 text-destructive">
          <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
          {entry.key_type === "ssh-dss" ? "Weak: DSA is no longer safe" : "Weak: too short to be safe"}
        </span>
      )}
      {notes.map((note) => <TableSubText key={note} wrap>{note}</TableSubText>)}
      {!entry.weak && notes.length === 0 && EMPTY_VALUE}
    </TableCell>
  );
}

interface KeysSectionProps {
  overview: GuestOverview;
  target: GuestTarget;
  active: boolean;
  user: string | null;
  onUser: (user: string) => void;
  writeReason?: string;
  onRequestAccess: () => void;
  onOpenConsoleTab?: (tab: "serial" | "graphics") => void;
}

export function KeysSection({ overview, target, active, user, onUser, writeReason, onRequestAccess, onOpenConsoleTab }: KeysSectionProps) {
  const keys = useAuthorizedKeys(target, user, active);
  const change = useCredentialChange(target);
  const [adding, setAdding] = React.useState(false);
  const [removing, setRemoving] = React.useState<AuthorizedKey | null>(null);
  const [shownRemoval, setShownRemoval] = React.useState<AuthorizedKey | null>(null);
  const [pending, setPending] = React.useState(false);
  const [removeError, setRemoveError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<SectionNotice | null>(null);
  const removeInFlightRef = React.useRef(false);

  const options = React.useMemo(
    () => overview.accounts.map((account) => ({ value: account.name, label: account.name, sublabel: KIND_TEXT[account.kind] })),
    [overview.accounts],
  );
  const data = keys.data;
  const editable = data?.state === "present" || data?.state === "absent";

  const remove = async () => {
    const entry = removing;
    if (!entry?.fingerprint || !user || removeInFlightRef.current) return;
    removeInFlightRef.current = true;
    setPending(true);
    setRemoveError(null);
    try {
      const answer = await change({ kind: "remove-key", user, fingerprint: entry.fingerprint });
      setRemoving(null);
      if (answer) {
        setNotice({
          tone: "success",
          text: answer.removed > 1 ? `${answer.removed} lines holding this key were removed for ${user}.` : `The key was removed for ${user}.`,
        });
      }
    } catch (failure) {
      if (isOutcomeUnknown(failure)) {
        setRemoving(null);
        setNotice({ tone: "warning", text: OUTCOME_UNKNOWN });
      } else {
        setRemoveError(credentialErrorMessage(failure, "The key could not be removed."));
      }
    } finally {
      removeInFlightRef.current = false;
      setPending(false);
    }
  };

  let body: React.ReactNode;
  if (!user) {
    body = <p className="text-sm text-muted-foreground">This guest lists no login account to hold keys.</p>;
  } else if (keys.isLoading) {
    body = (
      <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
        <LoadingLottieSmall />
        Reading the keys of {user}…
      </div>
    );
  } else if (keys.error && !data) {
    body = (
      <CredentialsStates
        target={target}
        error={keys.error}
        subject="this account's keys"
        onRetry={() => void keys.refetch()}
        onRequestAccess={onRequestAccess}
        onOpenConsoleTab={onOpenConsoleTab}
      />
    );
  } else if (data) {
    body = (
      <>
        {FILE_STATE_TEXT[data.state] && (
          <Alert variant="warning" role="note"><AlertDescription>{FILE_STATE_TEXT[data.state]}</AlertDescription></Alert>
        )}
        {data.warnings.map((warning) => (
          <Alert key={warning} variant="warning" role="note"><AlertDescription>{warningText(warning)}</AlertDescription></Alert>
        ))}
        <Table containerClassName="rounded-md border bg-card" scrollLabel={`SSH keys of ${user}`}>
          <TableHeader>
            <TableRow>
              {KEY_TABLE.columns.map((column) => <TableHead key={column.field}>{column.label}</TableHead>)}
              <TableHead className="w-12 text-right" pinned="end"><span className="sr-only">Actions</span></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.entries.length === 0 ? (
              <TableEmptyRow
                colSpan={5}
                title="No SSH keys for this account"
                description={data.state === "absent" ? "There is no authorized_keys file yet. Adding a key creates it." : "Add a key to log in over SSH without a password."}
              />
            ) : (
              data.entries.map((entry) => (
                <TableRow key={`${entry.line}-${entry.fingerprint ?? "unread"}`} data-row-key={entry.fingerprint ?? `line-${entry.line}`}>
                  <TableCell className="font-medium">
                    {entry.key_type ? KEY_TYPE_TEXT[entry.key_type] ?? entry.key_type : EMPTY_VALUE}
                    <TableSubText wrap>{entry.bits ? `${entry.bits} bits · line ${entry.line}` : `line ${entry.line}`}</TableSubText>
                  </TableCell>
                  <TableCell className="font-mono">
                    {entry.fingerprint ? <OverflowText value={entry.fingerprint} label={`fingerprint of the key on line ${entry.line}`} mono copy="always" /> : EMPTY_VALUE}
                  </TableCell>
                  <TableCell className="max-w-56">
                    {entry.comment ? <OverflowText value={entry.comment} label={`comment of the key on line ${entry.line}`} /> : EMPTY_VALUE}
                  </TableCell>
                  <KeyNotes entry={entry} />
                  <TableCell className="w-12 text-right" pinned="end">
                    {entry.fingerprint && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-8 w-8 p-0"
                        aria-label={`Remove the key on line ${entry.line}`}
                        disabled={!!writeReason || !editable}
                        onClick={() => {
                          setRemoveError(null);
                          setShownRemoval(entry);
                          setRemoving(entry);
                        }}
                      >
                        <Trash2 className="h-4 w-4" aria-hidden="true" />
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-48 flex-1 space-y-1.5">
          <Label htmlFor="credentials-key-account">Account</Label>
          <SearchableSelect id="credentials-key-account" value={user ?? ""} onValueChange={onUser} options={options} ariaLabel="Account whose keys are shown" />
        </div>
        <Button size="sm" onClick={() => setAdding(true)} disabled={!user || !!writeReason || (data !== undefined && !editable)}>
          <Plus className="mr-1.5 h-4 w-4" aria-hidden="true" />
          Add Key
        </Button>
      </div>
      <NoticeAlert notice={notice} onDismiss={() => setNotice(null)} />
      {body}

      <DestructiveConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => !open && setRemoving(null)}
        title="Remove SSH Key"
        description={`${user} can no longer log in with the key ${shownRemoval?.fingerprint ?? ""}${shownRemoval?.comment ? ` (${shownRemoval.comment})` : ""}. If it is the key you log in with, make sure you have another way in first.`}
        confirmLabel="Remove Key"
        pendingLabel="Removing…"
        pending={pending}
        onConfirm={() => void remove()}
        error={removeError}
      />
      {user && (
        <AddKeyDialog open={adding} user={user} target={target} writeReason={writeReason} onClose={() => setAdding(false)} onDone={setNotice} />
      )}
    </div>
  );
}
