// The identity provider's account settings page, derived from its end-session URL.
//
// important: alias-free with no runtime imports and no Node globals.
// scripts/checks/check-idp-account-url.mjs compiles this file on its own and exercises it.

// Every Authentik provider endpoint sits under this segment; whatever precedes it is the
// Authentik base, including a path prefix when Authentik is served under one.
const AUTHENTIK_PROVIDER_SEGMENT = "/application/o/";

// The value frontend/Dockerfile builds with, as URL parsing lower-cases it. It survives into the
// running bundle when the container starts without NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL.
const BUILD_PLACEHOLDER_HOST = "next_public_authentik_logout_url_placeholder";

/**
 * The account settings page of the Authentik that serves `logoutUrl`, or null when the value
 * is unset, still the build placeholder, not http(s), or not an Authentik endpoint.
 *
 * Derived from the end-session URL rather than configured separately: it is the one IdP address
 * every deployment already sets, and both live on the same Authentik. Only the origin and the
 * path prefix are kept, so credentials and query parameters in the value never reach the link.
 */
export function idpAccountSettingsUrl(logoutUrl: string): string | null {
  let url: URL;
  try {
    url = new URL(logoutUrl.trim());
  } catch {
    return null;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  if (url.hostname === BUILD_PLACEHOLDER_HOST) return null;
  const marker = url.pathname.indexOf(AUTHENTIK_PROVIDER_SEGMENT);
  if (marker < 0) return null;
  return `${url.origin}${url.pathname.slice(0, marker)}/if/user/#/settings`;
}
