"use client";

/**
 * Authentication Error Page
 *
 * Displays user-friendly error messages for authentication failures.
 */

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
import { AlertTriangle, ArrowLeft, LogIn, Loader2 } from "lucide-react";
import Link from "next/link";
import { recallReturnPath } from "@/lib/auth-return";
import { AuthBrandMark } from "@/components/branding/auth-brand-mark";
import { IllustrationFallback } from "@/components/illustrations/illustration-frame";
import { AccessGateIllustration } from "@/components/illustrations/scenes/states";

// Error details mapping
const errorDetails: Record<string, { title: string; description: string }> = {
  Configuration: {
    title: "Authentication Configuration Error",
    description:
      "There is a problem with the authentication configuration. Contact your administrator.",
  },
  AccessDenied: {
    title: "Access Denied",
    description:
      "You do not have permission to access this application. Contact your administrator for access.",
  },
  Verification: {
    title: "Verification Failed",
    description:
      "The verification link may have expired or already been used. Try signing in again.",
  },
  OAuthSignin: {
    title: "OAuth Sign-in Error",
    description:
      "There was an error starting the authentication process. Try again.",
  },
  OAuthCallback: {
    title: "OAuth Callback Error",
    description:
      "There was an error processing the authentication response. Try again.",
  },
  OAuthCreateAccount: {
    title: "Account Creation Failed",
    description:
      "Could not create your account. Contact your administrator.",
  },
  OAuthAccountNotLinked: {
    title: "Account Not Linked",
    description:
      "This email is already associated with another sign-in method. Use that method instead.",
  },
  SessionRequired: {
    title: "Session Required",
    description: "You need to be signed in to access this page.",
  },
  Default: {
    title: "Authentication Error",
    description:
      "An unexpected error occurred during authentication. Try again.",
  },
};

function ErrorContent() {
  const searchParams = useSearchParams();
  const error = searchParams.get("error") || "Default";
  const { title, description } = errorDetails[error] || errorDetails.Default;
  // note: read after mount, because the server render has no tab storage to agree with.
  const [signInHref, setSignInHref] = useState("/auth/signin");
  useEffect(() => {
    const returnPath = recallReturnPath();
    if (returnPath) setSignInHref(`/auth/signin?${new URLSearchParams({ callbackUrl: returnPath })}`);
  }, []);

  return (
    <div className="min-h-screen flex flex-col items-center justify-center gap-6 bg-gradient-to-br from-background via-background to-muted/20 p-4">
      {/* The error has its own icon below; the deployment's logo is shown only when one is set. */}
      <AuthBrandMark withoutLogo="nothing" />
      <Card className="w-full max-w-md shadow-xl border-border/50">
        <CardHeader className="text-center space-y-4">
          {/* The scene names the situation; the icon comes back when illustrations are hidden. */}
          <AccessGateIllustration size="md" className="mx-auto" />
          <IllustrationFallback>
            <span className="mx-auto w-16 h-16 bg-destructive/10 rounded-2xl flex items-center justify-center">
              <AlertTriangle className="w-8 h-8 text-destructive" aria-hidden="true" />
            </span>
          </IllustrationFallback>

          <div>
            <CardTitle className="text-2xl font-bold">{title}</CardTitle>
            <CardDescription className="mt-2">{description}</CardDescription>
          </div>
        </CardHeader>

        <CardContent className="space-y-4">
          {/* Action Buttons */}
          <div className="flex flex-col gap-3">
            <Link href={signInHref} className="w-full">
              <Button className="w-full" size="lg">
                <LogIn className="mr-2 h-4 w-4" />
                Back to sign in
              </Button>
            </Link>

            <Link href="/" className="w-full">
              <Button variant="outline" className="w-full" size="lg">
                <ArrowLeft className="mr-2 h-4 w-4" />
                Go to Homepage
              </Button>
            </Link>
          </div>

          {/* Error Code */}
          <div className="text-center text-xs text-muted-foreground pt-4 border-t">
            <p>Error Code: {error}</p>
            <p className="mt-1">
              If this problem persists, contact support
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

export default function AuthErrorPage() {
  return (
    <Suspense
      fallback={
        <div className="min-h-screen flex items-center justify-center">
          <Loader2 className="h-8 w-8 animate-spin text-primary" />
        </div>
      }
    >
      <ErrorContent />
    </Suspense>
  );
}
