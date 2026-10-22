/**
 * The shape a server-read session travels in from the root layout to SessionProvider.
 *
 * important: the access token is masked with a fresh random pad on every render. The document is
 * gzip-compressed and also echoes the request's query string, so an unmasked bearer next to
 * attacker-chosen text is the BREACH setup: an observer of encrypted response sizes could
 * recover it a byte at a time over a TLS deployment. A token XORed with a new pad per response
 * compresses differently every time, which takes away the length signal. Same technique as
 * masked CSRF tokens. The real token only ever exists in memory, after unmaskSession.
 *
 * Isomorphic on purpose: masking runs in the server render, unmasking in the browser (and in the
 * server render of the client tree), and scripts/checks exercise both directly.
 */
import type { Session } from "next-auth";

export type HydratedSession = Omit<Session, "accessToken"> & {
  /** "<pad>.<token XOR pad>", both base64url. */
  maskedAccessToken: string;
};

function toBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(text: string): Uint8Array {
  const base64 = text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4);
  return Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
}

export function maskToken(token: string): string {
  const plain = new TextEncoder().encode(token);
  const pad = crypto.getRandomValues(new Uint8Array(plain.length));
  const masked = plain.map((byte, i) => byte ^ pad[i]);
  return `${toBase64Url(pad)}.${toBase64Url(masked)}`;
}

/** The token, or undefined for anything that is not a well-formed masked value. */
export function unmaskToken(value: string): string | undefined {
  const [padText, maskedText, extra] = value.split(".");
  if (!padText || !maskedText || extra !== undefined) return undefined;
  try {
    const pad = fromBase64Url(padText);
    const masked = fromBase64Url(maskedText);
    if (pad.length !== masked.length || pad.length === 0) return undefined;
    return new TextDecoder().decode(masked.map((byte, i) => byte ^ pad[i]));
  } catch {
    return undefined;
  }
}

/** The session SessionProvider takes, or undefined so the browser fetches one as before. */
export function unmaskSession(hydrated: HydratedSession | undefined): Session | undefined {
  if (!hydrated) return undefined;
  const { maskedAccessToken, ...rest } = hydrated;
  const accessToken = unmaskToken(maskedAccessToken);
  return accessToken ? { ...rest, accessToken } : undefined;
}
