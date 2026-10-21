import { signOut } from "next-auth/react";

// Authentik end_session endpoint for federated logout
// Uses environment variable to avoid hardcoded URLs
const AUTHENTIK_LOGOUT_URL = process.env.NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL || "";

/**
 * Sign out of the dashboard and of the SSO session behind it.
 *
 * Clears the NextAuth session first, then ends the Authentik session so the next sign-in asks for
 * credentials again rather than silently reusing the same account.
 */
export async function federatedSignOut(): Promise<void> {
  await signOut({ redirect: false });
  const postLogoutRedirect = encodeURIComponent(`${window.location.origin}/auth/signin`);
  window.location.href = `${AUTHENTIK_LOGOUT_URL}?post_logout_redirect_uri=${postLogoutRedirect}`;
}
