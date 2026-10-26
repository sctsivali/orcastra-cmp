import { signOut } from "next-auth/react";
import { IDP_LOGOUT_URL } from "@/lib/identity-provider";

/**
 * Sign out of the dashboard and of the SSO session behind it.
 *
 * Clears the NextAuth session first, then ends the Authentik session so the next sign-in asks for
 * credentials again rather than silently reusing the same account.
 */
export async function federatedSignOut(): Promise<void> {
  await signOut({ redirect: false });
  const postLogoutRedirect = encodeURIComponent(`${window.location.origin}/auth/signin`);
  window.location.href = `${IDP_LOGOUT_URL}?post_logout_redirect_uri=${postLogoutRedirect}`;
}
