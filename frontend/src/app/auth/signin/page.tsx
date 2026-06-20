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
import { Suspense, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Loader2, LogIn, ShieldCheck, AlertCircle } from "lucide-react";

// Auth backend selection (mini vs full).
const AUTH_MODE = process.env.NEXT_PUBLIC_AUTH_MODE || "authentik";
const IS_CERT_MODE = AUTH_MODE === "client-cert";
const PROVIDER_ID = IS_CERT_MODE ? "client-cert" : "authentik";

// Error messages mapping
const errorMessages: Record<string, string> = {
  OAuthSignin: "Error starting OAuth sign-in flow. Please try again.",
  OAuthCallback: "Error during OAuth callback. Please try again.",
  OAuthCreateAccount: "Could not create account. Please contact support.",
  EmailCreateAccount: "Could not create account with email.",
  Callback: "Authentication callback error. Please try again.",
  OAuthAccountNotLinked:
    "This email is linked to another account. Please use that method.",
  EmailSignin: "Error sending sign-in email.",
  CredentialsSignin: "Invalid credentials. Please check and try again.",
  SessionRequired: "Please sign in to access this page.",
  Default: "An authentication error occurred. Please try again.",
};

function SignInContent() {
  const searchParams = useSearchParams();
  const error = searchParams.get("error");
  const callbackUrl = searchParams.get("callbackUrl") || "/";
  const [isLoading, setIsLoading] = useState(false);
  const [autoRedirect, setAutoRedirect] = useState(true);

  // Auto-redirect to Authentik after a short delay
  useEffect(() => {
    if (!error && autoRedirect) {
      const timer = setTimeout(() => {
        handleSignIn();
      }, 1500);
      return () => clearTimeout(timer);
    }
  }, [error, autoRedirect]);

  const handleSignIn = async () => {
    setIsLoading(true);
    setAutoRedirect(false);
    try {
      await signIn(PROVIDER_ID, { callbackUrl });
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
          {/* Logo */}
          <div className="mx-auto w-16 h-16 bg-primary/10 rounded-2xl flex items-center justify-center">
            <ShieldCheck className="w-8 h-8 text-primary" />
          </div>

          <div>
            <CardTitle className="text-2xl font-bold">
              Orcastra Dashboard
            </CardTitle>
            <CardDescription className="mt-2">
              {IS_CERT_MODE
                ? "Sign in with your client certificate to continue"
                : "Sign in with your organization account to continue"}
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
                Redirecting to sign in...
              </p>
            </div>
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
                  Redirecting...
                </>
              ) : (
                <>
                  <LogIn className="mr-2 h-5 w-5" />
                  {IS_CERT_MODE ? "Sign in with Certificate" : "Sign in with SSO"}
                </>
              )}
            </Button>
          )}

          {/* Footer */}
          <div className="text-center text-xs text-muted-foreground">
            <p>
              Contact your administrator if you need access
            </p>
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
