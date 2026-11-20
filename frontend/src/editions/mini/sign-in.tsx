"use client";

/**
 * Mini's sign-in page: the browser's client certificate is the credential.
 *
 * It first asks the backend what this browser presented, then explains the one next step: import
 * a certificate, show an unenrolled certificate's fingerprint to report, enroll the first Administrator
 * with the bootstrap token, or sign in. A visit that was redirected here from a page signs in on its
 * own; a plain visit (after signing out, for one) waits for the button, so signing out sticks.
 */

import { signIn } from "next-auth/react";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Check, Copy, KeyRound, Loader2, LogIn, RotateCw, ShieldAlert, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { AuthBrandMark, BUILT_IN_NAME, useAuthFooterText } from "@/components/branding/auth-brand-mark";
import { usePublicBranding } from "@/components/branding/public-branding-context";
import { safeCallbackPath } from "@/lib/auth-redirect";
import { AUTH_PROVIDER_ID } from "@/lib/edition";
import { API_URL } from "@/lib/api";
import { errorText, type CertStatus } from "./api";

// The two public sign-in calls, answered by the backend through nginx without a session.
async function certStatus(): Promise<CertStatus> {
  const response = await fetch(`${API_URL}/auth/cert-status`, { cache: "no-store" });
  if (!response.ok) throw new Error(await errorText(response, "The sign-in status could not be read."));
  return (await response.json()) as CertStatus;
}

async function enrollFirstAdmin(body: { bootstrap_token: string; email?: string; display_name?: string }): Promise<void> {
  const response = await fetch(`${API_URL}/auth/cert-bootstrap`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await errorText(response, "The certificate could not be enrolled."));
}


const AUTO_KEY = "orcastra-mini-auto-signin-at";
const AUTO_LOOP_MS = 10_000;

function autoBounced(): boolean {
  try {
    return Date.now() - Number(window.sessionStorage.getItem(AUTO_KEY) || 0) < AUTO_LOOP_MS;
  } catch {
    return false;
  }
}

function markAuto(): void {
  try {
    window.sessionStorage.setItem(AUTO_KEY, String(Date.now()));
  } catch {
    // Storage blocked: only the loop guard is lost.
  }
}

function Fingerprint({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="space-y-2">
      <Label htmlFor="mini-fingerprint">Certificate Fingerprint (SHA-256)</Label>
      <div className="flex items-start gap-2">
        <code id="mini-fingerprint" translate="no"
          className="min-w-0 flex-1 break-all rounded-md border bg-muted/40 px-3 py-2 font-mono text-xs">
          {value}
        </code>
        <Button type="button" variant="outline" size="icon" onClick={copy}
          aria-label={copied ? "Fingerprint copied" : "Copy fingerprint"}>
          {copied ? <Check className="h-4 w-4" aria-hidden="true" /> : <Copy className="h-4 w-4" aria-hidden="true" />}
        </Button>
      </div>
      <p aria-live="polite" className="sr-only">{copied ? "Fingerprint copied" : ""}</p>
    </div>
  );
}

function ImportSteps() {
  return (
    <div className="space-y-3 text-sm">
      <p>Your browser did not offer a certificate. Import the <span translate="no">.p12</span> file you were given, then reload this page:</p>
      <ul className="list-disc space-y-1.5 pl-5 text-muted-foreground">
        <li><span className="font-medium text-foreground">Windows:</span> open the file, choose Current User, and enter its password.</li>
        <li><span className="font-medium text-foreground">macOS:</span> open the file to add it to the login keychain, and enter its password.</li>
        <li><span className="font-medium text-foreground">Linux, or Firefox anywhere:</span> browser settings, Certificates, Your Certificates, Import.</li>
      </ul>
      <p className="text-muted-foreground">The browser asks which certificate to use on the next visit. If it does not, close every window of the browser and open it again.</p>
    </div>
  );
}

function BootstrapForm({ onEnrolled }: { onEnrolled: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tokenRef = useRef<HTMLInputElement>(null);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const token = String(form.get("bootstrap_token") || "").trim();
    if (token.length < 16) {
      setError("Enter the bootstrap token from the installer or the deployment's .env file.");
      tokenRef.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await enrollFirstAdmin({
        bootstrap_token: token,
        email: String(form.get("email") || "").trim() || undefined,
        display_name: String(form.get("display_name") || "").trim() || undefined,
      });
      onEnrolled();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The certificate could not be enrolled.");
      setBusy(false);
      tokenRef.current?.focus();
    }
  };

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <Alert>
        <UserPlus className="h-4 w-4" aria-hidden="true" />
        <AlertDescription>
          No Administrator exists yet. Enroll this certificate as the first Administrator with the
          bootstrap token. It works once; clear it from the deployment afterwards.
        </AlertDescription>
      </Alert>
      <div className="space-y-2">
        <Label htmlFor="bootstrap_token">Bootstrap Token</Label>
        <Input ref={tokenRef} id="bootstrap_token" name="bootstrap_token" type="password" autoComplete="off"
          spellCheck={false} required aria-invalid={error ? true : undefined}
          aria-describedby={error ? "bootstrap-error" : undefined} placeholder="Paste the token…" />
      </div>
      <div className="space-y-2">
        <Label htmlFor="bootstrap_email">Email (Optional)</Label>
        <Input id="bootstrap_email" name="email" type="email" autoComplete="email" spellCheck={false}
          placeholder="admin@example.com…" />
      </div>
      <div className="space-y-2">
        <Label htmlFor="bootstrap_name">Display Name (Optional)</Label>
        <Input id="bootstrap_name" name="display_name" type="text" autoComplete="name" maxLength={255}
          placeholder="Ops Lead…" />
      </div>
      {error && (
        <p id="bootstrap-error" role="alert" className="text-sm text-destructive">{error}</p>
      )}
      <Button type="submit" className="w-full" disabled={busy}>
        {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" /> : <KeyRound className="mr-2 h-4 w-4" aria-hidden="true" />}
        {busy ? "Enrolling…" : "Enroll as Administrator"}
      </Button>
    </form>
  );
}

