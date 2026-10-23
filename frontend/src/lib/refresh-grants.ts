// One registry for refresh grants, so a refresh token is presented to the IdP at most once.
//
// important: Authentik rotates the refresh token on every grant by default (2025.10,
// refresh_token_threshold "seconds=0"): the old one is revoked, and presenting it again answers
// invalid_grant and records a SUSPICIOUS_REQUEST event. A rotated token reaches the browser only
// through the session cookie, so anything that holds an older cookie (a request that started
// before the rotation landed, a second tab, a grant that finished after its caller gave up) would
// present a spent token and end the session. This registry keeps each grant until it settles and
// keeps a successful rotation for KEEP_MS, so those callers get the newest tokens instead of a
// second grant.
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
  settled?: { ok: true; fields: RefreshedFields; nextKey: string };
  keepUntil: number;
  joined: number;
}

// Long enough for a request, a tab or a late grant holding the older cookie to come back
// (the session poll runs every 240 s); short enough that a cookie a few rotations old is refused.
const KEEP_MS = 10 * 60_000;
const TOMBSTONE_MS = 2 * 60_000;
const MAX_ENTRIES = 4096;
const MAX_HOPS = 64;

const entries = new Map<string, Entry>();
const tombstones = new Map<string, number>();

async function grantKey(refreshToken: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(refreshToken));
  let binary = "";
  for (const byte of new Uint8Array(digest)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function sweep(now: number): void {
  for (const [key, until] of tombstones) if (until <= now) tombstones.delete(key);
  for (const [key, entry] of entries) if (entry.keepUntil <= now) entries.delete(key);
  // Oldest settled entries go first; losing one costs a grant, never a wrong token.
  for (const [key, entry] of entries) {
    if (entries.size <= MAX_ENTRIES) break;
    if (entry.settled) entries.delete(key);
  }
}

/** The newest tokens of a chain this refresh token was already spent in, if any. */
export async function upgradeSpent(
  sub: string,
  refreshToken: string,
): Promise<{ fields: RefreshedFields; hops: number } | null> {
  const now = Date.now();
  let key = await grantKey(refreshToken);
  let newest: RefreshedFields | null = null;
  let hops = 0;
  while (hops < MAX_HOPS) {
    const entry = entries.get(key);
    if (!entry?.settled || entry.sub !== sub || entry.keepUntil <= now || tombstones.has(key)) break;
    newest = entry.settled.fields;
    key = entry.settled.nextKey;
    hops += 1;
  }
  return newest ? { fields: newest, hops } : null;
}

/**
 * Start a grant for this refresh token, or join the one already running. `outcome` settles when
 * the IdP answers (or the grant's own cap runs out), whoever is still waiting.
 * note: wrapped in an object on purpose; awaiting a promise of a promise would wait for the grant.
 */
export async function grantOnce(
  sub: string,
  refreshToken: string,
  grant: () => Promise<GrantOutcome>,
): Promise<{ outcome: Promise<GrantOutcome> }> {
  const key = await grantKey(refreshToken);
  // A signed-out chain is not presented to the IdP again: it would only be refused.
  if (tombstones.has(key)) return { outcome: Promise.resolve({ ok: false, forgotten: true }) };
  const running = entries.get(key);
  if (running && running.sub === sub && !running.settled) {
    running.joined += 1;
    return { outcome: running.outcome };
  }

  const startedAt = Date.now();
  const entry: Entry = { sub, outcome: Promise.resolve({ ok: false }), keepUntil: Infinity, joined: 0 };
  entry.outcome = grant().then(async (outcome): Promise<GrantOutcome> => {
    // Deliberate and permanent: proves from outside that a burst made exactly one grant, and
    // which runtime made it. Logs no token material.
    console.info(
      `[AUTH] refresh grant runtime=${process.env.NEXT_RUNTIME ?? "nodejs"} ` +
        `shared=${entry.joined + 1} ms=${Date.now() - startedAt} ok=${outcome.ok}`,
    );
    if (tombstones.has(key)) {
      entries.delete(key);
      return { ok: false, forgotten: true };
    }
    // Keep only a real rotation: an IdP that hands back the same refresh token would otherwise
    // link the key to itself.
    if (outcome.ok && outcome.fields.refreshToken && outcome.fields.refreshToken !== refreshToken) {
      entry.settled = { ok: true, fields: outcome.fields, nextKey: await grantKey(outcome.fields.refreshToken) };
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
  sweep(startedAt);
  return { outcome: entry.outcome };
}

/**
 * Sign-out: drop every link that leads into this refresh token's chain, so a cookie from before
 * the sign-out is not healed into it, and make a grant still in flight for it settle as forgotten.
 */
export async function forget(sub: string, refreshToken: string): Promise<void> {
  const chain = new Set<string>();
  let key: string | undefined = await grantKey(refreshToken);
  while (key && !chain.has(key) && chain.size < MAX_HOPS) {
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

/** The outcome if it settles within `ms`, otherwise undefined (the grant keeps running). */
export function settleWithin(outcome: Promise<GrantOutcome>, ms: number): Promise<GrantOutcome | undefined> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(undefined), ms);
    (timer as { unref?: () => void }).unref?.();
    void outcome.then((settled) => {
      clearTimeout(timer);
      resolve(settled);
    });
  });
}
