import { idpAccountSettingsUrl } from "@/lib/idp-urls";

/**
 * The Authentik end-session endpoint of this deployment. The image is built with a placeholder
 * that frontend/entrypoint.sh swaps for NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL at container start, so
 * each install, ours or a partner's, carries its own IdP address without a rebuild.
 */
export const IDP_LOGOUT_URL = process.env.NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL || "";

/** Where Profile in the user menu leads; null when this deployment's IdP address is unusable. */
export const IDP_ACCOUNT_SETTINGS_URL = idpAccountSettingsUrl(IDP_LOGOUT_URL);
