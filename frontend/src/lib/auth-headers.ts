// Headers the browser and the /api/v1 proxy share. A module of its own so the browser bundle does
// not pull in the proxy's server-only session reader.

/** The signed-in account a page acts for, URI-encoded. The proxy compares it with the cookie. */
export const ACTOR_HEADER = "x-orcastra-actor";
/** Why the proxy answered 401 itself: "session-ended" or "actor-changed". */
export const AUTH_SIGNAL_HEADER = "x-orcastra-auth";

/**
 * Names one account on a request, for a caller that bound its request to the actor it started
 * under: once another account signs in on this browser, the proxy refuses it instead of running
 * it as the new account.
 */
export function actorHeaders(actorId: string | null | undefined): Record<string, string> {
  return actorId ? { [ACTOR_HEADER]: encodeURIComponent(actorId) } : {};
}
