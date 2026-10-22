/**
 * Which response bodies the backend served past their cache TTL (X-Orcastra-Cache: stale).
 *
 * The backend answers a fan-out list from its last read for up to three minutes past the TTL
 * while one background read replaces it (backend core/swr.py). Left alone, the page would keep
 * that older body until its next poll, which for the fleet lists is three minutes away. The
 * QueryCache in components/providers.tsx asks again a few seconds later instead, when the
 * replacement has normally landed.
 *
 * note: keyed by the parsed body object, so a query function that reshapes the body (rather
 * than returning it) simply converges on its own poll, as before.
 */

const staleBodies = new WeakSet<object>();

export function noteServedStale(body: unknown, cacheHeader: string | null): void {
  if (cacheHeader === "stale" && body !== null && typeof body === "object") {
    staleBodies.add(body);
  }
}

export function wasServedStale(data: unknown): boolean {
  return data !== null && typeof data === "object" && staleBodies.has(data);
}

// The background read usually lands within a fan-out's duration; a second, later look covers a
// slow one. Two looks per stale answer, never more: a cluster that stays down must not turn this
// into a poll.
export const STALE_FOLLOW_UP_DELAYS_MS = [4_000, 12_000] as const;
