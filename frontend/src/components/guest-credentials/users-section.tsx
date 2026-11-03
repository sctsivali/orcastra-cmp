"use client";

import * as React from "react";
import { AlertTriangle, KeyRound, Lock, Plus, ShieldCheck, ShieldOff } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { RowActions, type RowAction } from "@/components/ui/row-actions";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { EMPTY_VALUE, TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import type { GuestAccount, GuestOverview, GuestTarget } from "@/lib/guest-credentials/api";
import { KIND_TEXT, PASSWORD_DETAIL, PASSWORD_TEXT, sudoText } from "@/lib/guest-credentials/copy";
import { defineTable } from "@/lib/table-filter";
import { plural } from "@/lib/utils";
import { LockPasswordDialog, SudoDialog, type SectionNotice } from "./account-dialogs";
import { CreateUserDialog } from "./create-user-dialog";
import { NoticeAlert } from "./credentials-states";
import { PasswordDialog } from "./password-dialog";

// note: a guest has a handful of login accounts, read in full each time, so the table carries no
// toolbar. The definition is still the contract the table guards read.
export const ACCOUNT_TABLE = defineTable<GuestAccount>()({
  id: "guestaccounts",
  searchPlaceholder: "Filter accounts",
  defaultSort: { field: "name" },
  columns: [
    { field: "name", label: "Name", search: (account) => account.name },
    { field: "kind", label: "Kind" },
    { field: "password", label: "Password" },
    { field: "sudo", label: "Sudo" },
    { field: "groups", label: "Admin Groups" },
  ],
});

type Prompt = { kind: "password" | "lock" | "grant" | "revoke"; account: GuestAccount } | null;

function PasswordCell({ account }: { account: GuestAccount }) {
  const detail = account.must_change && account.password !== "empty" ? "Must change at next login" : PASSWORD_DETAIL[account.password];
  return (
    <TableCell>
      <span className="inline-flex items-center gap-1.5">
        {account.password === "empty" && <AlertTriangle className="h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />}
        <span className={account.password === "empty" ? "text-destructive" : undefined}>{PASSWORD_TEXT[account.password] ?? account.password}</span>
      </span>
      {detail && <TableSubText wrap>{detail}</TableSubText>}
    </TableCell>
  );
}

interface UsersSectionProps {
  overview: GuestOverview;
  target: GuestTarget;
  writeReason?: string;
}

export function UsersSection({ overview, target, writeReason }: UsersSectionProps) {
  const [prompt, setPrompt] = React.useState<Prompt>(null);
  const [creating, setCreating] = React.useState(false);
  const [notice, setNotice] = React.useState<SectionNotice | null>(null);
  // Held apart from the prompt so the dialog keeps its wording while it fades out.
  const [sudoGrant, setSudoGrant] = React.useState(true);
  const { capabilities, accounts } = overview;
  const group = capabilities.sudo_group;
  const openless = accounts.filter((account) => account.password === "empty");

  const actionsFor = (account: GuestAccount): RowAction[] => {
    const blocked = writeReason;
    const items: RowAction[] = [
      {
        id: "password",
        label: "Set Password",
        icon: KeyRound,
        onSelect: () => setPrompt({ kind: "password", account }),
        unavailableReason: blocked ?? (capabilities.set_password ? undefined : "This guest has no chpasswd"),
      },
      {
        id: "lock",
        label: "Lock Password",
        icon: Lock,
        onSelect: () => setPrompt({ kind: "lock", account }),
        unavailableReason:
          blocked ??
          (!capabilities.lock
            ? "This guest has neither usermod nor passwd"
            : account.password === "locked" || account.password === "none"
              ? "Password login is already off"
              : undefined),
      },
    ];
    if (account.kind === "human") {
      const member = group !== null && account.admin_groups.includes(group);
      items.push({
        id: member ? "revoke" : "grant",
        label: member ? "Revoke Sudo" : "Grant Sudo",
        icon: member ? ShieldOff : ShieldCheck,
        onSelect: () => {
          setSudoGrant(!member);
          setPrompt({ kind: member ? "revoke" : "grant", account });
        },
        unavailableReason:
          blocked ??
          (!group
            ? "This guest has neither a sudo nor a wheel group"
            : capabilities.sudo_change ? undefined : "This guest lacks the tools to change groups"),
        separatorBefore: true,
      });
    }
    return items;
  };

  const close = () => setPrompt(null);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <p className="min-w-0 flex-1 text-sm text-muted-foreground">
          {plural(accounts.length, "login account")} on this guest. System accounts without a login shell are left out.
        </p>
        <Button size="sm" onClick={() => setCreating(true)} disabled={!!writeReason || !capabilities.create_user}>
          <Plus className="mr-1.5 h-4 w-4" aria-hidden="true" />
          Create User
        </Button>
      </div>

      {!capabilities.create_user && (
        <p className="text-xs text-muted-foreground">This guest has no tool to create accounts, so Create User is not offered.</p>
      )}
      {openless.length > 0 && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            {openless.map((account) => account.name).join(", ")} {openless.length === 1 ? "has" : "have"} an empty
            password: anyone at the Serial or Graphics console can log in without one. Set a password or lock it.
          </AlertDescription>
        </Alert>
      )}
      {overview.accounts_omitted > 0 && (
        <p className="text-xs text-muted-foreground">
          {plural(overview.accounts_omitted, "more account is", "more accounts are")} not listed: this panel shows the first {accounts.length}.
        </p>
      )}
      <NoticeAlert notice={notice} onDismiss={() => setNotice(null)} />

      <Table containerClassName="rounded-md border bg-card" scrollLabel="Login accounts">
        <TableHeader>
          <TableRow>
            {ACCOUNT_TABLE.columns.map((column) => <TableHead key={column.field}>{column.label}</TableHead>)}
            <TableHead className="w-12 text-right" pinned="end"><span className="sr-only">Actions</span></TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {accounts.length === 0 ? (
            <TableEmptyRow colSpan={6} title="No login accounts found" description="The guest listed no account with a login shell." />
          ) : (
            accounts.map((account) => (
              <TableRow key={account.name} data-row-key={account.name}>
                <TableCell className="font-medium">
                  <span className="font-mono" translate="no">{account.name}</span>
                  <TableSubText wrap>UID {account.uid} · {account.home}</TableSubText>
                </TableCell>
                <TableCell>{KIND_TEXT[account.kind] ?? account.kind}</TableCell>
                <PasswordCell account={account} />
                <TableCell>{sudoText(account.sudo, account.kind) ?? EMPTY_VALUE}</TableCell>
                <TableCell>{account.admin_groups.length ? account.admin_groups.join(", ") : EMPTY_VALUE}</TableCell>
                <TableCell className="w-12 text-right" pinned="end">
                  <RowActions actions={actionsFor(account)} triggerLabel={`Actions for account ${account.name}`} />
                </TableCell>
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>

      <PasswordDialog
        account={prompt?.kind === "password" ? prompt.account : null}
        overview={overview}
        target={target}
        writeReason={writeReason}
        onClose={close}
      />
      <LockPasswordDialog account={prompt?.kind === "lock" ? prompt.account : null} target={target} onClose={close} onDone={setNotice} />
      <SudoDialog
        account={prompt?.kind === "grant" || prompt?.kind === "revoke" ? prompt.account : null}
        grant={sudoGrant}
        overview={overview}
        target={target}
        onClose={close}
        onDone={setNotice}
      />
      <CreateUserDialog open={creating} overview={overview} target={target} writeReason={writeReason} onClose={() => setCreating(false)} />
    </div>
  );
}
