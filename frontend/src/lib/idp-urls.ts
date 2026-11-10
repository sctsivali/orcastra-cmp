// Addresses on the identity provider the console sends a browser to, derived from its end-session
// URL: the account settings page (Profile) and the end-session endpoint (sign-out).
//
// Alias-free with no runtime imports and no Node globals, because
// scripts/checks/check-idp-urls.mjs compiles this file on its own and exercises it.

// Every Authentik provider endpoint sits under this segment; whatever precedes it is the
// Authentik base, including a path prefix when Authentik is served under one.
const AUTHENTIK_PROVIDER_SEGMENT = "/application/o/";

// The value frontend/Dockerfile builds with, as URL parsing lower-cases it. It survives into the
// running bundle when the container starts without NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL.
const BUILD_PLACEHOLDER_HOST = "next_public_authentik_logout_url_placeholder";

function parseIdpUrl(value: string): URL | null {
  let url: URL;
  try {
    url = new URL(value.trim());
  } catch {
    return null;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  if (url.hostname === BUILD_PLACEHOLDER_HOST) return null;
  return url;
}

/**
 * The account settings page of the Authentik that serves `logoutUrl`, or null when the value
 * is unset, still the build placeholder, not http(s), or not an Authentik endpoint.
 *
 * Derived from the end-session URL, the one IdP address every deployment sets; both live on the
 * same Authentik. Only the origin and the path prefix are kept, so credentials and query
 * parameters in the value never reach the link.
 */
export function idpAccountSettingsUrl(logoutUrl: string): string | null {
  const url = parseIdpUrl(logoutUrl);
  if (!url) return null;
  const marker = url.pathname.indexOf(AUTHENTIK_PROVIDER_SEGMENT);
  if (marker < 0) return null;
  return `${url.origin}${url.pathname.slice(0, marker)}/if/user/#/settings`;
}

/**
 * The end-session endpoint with `postLogoutRedirect` as its return address, or null when the
 * value is unset, still the build placeholder, or not http(s).
 *
 * Accepts any OIDC end-session endpoint. A query the operator set (ex; a client_id) is kept, a
 * return address already in it is replaced instead of sent twice, and credentials are dropped.
 */
export function idpEndSessionUrl(logoutUrl: string, postLogoutRedirect: string): string | null {
  const url = parseIdpUrl(logoutUrl);
  if (!url) return null;
  url.username = "";
  url.password = "";
  url.searchParams.set("post_logout_redirect_uri", postLogoutRedirect);
  return url.toString();
}
