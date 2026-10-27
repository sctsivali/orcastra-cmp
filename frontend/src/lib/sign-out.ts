import { signOut } from "next-auth/react";
import { IDP_LOGOUT_URL } from "@/lib/identity-provider";
import { idpEndSessionUrl } from "@/lib/idp-urls";
import { clearPersonalizationHint } from "@/lib/personalization-cookie";

/**
 * Sign out of the dashboard and of the SSO session behind it.
 *
 * Clears the NextAuth session first, then ends the Authentik session so the next sign-in asks for
 * credentials again rather than silently reusing the same account.
 */
export async function federatedSignOut(): Promise<void> {
  await signOut({ redirect: false });
  clearPersonalizationHint();
  const signInPage = `${window.location.origin}/auth/signin`;
  const endSession = idpEndSessionUrl(IDP_LOGOUT_URL, signInPage);
  if (!endSession) {
    // important: without a usable end-session URL only the console session ends. The IdP session
    // stays, so the next sign-in on this browser may not ask for a password. Landing on the
    // sign-in page beats navigating to the unreplaced build placeholder, which is a dead host.
    console.warn(
      "[sign-out] NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL is not a usable URL; only the console session was ended"
    );
  }
  window.location.href = endSession ?? signInPage;
}
