"use client";

import { useState } from "react";
import { signIn } from "next-auth/react";
import { KeyRound, Loader2, LogIn, LogOut, RefreshCw, UserX } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { IllustrationFallback } from "@/components/illustrations/illustration-frame";
import { AccessGateIllustration } from "@/components/illustrations/scenes/states";
import type { AccessGateState, AccessRecheck } from "@/hooks/use-access-gate";
import { federatedSignOut } from "@/lib/sign-out";
import { formatDateTime } from "@/lib/utils";

// Issue #17: a signed-in account Orcastra has given nothing used to see an empty dashboard that
// read as a broken deployment. This replaces the whole shell, so none of the dashboard's global
// reads (task panel, read-only mode, create menu) fire for a caller the backend refuses anyway.

const COPY: Record<Exclude<AccessGateState, "allowed" | "checking">, { title: string; body: string }> = {
  no_role: {
    title: "No role assigned yet",
    body: "Your account has no role in Orcastra yet. Contact your administrator to have a role assigned, then choose Check again.",
  },
  no_tenant_access: {
    title: "No active access",
    body: "Your account has no active access to any cluster. Contact your provider to restore access, then choose Check again.",
  },
};

const RECHECK_RESULT: Record<Exclude<AccessRecheck, "changed">, string> = {
  unchanged: "Still no access. Your administrator's change has not reached this session yet.",
  failed: "Could not check right now. Try again in a moment, or sign in again.",
};

interface AccessBlockedNoticeProps {
  state: Exclude<AccessGateState, "allowed" | "checking">;
  email: string | null;
  /** When the caller's access ended by its paid term, if it did. */
  endedAt?: string | null;
  rechecking: boolean;
  onCheckAgain: () => Promise<AccessRecheck>;
}

export function AccessBlockedNotice({ state, email, endedAt, rechecking, onCheckAgain }: AccessBlockedNoticeProps) {
  const [result, setResult] = useState<string | null>(null);
  const [leaving, setLeaving] = useState<"sign-in" | "sign-out" | null>(null);
  const { title, body } = COPY[state];
  const Icon = state === "no_role" ? UserX : KeyRound;
  const busy = rechecking || leaving !== null;

  const checkAgain = async () => {
    setResult(null);
    const outcome = await onCheckAgain();
    setResult(outcome === "changed" ? null : RECHECK_RESULT[outcome]);
  };

  const signInAgain = () => {
    setLeaving("sign-in");
    void signIn("authentik", { callbackUrl: `${window.location.pathname}${window.location.search}` });
  };

  const signOut = async () => {
    setLeaving("sign-out");
    try {
      await federatedSignOut();
    } catch {
      setLeaving(null);
    }
  };

  return (
    <main className="grid min-h-[100dvh] place-items-center bg-background p-4">
      <Card className="w-full max-w-md">
        <CardContent className="space-y-6 p-6 sm:p-8">
          <div className="space-y-3 text-center">
            <AccessGateIllustration size="md" className="mx-auto" />
            <IllustrationFallback>
              <Icon className="mx-auto h-10 w-10 text-muted-foreground" aria-hidden="true" />
            </IllustrationFallback>
            <h1 className="text-xl font-semibold">{title}</h1>
            <p className="text-sm text-muted-foreground">{body}</p>
            {state === "no_tenant_access" && endedAt && (
              <p className="text-sm text-muted-foreground">Your access ended on {formatDateTime(endedAt)}.</p>
            )}
            {email && (
              <p className="text-sm">
                Signed in as <span className="font-medium break-all">{email}</span>
              </p>
            )}
          </div>

          <div className="flex flex-col gap-2">
            <Button onClick={checkAgain} disabled={busy}>
              {rechecking ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
              Check again
            </Button>
            <div className="grid grid-cols-2 gap-2">
              <Button variant="outline" onClick={signInAgain} disabled={busy}>
                {leaving === "sign-in" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <LogIn className="mr-2 h-4 w-4" />}
                Sign in again
              </Button>
              <Button variant="outline" onClick={signOut} disabled={busy}>
                {leaving === "sign-out" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <LogOut className="mr-2 h-4 w-4" />}
                Sign out
              </Button>
            </div>
          </div>

          <p role="status" aria-live="polite" className="min-h-5 text-center text-sm text-muted-foreground">
            {result ?? "Changes can take up to 5 minutes to reach this page. If Check again does not help, sign in again."}
          </p>
        </CardContent>
      </Card>
    </main>
  );
}
