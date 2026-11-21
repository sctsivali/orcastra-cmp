/**
 * The deployment edition this build is, and the client-safe facts about it.
 *
 * Full is the default. Another edition's branch sets these values in its own build, so nothing is
 * read from the environment at runtime and a page never has to ask which edition it is in. Keep
 * this module free of server-only imports: client components, middleware and auth.ts all read it.
 */

export const EDITION: string = "full";

/** The product name a deployment without its own branding shows (tab title, sign-in card). */
export const PRODUCT_NAME: string = "Orcastra CMP";

/** The NextAuth provider every sign-in call names. */
export const AUTH_PROVIDER_ID: string = "authentik";

/** Surfaces Full shows that another edition may leave out, by id. */
export type EditionSurface = "settings.user-groups" | "integrations.synced-organizations";

const HIDDEN_SURFACES: ReadonlySet<EditionSurface> = new Set<EditionSurface>();

export function editionShows(surface: EditionSurface): boolean {
  return !HIDDEN_SURFACES.has(surface);
}

/**
 * Pages an edition adds that the authorized() callback in auth.ts refuses below their role.
 * Plain string arrays so scripts/checks/check-role-gates.mjs can read them like the lists there.
 */
export const EDITION_ADMIN_OR_PARTNER_PATHS: readonly string[] = [];
export const EDITION_ADMIN_ONLY_PATHS: readonly string[] = [];

/** The version shown in the footer. */
export function footerVersion(appVersion: string): string {
  return `v${appVersion}`;
}
