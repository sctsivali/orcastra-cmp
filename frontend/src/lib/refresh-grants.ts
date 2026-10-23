// One registry for refresh grants, so a refresh token is presented to the IdP at most once.
//
// important: Authentik rotates the refresh token on every grant by default (2025.10,
// refresh_token_threshold "seconds=0"): the old one is revoked, and presenting it again answers
// invalid_grant and records a SUSPICIOUS_REQUEST event. Readers that arrive together with one
// spent cookie (a burst of session reads, a second tab, a 401 retry) share the grant in flight,
// and a reader that arrives just after it settled is handed the result instead of a second grant.
//
// important: the window after a rotation is short on purpose (KEEP_MS, MAX_HOPS). Healing an
// older cookie into the newest tokens also means the IdP never sees a reuse, which is how rotation
// detects a copied session. The window only has to cover readers that were already in flight when
// the rotation landed; session reads wait for the grant itself (auth.ts), so nothing depends on
// healing a cookie minutes later.
//
// note: per process. The dashboard runs one frontend process and only the nodejs runtime grants
// (middleware has its own non-refreshing NextAuth instance). More replicas need a shared store.

export interface RefreshedFields {
  accessToken: string;
  refreshToken: string;
  expiresAt: number;
  groupsReadAt: number;
  groups?: string[];
  roles?: string[];
}

export type GrantOutcome =
  | { ok: true; fields: RefreshedFields }
  | { ok: false; forgotten?: boolean };

interface Entry {
  sub: string;
  outcome: Promise<GrantOutcome>;
  settled?: { fields: RefreshedFields; nextKey: string; at: number };
  keepUntil: number;
  joined: number;
}

const KEEP_MS = 60_000;
const MAX_HOPS = 2;
// Longer than an access token plus the refresh skew, so a cookie from before a sign-out cannot
// refresh through the dashboard before it would have expired anyway.
const TOMBSTONE_MS = 10 * 60_000;
const MAX_ENTRIES = 4096;
const MAX_PER_SUBJECT = 16;

const entries = new Map<string, Entry>();
const tombstones = new Map<string, number>();

