"use client";

import type * as React from "react";
import { AlertTriangle, Info } from "lucide-react";
import { LoadingLottieSmall } from "@/components/loading-lottie";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { CodeBlock } from "@/components/ui/code-block";
import { OverflowText } from "@/components/ui/overflow-text";
import { useSshServer } from "@/hooks/use-guest-credentials";
import type { GuestTarget, SshServerFacts } from "@/lib/guest-credentials/api";
import { ADDRESS_TEXT, EFFECTIVE_SETTINGS, effectiveGloss, NOTE_TEXT } from "@/lib/guest-credentials/copy";
import { CredentialsStates } from "./credentials-states";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function Fact({ name, children }: { name: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[minmax(8rem,auto)_1fr] gap-x-3 gap-y-0.5 py-1 text-sm">
      <dt className="text-muted-foreground">{name}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

/** What a person reads first: is anything listening for SSH at all. */
function serverState(facts: SshServerFacts): string {
  if (!facts.installed) return "Not installed";
  if (facts.running) return "Running";
  // Ubuntu 22.10 and later: systemd holds port 22 and starts the server on the first connection,
  // so ssh.service reads inactive while logins work.
  if (facts.socket_activated) return "Listening: the server starts on the first connection";
  return "Not running";
}

function formatValue(value: string | string[] | undefined): string | null {
  if (value === undefined) return null;
  return Array.isArray(value) ? value.join(" ") : value;
}

interface SshServerSectionProps {
  target: GuestTarget;
  active: boolean;
  /** The account a connect example names. */
  exampleUser: string | null;
  onRequestAccess: () => void;
  onOpenConsoleTab?: (tab: "serial" | "graphics") => void;
}

/** The guest's SSH server, read only: whether it runs, how it is set, and where it can be reached. */
export function SshServerSection({ target, active, exampleUser, onRequestAccess, onOpenConsoleTab }: SshServerSectionProps) {
  const query = useSshServer(target, active);
  if (query.isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
        <LoadingLottieSmall />
        Reading the SSH server…
      </div>
    );
  }
  if (!query.data) {
    return (
      <CredentialsStates
        target={target}
        error={query.error}
        subject="the SSH server"
        onRetry={() => void query.refetch()}
        onRequestAccess={onRequestAccess}
        onOpenConsoleTab={onOpenConsoleTab}
      />
    );
  }

  const facts = query.data;
  const port = formatValue(facts.effective?.port) ?? "22";
  const firstAddress = facts.addresses.find((entry) => entry.label === "public") ?? facts.addresses[0];
  const example = firstAddress && exampleUser
    ? `ssh ${port === "22" ? "" : `-p ${port} `}${exampleUser}@${firstAddress.family === "ipv6" ? `[${firstAddress.address}]` : firstAddress.address}`
    : null;

  return (
    <div className="space-y-5">
      <Section title="Server">
        <dl>
          <Fact name="State">{serverState(facts)}</Fact>
          {facts.units.length > 0 && (
            <Fact name="Units">
              <span className="font-mono text-xs" translate="no">
                {facts.units.map((unit) => `${unit.unit} ${unit.state}`).join(" · ")}
              </span>
            </Fact>
          )}
        </dl>
        {!facts.installed && (
          <p className="text-sm text-muted-foreground">
            No SSH server is installed in this guest, so SSH logins are not possible. Install openssh-server inside the guest to allow them.
          </p>
        )}
      </Section>

      {facts.notes.map((note) => (
        <Alert key={note} variant="warning" role="note">
          <AlertTriangle className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>{NOTE_TEXT[note] ?? note}</AlertDescription>
        </Alert>
      ))}

      {facts.installed && (
        <Section title="Settings">
          {facts.effective ? (
            <>
              <dl>
                {EFFECTIVE_SETTINGS.map((setting) => {
                  const value = formatValue(facts.effective?.[setting.key]);
                  const gloss = value ? effectiveGloss(setting.key, value) : null;
                  return (
                    <Fact key={setting.key} name={setting.name}>
                      <span className="font-mono" translate="no">{value ?? "unknown"}</span>
                      {gloss && <span className="text-muted-foreground"> ({gloss})</span>}
                      <span className="block text-xs text-muted-foreground" translate="no">{setting.directive}</span>
                    </Fact>
                  );
                })}
              </dl>
              <p className="text-xs text-muted-foreground">
                These are the server&apos;s global values. A Match block in its configuration can change them for some
                users or addresses, and that is not shown here.
              </p>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">Unknown. The SSH server&apos;s settings could not be read from inside the guest.</p>
          )}
        </Section>
      )}

      {facts.host_keys.length > 0 && (
        <Section title="Host Key Fingerprints">
          <p className="text-xs text-muted-foreground">
            The first time you connect, SSH shows one of these and asks whether to trust it. Compare them: if none matches,
            do not connect.
          </p>
          <ul className="space-y-1.5">
            {facts.host_keys.map((key) => (
              <li key={key.fingerprint} className="grid grid-cols-[minmax(6rem,auto)_1fr] items-baseline gap-x-3 text-sm">
                <span className="text-muted-foreground" translate="no">{key.type}{key.bits ? ` (${key.bits})` : ""}</span>
                <OverflowText value={key.fingerprint} label={`${key.type} host key fingerprint`} mono copy="always" />
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Addresses">
        <Alert role="note">
          <Info className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            Most instances have a private address, which works only inside that network. The dashboard does not test
            whether your own computer can reach any of these.
          </AlertDescription>
        </Alert>
        {facts.addresses.length === 0 ? (
          <p className="text-sm text-muted-foreground">LXD reports no address for this instance.</p>
        ) : (
          <ul className="divide-y rounded-md border">
            {facts.addresses.map((entry) => (
              <li key={`${entry.interface}-${entry.address}`} className="space-y-0.5 px-3 py-2 text-sm">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <OverflowText value={entry.address} label={`address on ${entry.interface}`} mono copy="always" className="min-w-0" />
                  <span className="text-xs text-muted-foreground" translate="no">{entry.interface} · {entry.family === "ipv6" ? "IPv6" : "IPv4"}</span>
                </div>
                <p className="text-xs text-muted-foreground">
                  <span className="font-medium text-foreground">{ADDRESS_TEXT[entry.label]?.name ?? entry.label}.</span>{" "}
                  {ADDRESS_TEXT[entry.label]?.reach}
                </p>
              </li>
            ))}
          </ul>
        )}
        {example && facts.installed && <CodeBlock value={example} copyLabel="Command" label="To connect, from a computer that can reach the address" />}
      </Section>
    </div>
  );
}
