"use client";

/**
 * Sign In Page
 *
 * Custom sign-in page that redirects to Authentik for authentication.
 * Features:
 * - Clean, branded UI
 * - Automatic redirect to Authentik
 * - Error handling for failed authentication
 */

import { signIn } from "next-auth/react";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Loader2, LogIn, AlertCircle } from "lucide-react";
import { AuthBrandMark, BUILT_IN_NAME, useAuthFooterText } from "@/components/branding/auth-brand-mark";
import { usePublicBranding } from "@/components/branding/public-branding-context";
import { safeCallbackPath } from "@/lib/auth-redirect";
import { forgetReturnPath, recallReturnPath, rememberReturnPath } from "@/lib/auth-return";

// Error messages mapping
const errorMessages: Record<string, string> = {
  OAuthSignin: "Error starting OAuth sign-in flow. Try again.",
  OAuthCallback: "Error during OAuth callback. Try again.",
  OAuthCreateAccount: "Could not create account. Contact support.",
  EmailCreateAccount: "Could not create account with email.",
  Callback: "Authentication callback error. Try again.",
  OAuthAccountNotLinked:
    "This email is linked to another account. Use that method.",
  EmailSignin: "Error sending sign-in email.",
  CredentialsSignin: "Invalid credentials. Check and try again.",
  SessionRequired: "Sign in to access this page.",
  Default: "An authentication error occurred. Try again.",
};

// One automatic hand-off to the identity provider per tab within this window. Coming back here
// without an error sooner than that means the sign-in did not stick (a cookie the browser
// refused, a clock skew), and redirecting again would only loop between the two hosts.
const AUTO_SIGNIN_KEY = "orcastra-auto-signin-at";
const AUTO_SIGNIN_LOOP_MS = 10_000;

function autoSignInBounced(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const last = Number(window.sessionStorage.getItem(AUTO_SIGNIN_KEY) || 0);
    return Date.now() - last < AUTO_SIGNIN_LOOP_MS;
  } catch {
    return false;
  }
}

function markAutoSignIn(): void {
  try {
    window.sessionStorage.setItem(AUTO_SIGNIN_KEY, String(Date.now()));
  } catch {
    // Storage blocked: the redirect still happens, only the loop guard is lost.
  }
}

function SignInContent() {
  const branding = usePublicBranding();
  const footerText = useAuthFooterText();
  const searchParams = useSearchParams();
  const error = searchParams.get("error");
  const requested = searchParams.get("callbackUrl");
  // note: read once, on the client. TokenSync renders nothing on the server, so this page's first
  // render is always in the browser.
  const [bounced] = useState(() => !error && autoSignInBounced());
  const [isLoading, setIsLoading] = useState(!error && !bounced);
  const [autoRedirect, setAutoRedirect] = useState(!bounced);
  const autoStartedRef = useRef(false);

  // A failed sign-in comes back here (or via /auth/error) without its callbackUrl, so the page
  // asked for is kept for the tab and recalled on the retry. A plain visit forgets a stale one.
  useEffect(() => {
    if (requested) rememberReturnPath(safeCallbackPath(requested, window.location.origin));
    else if (!error) forgetReturnPath();
  }, [requested, error]);

  const resolveCallbackUrl = () =>
    safeCallbackPath(requested, window.location.origin) ?? (error ? recallReturnPath() : null) ?? "/";

  // Hand off to Authentik straight away. It used to wait a fixed 1.5 s first, which every sign-in
  // and every return after an expired session paid, for no recorded reason. The ref keeps React's
  // development double effect from starting a second attempt.
  useEffect(() => {
    if (error || !autoRedirect || autoStartedRef.current) return;
    autoStartedRef.current = true;
    markAutoSignIn();
    void handleSignIn();
  }, [error, autoRedirect]);

  const handleSignIn = async () => {
    setIsLoading(true);
    setAutoRedirect(false);
    try {
      await signIn("authentik", { callbackUrl: resolveCallbackUrl() });
    } catch (err) {
      console.error("Sign in error:", err);
      setIsLoading(false);
    }
  };

  const errorMessage = error ? errorMessages[error] || errorMessages.Default : null;

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-background via-background to-muted/20 p-4">
      <Card className="w-full max-w-md shadow-xl border-border/50">
        <CardHeader className="text-center space-y-4">
          <AuthBrandMark />

          <div>
            <CardTitle className="text-2xl font-bold">
              {branding?.app_name ?? BUILT_IN_NAME}
            </CardTitle>
            <CardDescription className="mt-2">
              Sign in with your organization account to continue
            </CardDescription>
          </div>
        </CardHeader>

        <CardContent className="space-y-6">
          {/* Error Alert */}
          {errorMessage && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{errorMessage}</AlertDescription>
            </Alert>
          )}

          {/* Loading State */}
          {isLoading && !error && (
            <div className="flex flex-col items-center gap-4 py-8">
              <Loader2 className="h-8 w-8 animate-spin text-primary" />
              <p className="text-sm text-muted-foreground">
                Redirecting to sign in…
              </p>
            </div>
          )}

          {bounced && !error && (
            <p className="text-center text-sm text-muted-foreground">
              Automatic sign-in did not finish. Sign in to try again.
            </p>
          )}

          {/* Sign In Button */}
          {(!isLoading || error) && (
            <Button
              onClick={handleSignIn}
              disabled={isLoading}
              className="w-full h-12 text-base font-medium"
              size="lg"
            >
              {isLoading ? (
                <>
                  <Loader2 className="mr-2 h-5 w-5 animate-spin" />
                  Redirecting…
                </>
              ) : (
                <>
                  <LogIn className="mr-2 h-5 w-5" />
                  Sign in with SSO
                </>
              )}
            </Button>
          )}

          {/* Footer */}
          <div className="space-y-1 text-center text-xs text-muted-foreground">
            <p>
              Contact your administrator if you need access
            </p>
            {footerText && <p>{footerText}</p>}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

export default function SignInPage() {
  return (
    <Suspense
      fallback={
        <div className="min-h-screen flex items-center justify-center">
          <Loader2 className="h-8 w-8 animate-spin text-primary" />
        </div>
      }
    >
      <SignInContent />
    </Suspense>
  );
}
