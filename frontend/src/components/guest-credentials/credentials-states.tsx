"use client";

import { AlertTriangle, Ban, Clock, Cloud, HardDrive, Lock, Pause, Power, ShieldAlert, TerminalSquare } from "lucide-react";
import { AgentMissing, WindowsGuestState } from "@/components/instance-files/agent-missing";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { DialogSubmitError } from "@/components/ui/dialog-submit-error";
import { EmptyState } from "@/components/ui/empty-state";
import { credentialErrorCode, isOutcomeUnknown, type GuestTarget } from "@/lib/guest-credentials/api";
import { credentialErrorMessage, OUTCOME_UNKNOWN } from "@/lib/guest-credentials/copy";
import { describeReadError } from "@/lib/read-error";
import { APIError } from "@/types/api";
import type { SectionNotice } from "./account-dialogs";

const WINDOWS_TITLE = "Logins are not managed here for Windows guests";
const WINDOWS_DESCRIPTION =
  "Windows accounts are managed inside Windows. Use the Graphics console to sign in and change them there.";
const AGENT_DESCRIPTION =
  "Managing logins inside a virtual machine goes through the guest agent, and it is not answering. If this VM has just started, give it a minute.";

interface CredentialsStatesProps {
  target: GuestTarget;
  error: unknown;
  /** What could not be read, for the fallback, ex; "this guest's accounts". */
  subject: string;
  onRetry: () => void;
  onRequestAccess: () => void;
  onOpenConsoleTab?: (tab: "serial" | "graphics") => void;
}

/** Every refusal a read can meet, in words that say what to do next. Nothing was changed by any of them. */
export function CredentialsStates({ target, error, subject, onRetry, onRequestAccess, onOpenConsoleTab }: CredentialsStatesProps) {
  const code = credentialErrorCode(error);
  const retry = <Button size="sm" variant="outline" onClick={onRetry}>Check again</Button>;
  switch (code) {
    case "agent_unavailable":
      return (
        <AgentMissing
          target={target}
          onRetry={onRetry}
          onOpenConsoleTab={onOpenConsoleTab}
          windowsTitle={WINDOWS_TITLE}
          windowsDescription={WINDOWS_DESCRIPTION}
          agentDescription={AGENT_DESCRIPTION}
        />
      );
    case "guest_os_unsupported":
      return <WindowsGuestState title={WINDOWS_TITLE} description={WINDOWS_DESCRIPTION} onOpenConsoleTab={onOpenConsoleTab} />;
    case "instance_not_running":
      return <EmptyState icon={Power} title="This instance is stopped" description="Start the instance to manage its logins, then check again." action={retry} />;
    case "instance_frozen":
      return <EmptyState icon={Pause} title="This instance is frozen" description="Resume the instance to manage its logins, then check again." action={retry} />;
    case "instance_not_found":
      return <EmptyState icon={Ban} title="This instance no longer exists" />;
    case "guest_shell_missing":
      return <EmptyState icon={TerminalSquare} title="This guest has no shell" description={credentialErrorMessage(error, "")} />;
    case "cluster_unreachable":
      return <EmptyState icon={Cloud} title="Cluster not reachable" description={`${target.host} did not answer. Nothing changed.`} action={retry} />;
    case "cluster_not_pinned":
      return <EmptyState icon={ShieldAlert} title="Cluster certificate not confirmed" description="This cluster's certificate could not be confirmed, so nothing was sent." action={retry} />;
    case "migration_fenced":
      return <EmptyState icon={HardDrive} title="This instance is being migrated" description="Its logins can be managed once the move finishes." action={retry} />;
    case "feature_disabled":
      return <EmptyState icon={Ban} title="Credentials are turned off" description="This deployment does not offer the Credentials panel." />;
    case "break_glass_required":
      return (
        <EmptyState
          icon={ShieldAlert}
          title="This instance belongs to another organization"
          description="Managing its logins needs a recorded break-glass grant, the same as opening a shell."
          action={<Button size="sm" onClick={onRequestAccess}>Request access</Button>}
        />
      );
    case "busy":
    case "rate_limited":
      return <EmptyState icon={Clock} title="Too many requests right now" description={credentialErrorMessage(error, "")} action={retry} />;
    case "guest_timeout":
      return <EmptyState icon={Clock} title="The guest did not answer" description="It may be busy or still starting. Nothing changed." action={retry} />;
    default: {
      if (error instanceof APIError && error.status === 403 && !code) {
        return (
          <EmptyState
            icon={Lock}
            title="Your access does not include the console"
            description="Ask your provider to add Console to your access for this instance."
          />
        );
      }
      return (
        <EmptyState
          icon={AlertTriangle}
          title={`Could not read ${subject}`}
          description={`${credentialErrorMessage(error, describeReadError(error).reason)} Nothing changed.`}
          action={retry}
        />
      );
    }
  }
}

/**
 * Why a change did not go through, next to the buttons that sent it. An unconfirmed change is not
 * a failure and is never painted as one: it may have happened.
 */
export function ChangeError({ error, fallback }: { error: unknown; fallback: string }) {
  if (!error) return null;
  if (isOutcomeUnknown(error)) {
    return (
      <Alert variant="warning" className="mt-4 shrink-0">
        <Clock className="h-4 w-4" aria-hidden="true" />
        <AlertTitle>Sent, not confirmed</AlertTitle>
        <AlertDescription>{OUTCOME_UNKNOWN}</AlertDescription>
      </Alert>
    );
  }
  return <DialogSubmitError message={credentialErrorMessage(error, fallback)} />;
}

/** What the last change in a section did, until the next one or a dismissal. */
export function NoticeAlert({ notice, onDismiss }: { notice: SectionNotice | null; onDismiss: () => void }) {
  return (
    <div aria-live="polite">
      {notice && (
        <Alert variant={notice.tone === "warning" ? "warning" : "default"} role="status">
          <AlertDescription className="flex flex-wrap items-start gap-2">
            <span className="min-w-0 flex-1">{notice.text}</span>
            <Button type="button" size="sm" variant="ghost" className="h-6 px-2 text-xs" onClick={onDismiss}>Dismiss</Button>
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}