function MiniSignInContent() {
  const branding = usePublicBranding();
  const footerText = useAuthFooterText();
  const params = useSearchParams();
  const error = params.get("error");
  const requested = params.get("callbackUrl");
  const [status, setStatus] = useState<CertStatus | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [signingIn, setSigningIn] = useState(false);
  const autoStarted = useRef(false);

  const load = useCallback(async () => {
    setReadError(null);
    try {
      setStatus(await certStatus());
    } catch (err) {
      setReadError(err instanceof Error ? err.message : "The sign-in status could not be read.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const callbackUrl = () => safeCallbackPath(requested, window.location.origin) ?? "/";

  const startSignIn = useCallback(async () => {
    setSigningIn(true);
    try {
      await signIn(AUTH_PROVIDER_ID, { callbackUrl: callbackUrl() });
    } catch {
      setSigningIn(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requested]);

  // A redirect from a page (it carries callbackUrl) signs in straight away; a plain visit waits.
  useEffect(() => {
    if (!status?.active || !requested || error || autoStarted.current || autoBounced()) return;
    autoStarted.current = true;
    markAuto();
    void startSignIn();
  }, [status, requested, error, startSignIn]);

  const failed = error
    ? "The certificate sign-in did not finish. If your certificate was just issued or renewed, reload the page and choose it."
    : null;

  let body: React.ReactNode;
  if (readError) {
    body = (
      <div className="space-y-4">
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>{readError}</AlertDescription>
        </Alert>
        <Button variant="outline" className="w-full" onClick={load}>
          <RotateCw className="mr-2 h-4 w-4" aria-hidden="true" /> Try Again
        </Button>
      </div>
    );
  } else if (!status) {
    body = (
      <div className="flex flex-col items-center gap-3 py-6" aria-live="polite">
        <Loader2 className="h-7 w-7 animate-spin text-primary" aria-hidden="true" />
        <p className="text-sm text-muted-foreground">Checking your certificate…</p>
      </div>
    );
  } else if (!status.certificate_presented) {
    body = (
      <div className="space-y-4">
        <ImportSteps />
        <Button variant="outline" className="w-full" onClick={() => window.location.reload()}>
          <RotateCw className="mr-2 h-4 w-4" aria-hidden="true" /> Reload
        </Button>
      </div>
    );
  } else if (!status.enrolled) {
    body = status.bootstrap_available ? (
      <div className="space-y-5">
        {status.fingerprint && <Fingerprint value={status.fingerprint} />}
        <BootstrapForm onEnrolled={() => void startSignIn()} />
      </div>
    ) : (
      <div className="space-y-4">
        <Alert>
          <ShieldAlert className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            This certificate is not enrolled. Ask an Administrator, or the Partner who runs your
            organization, for a certificate from the Operators page. If you report this, include the
            fingerprint below.
          </AlertDescription>
        </Alert>
        {status.fingerprint && <Fingerprint value={status.fingerprint} />}
      </div>
    );
  } else if (!status.active) {
    body = (
      <Alert variant="destructive">
        <ShieldAlert className="h-4 w-4" aria-hidden="true" />
        <AlertDescription>
          This certificate was revoked or has expired. Ask an Administrator for a new certificate, or
          choose another certificate by closing every window of the browser and opening it again.
        </AlertDescription>
      </Alert>
    );
  } else {
    const waiting = signingIn || (requested && !error && !autoBounced() && autoStarted.current);
    body = (
      <div className="space-y-4">
        {!requested && !error && (
          <p className="text-center text-sm text-muted-foreground">
            You are signed out. Sign in with the certificate below, or close every window of the browser
            to sign in with another one.
          </p>
        )}
        {status.common_name && (
          <p className="text-center text-sm">
            Certificate <span className="font-medium" translate="no">{status.common_name}</span>
          </p>
        )}
        <Button onClick={startSignIn} disabled={Boolean(waiting)} className="h-12 w-full text-base font-medium" size="lg">
          {waiting ? <Loader2 className="mr-2 h-5 w-5 animate-spin" aria-hidden="true" /> : <LogIn className="mr-2 h-5 w-5" aria-hidden="true" />}
          {waiting ? "Signing In…" : "Sign In with Certificate"}
        </Button>
      </div>
    );
  }

  return (
    <main className="flex min-h-[100dvh] items-center justify-center bg-gradient-to-br from-background via-background to-muted/20 p-4">
      <Card className="w-full max-w-md border-border/50 shadow-xl">
        <CardHeader className="space-y-4 text-center">
          <AuthBrandMark />
          <div>
            <CardTitle className="text-2xl font-bold">{branding?.app_name ?? BUILT_IN_NAME}</CardTitle>
            <CardDescription className="mt-2">Sign in with your client certificate</CardDescription>
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          {failed && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" aria-hidden="true" />
              <AlertDescription>{failed}</AlertDescription>
            </Alert>
          )}
          {body}
          {footerText && <p className="text-center text-xs text-muted-foreground">{footerText}</p>}
        </CardContent>
      </Card>
    </main>
  );
}

export function MiniSignIn(): JSX.Element {
  return (
    <Suspense fallback={<main className="flex min-h-[100dvh] items-center justify-center"><Loader2 className="h-8 w-8 animate-spin text-primary" aria-hidden="true" /></main>}>
      <MiniSignInContent />
    </Suspense>
  );
}
