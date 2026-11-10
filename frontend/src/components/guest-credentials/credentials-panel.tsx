"use client";

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Lock } from "lucide-react";
import { LoadingLottieMedium } from "@/components/loading-lottie";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useCredentialsUnavailableReason, useCredentialsWriteReason, useGuestOverview } from "@/hooks/use-guest-credentials";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useBreakGlassStore } from "@/lib/break-glass-store";
import { credentialErrorCode, type GuestTarget } from "@/lib/guest-credentials/api";
import { credentialErrorMessage } from "@/lib/guest-credentials/copy";
import { credentialsScope } from "@/lib/guest-credentials/keys";
import { cn, formatInstanceType } from "@/lib/utils";
import { CredentialsStates } from "./credentials-states";
import { defaultKeyAccount, KeysSection } from "./keys-section";
import { SshServerSection } from "./ssh-server-section";
import { UsersSection } from "./users-section";

type Section = "users" | "keys" | "ssh";

export interface CredentialsPanelProps {
  host: string;
  project: string;
  instanceName: string;
  /** False while hidden, so a hidden pane runs no reads. */
  active?: boolean;
  /** Reachable from the states that point at another console tab. */
  onOpenConsoleTab?: (tab: "serial" | "graphics") => void;
  onHealth?: (status: "connecting" | "connected" | "error", detail?: string) => void;
  className?: string;
}

/**
 * Guest logins of one running Linux instance, for someone who does not want to type commands:
 * local users and their passwords and sudo, SSH public keys, and the SSH server as read.
 */
export function CredentialsPanel({ host, project, instanceName, active = true, onOpenConsoleTab, onHealth, className }: CredentialsPanelProps) {
  const target = React.useMemo<GuestTarget>(() => ({ host, project, instanceName }), [host, instanceName, project]);
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  const unavailableReason = useCredentialsUnavailableReason(host, project, instanceName);
  const writeReason = useCredentialsWriteReason(host, project, instanceName);
  const overview = useGuestOverview(target, active && !unavailableReason);
  const [section, setSection] = React.useState<Section>("users");
  const [keyUser, setKeyUser] = React.useState<string | null>(null);

  const code = credentialErrorCode(overview.error);
  React.useEffect(() => {
    if (overview.isError) onHealth?.("error", code);
    else if (overview.isSuccess) onHealth?.("connected");
    else onHealth?.("connecting");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overview.isError, overview.isSuccess, code]);

  const data = overview.data;
  const keyAccount =
    data && keyUser && data.accounts.some((account) => account.name === keyUser) ? keyUser : data ? defaultKeyAccount(data) : null;

  const requestAccess = async () => {
    const granted = await useBreakGlassStore.getState().request(host, instanceName);
    if (granted) void overview.refetch();
  };

  // important: refetch rather than invalidate. An invalidation swallows a failed read, which the
  // button would then report as a success. Only the views on screen are read again.
  const refresh = () =>
    queryClient.refetchQueries({ queryKey: credentialsScope(scopeKey, target), type: "active" }, { throwOnError: true });

  let body: React.ReactNode;
  if (unavailableReason) {
    body = <EmptyState icon={Lock} title="Credentials are not available here" description={unavailableReason} />;
  } else if (overview.isLoading) {
    body = (
      <div className="flex flex-col items-center gap-3 py-12 text-sm text-muted-foreground" role="status">
        <LoadingLottieMedium />
        Reading the accounts of {instanceName}…
      </div>
    );
  } else if (!data) {
    body = (
      <CredentialsStates
        target={target}
        error={overview.error}
        subject="this guest's accounts"
        onRetry={() => void overview.refetch()}
        onRequestAccess={() => void requestAccess()}
        onOpenConsoleTab={onOpenConsoleTab}
      />
    );
  }

  const sectionClass = "mt-0 min-h-0 flex-1 overflow-y-auto overscroll-contain p-3 focus-visible:ring-inset focus-visible:ring-offset-0";

  return (
    <Tabs
      value={section}
      onValueChange={(next) => setSection(next as Section)}
      className={cn("flex h-full min-h-0 flex-col bg-background", className)}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-3 py-2">
        {/* min-w-40: at 390 px the tabs wrap onto their own line instead of squeezing the title. */}
        <div className="min-w-40 flex-1">
          <h2 className="text-sm font-semibold">Credentials</h2>
          <p className="break-words text-xs text-muted-foreground">
            {data ? `${data.guest.os_name || "Linux"} · ${formatInstanceType(data.guest.instance_type)}` : instanceName}
          </p>
        </div>
        {data && (
          <TabsList aria-label="Credential sections">
            <TabsTrigger value="users">Users</TabsTrigger>
            <TabsTrigger value="keys">SSH Keys</TabsTrigger>
            <TabsTrigger value="ssh">SSH Server</TabsTrigger>
          </TabsList>
        )}
        <RefreshButton
          label="Refresh credentials"
          onRefresh={refresh}
          lastUpdatedAt={overview.dataUpdatedAt || null}
          hideTimestamp
          disabled={!!unavailableReason}
        />
      </div>

      {data && writeReason && (
        <p className="border-b px-3 py-1.5 text-xs text-muted-foreground" role="note">{writeReason}. Changes are not offered right now.</p>
      )}
      {data && overview.error && (
        <Alert variant="warning" className="rounded-none border-x-0 border-t-0 py-2">
          <AlertDescription className="flex flex-wrap items-center gap-2 text-xs">
            The accounts could not be read again, so what is shown may be out of date.{" "}
            {credentialErrorMessage(overview.error, "The guest did not answer.")}
            <Button size="sm" variant="outline" className="h-6 px-2 text-xs" onClick={() => void overview.refetch()}>Retry</Button>
          </AlertDescription>
        </Alert>
      )}

      {body ? (
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-3">{body}</div>
      ) : data ? (
        <>
          <TabsContent value="users" className={sectionClass}>
            <UsersSection overview={data} target={target} writeReason={writeReason} />
          </TabsContent>
          <TabsContent value="keys" className={sectionClass}>
            <KeysSection
              overview={data}
              target={target}
              active={active}
              user={keyAccount}
              onUser={setKeyUser}
              writeReason={writeReason}
              onRequestAccess={() => void requestAccess()}
              onOpenConsoleTab={onOpenConsoleTab}
            />
          </TabsContent>
          <TabsContent value="ssh" className={sectionClass}>
            <SshServerSection
              target={target}
              active={active}
              exampleUser={keyAccount}
              onRequestAccess={() => void requestAccess()}
              onOpenConsoleTab={onOpenConsoleTab}
            />
          </TabsContent>
        </>
      ) : null}
    </Tabs>
  );
}
