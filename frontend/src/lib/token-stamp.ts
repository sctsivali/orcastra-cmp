/**
 * A non-secret stamp of an access token: the first 12 bytes of its SHA-256, in hex. It changes
 * whenever the token rotates, which is all the browser needs to know about the token, and the token
 * cannot be recovered from it. Runs wherever the session callback runs (Node and the edge runtime).
 */
export async function tokenStamp(accessToken: string | null | undefined): Promise<string | undefined> {
  if (!accessToken) return undefined;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(accessToken));
  return Array.from(new Uint8Array(digest).slice(0, 12), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
