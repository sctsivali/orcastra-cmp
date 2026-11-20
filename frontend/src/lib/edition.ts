/**
 * The deployment edition this build is, and the client-safe facts about it.
 *
 * Full is the default. Another edition's branch sets these values in its own build, so nothing is
 * read from the environment at runtime and a page never has to ask which edition it is in. Keep
 * this module free of server-only imports: client components, middleware and auth.ts all read it.
 */

// The CMP release this build is based on: package.json carries it unchanged from CMP, while
// APP_VERSION carries the Mini version (next.config.js).
import packageJson from "../../package.json";

export const EDITION: string = "mini";

/** The NextAuth provider every sign-in call names. */
export const AUTH_PROVIDER_ID: string = "client-cert";

/** Surfaces Full shows that another edition may leave out, by id. */
export type EditionSurface = "settings.user-groups" | "integrations.synced-organizations";

// Mini has no identity provider to sync roles to and no marketplace federation.
const HIDDEN_SURFACES: ReadonlySet<EditionSurface> = new Set<EditionSurface>([
  "settings.user-groups",
  "integrations.synced-organizations",
]);

export function editionShows(surface: EditionSurface): boolean {
  return !HIDDEN_SURFACES.has(surface);
}

/**
 * Pages an edition adds that the authorized() callback in auth.ts refuses below their role.
 * Plain string arrays so scripts/checks/check-role-gates.mjs can read them like the lists there.
 */
export const EDITION_ADMIN_OR_PARTNER_PATHS: readonly string[] = ["/operators"];
export const EDITION_ADMIN_ONLY_PATHS: readonly string[] = ["/audit-log"];

/** The version shown in the footer: the Mini release and the CMP release under it. */
export function footerVersion(appVersion: string): string {
  return `Mini ${appVersion} (CMP ${packageJson.version})`;
}