export async function grantKey(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  let binary = "";
  for (const byte of new Uint8Array(digest)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function sweep(now: number): void {
  for (const [key, until] of tombstones) if (until <= now) tombstones.delete(key);
  const perSubject = new Map<string, number>();
  for (const [key, entry] of entries) {
    if (entry.keepUntil <= now) {
      entries.delete(key);
      continue;
    }
    if (entry.settled) perSubject.set(entry.sub, (perSubject.get(entry.sub) ?? 0) + 1);
  }
  // Oldest first: one subject cannot hold more than its share, and the whole map stays bounded.
  let evicted = 0;
  for (const [key, entry] of entries) {
    if (!entry.settled) continue;
    const own = perSubject.get(entry.sub) ?? 0;
    if (own > MAX_PER_SUBJECT || entries.size > MAX_ENTRIES) {
      entries.delete(key);
      perSubject.set(entry.sub, own - 1);
      evicted += 1;
    }
  }
  if (evicted) console.warn(`[AUTH] grant registry evicted ${evicted} kept rotations (bounds reached)`);
}

/** The newest kept rotation reachable from this key, within MAX_HOPS. */
function walk(key: string, sub: string, now: number): { fields: RefreshedFields; hops: number; at: number } | null {
  let found: { fields: RefreshedFields; hops: number; at: number } | null = null;
  let hops = 0;
  while (hops < MAX_HOPS) {
    const entry = entries.get(key);
    if (!entry?.settled || entry.sub !== sub || entry.keepUntil <= now || tombstones.has(key)) break;
    hops += 1;
    found = { fields: entry.settled.fields, hops, at: entry.settled.at };
    key = entry.settled.nextKey;
  }
  return found;
}

/** The newest tokens of a chain this refresh token was spent in moments ago, if any. */
export async function upgradeSpent(
  sub: string,
  refreshToken: string,
): Promise<{ fields: RefreshedFields; hops: number; ageMs: number } | null> {
  const key = await grantKey(refreshToken);
  const now = Date.now();
  const found = walk(key, sub, now);
  return found ? { fields: found.fields, hops: found.hops, ageMs: now - found.at } : null;
}

/**
 * Start a grant for this refresh token, join the one already running, or take the result of one
 * that settled moments ago. `outcome` settles when the IdP answers or the grant's cap runs out.
 * note: wrapped in an object on purpose; awaiting a promise of a promise would wait for the grant.
 * important: everything after the one await is synchronous, so a grant that settles meanwhile is
 * seen here and never overwritten by a second grant with its spent token.
 */
export async function grantOnce(
  sub: string,
  refreshToken: string,
  grant: () => Promise<GrantOutcome>,
): Promise<{ outcome: Promise<GrantOutcome> }> {
  const key = await grantKey(refreshToken);
  const now = Date.now();
  // A signed-out chain is not presented to the IdP again: it would only be refused.
  if (tombstones.has(key)) return { outcome: Promise.resolve({ ok: false, forgotten: true }) };
  const existing = entries.get(key);
  if (existing && existing.keepUntil > now) {
    if (existing.sub !== sub) return { outcome: Promise.resolve({ ok: false }) };
    if (!existing.settled) {
      existing.joined += 1;
      return { outcome: existing.outcome };
    }
    const head = walk(key, sub, now);
    if (head) return { outcome: Promise.resolve({ ok: true, fields: head.fields }) };
  }

  const entry: Entry = { sub, outcome: Promise.resolve({ ok: false }), keepUntil: Infinity, joined: 0 };
  entry.outcome = grant().then(async (outcome): Promise<GrantOutcome> => {
    // Deliberate and permanent: proves from outside that a burst made exactly one grant, and
    // which runtime made it. Logs no token material.
    console.info(
      `[AUTH] refresh grant runtime=${process.env.NEXT_RUNTIME ?? "nodejs"} ` +
        `shared=${entry.joined + 1} ms=${Date.now() - now} ok=${outcome.ok}`,
    );
    if (tombstones.has(key)) {
      entries.delete(key);
      return { ok: false, forgotten: true };
    }
    // Keep only a real rotation: an IdP that hands back the same refresh token would otherwise
    // link the key to itself.
    if (outcome.ok && outcome.fields.refreshToken && outcome.fields.refreshToken !== refreshToken) {
      entry.settled = { fields: outcome.fields, nextKey: await grantKey(outcome.fields.refreshToken), at: Date.now() };
      entry.keepUntil = Date.now() + KEEP_MS;
    } else {
      entries.delete(key);
    }
    return outcome;
  }).catch((): GrantOutcome => {
    entries.delete(key);
    return { ok: false };
  });
  entries.set(key, entry);
  sweep(now);
  return { outcome: entry.outcome };
}

/**
 * Sign-out: drop every link that leads into this refresh token's chain, so a cookie from before
 * the sign-out is not healed into it, and make a grant still in flight for it settle as forgotten.
 */
export async function forget(sub: string, refreshToken: string): Promise<void> {
  const chain = new Set<string>();
  let key: string | undefined = await grantKey(refreshToken);
  while (key && !chain.has(key) && chain.size < MAX_ENTRIES) {
    chain.add(key);
    key = entries.get(key)?.settled?.nextKey;
  }
  for (let grew = true; grew; ) {
    grew = false;
    for (const [k, entry] of entries) {
      if (entry.sub === sub && entry.settled && chain.has(entry.settled.nextKey) && !chain.has(k)) {
        chain.add(k);
        grew = true;
      }
    }
  }
  const until = Date.now() + TOMBSTONE_MS;
  for (const k of chain) {
    if (entries.get(k)?.settled) entries.delete(k);
    tombstones.set(k, until);
  }
}
