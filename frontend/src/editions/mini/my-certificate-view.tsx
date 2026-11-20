"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { KeyRound, RefreshCw } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { EMPTY_VALUE, formatDate } from "@/lib/utils";
import { miniApi, type IssuedBundle } from "./api";
import { BundleReveal } from "./operators/bundle-reveal";

const ROLE_LABEL: Record<string, string> = { admin: "Administrator", partner: "Partner", tenant: "Tenant" };

export function MyCertificateView() {
  const { scopeKey } = useActorQueryScope();
  const query = useQuery({ queryKey: ["mini-my-certificate", scopeKey], queryFn: () => miniApi.myCertificate() });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [renewed, setRenewed] = useState<IssuedBundle | null>(null);

  const renew = async () => {
    setBusy(true);
    setError(null);
    try {
      setRenewed(await miniApi.renewMyCertificate());
      await query.refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The certificate could not be renewed.");
    } finally {
      setBusy(false);
    }
  };

  if (query.isLoading) return <LoadingLottiePage />;
  if (query.isError || !query.data) {
    return <ReadErrorState noun="your certificate" error={query.error} onRetry={() => query.refetch()} />;
  }
  const me = query.data;
  const facts: [string, React.ReactNode][] = [
    ["Email", me.email],
    ["Role", ROLE_LABEL[me.role] ?? me.role],
    ["Certificate", <span key="cn" translate="no">{me.common_name ?? EMPTY_VALUE}</span>],
    ["Expires", me.not_after ? `${formatDate(me.not_after)} (${me.days_left} days left)` : EMPTY_VALUE],
    ["Fingerprint", <code key="fp" className="break-all font-mono text-xs" translate="no">{me.fingerprint ?? EMPTY_VALUE}</code>],
    ["Other Active Certificates", String(me.other_active_certificates)],
  ];

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h1 className="text-3xl font-bold">My Certificate</h1>
        <p className="mt-2 text-muted-foreground">The certificate this browser signed in with</p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>{me.display_name || me.email}</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-[12rem_1fr]">
            {facts.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="text-sm text-muted-foreground">{label}</dt>
                <dd className="min-w-0 text-sm">{value}</dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><KeyRound className="h-5 w-5" aria-hidden="true" /> Renew</CardTitle>
          <CardDescription>
            A renewal gives you a new certificate for the same account. The current one keeps working
            until you sign in with the new one, so nothing is cut off while you switch.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {renewed ? (
            <>
              <BundleReveal bundle={renewed} holder="yourself" />
              <p className="text-sm text-muted-foreground">
                Import the new file, close every window of the browser, open it again, and choose the new certificate.
              </p>
            </>
          ) : me.renew_available ? (
            <Button onClick={renew} disabled={busy}>
              <RefreshCw className="mr-2 h-4 w-4" aria-hidden="true" /> {busy ? "Renewing…" : "Renew Certificate"}
            </Button>
          ) : (
            <p className="text-sm text-muted-foreground">
              {me.renew_opens_at && new Date(me.renew_opens_at) > new Date()
                ? `Renewal opens on ${formatDate(me.renew_opens_at)}.`
                : "This certificate was already renewed, or renewal is turned off. Ask an Administrator for a new certificate if you need one."}
            </p>
          )}
          {error && <Alert variant="destructive"><AlertDescription role="alert">{error}</AlertDescription></Alert>}
        </CardContent>
      </Card>
    </div>
  );
}
